"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.snapshot import MarketSnapshot

logger = get_logger(__name__)

#: 快照窗口下限：UptrendLimitDownStrategy 需要 60 日均线 + 昨日一行
_MIN_SNAPSHOT_DAYS = 62

#: 快照所需的列（避免 SELECT * 多读 id 列）
_SNAPSHOT_COLUMNS = "symbol,date,open,high,low,close,volume,turnover"

#: baostock 日线查询字段
_BS_FIELDS = "date,open,high,low,close,volume,amount"

#: 连续多少次查询失败就重连 baostock（socket 进入坏状态后必须重连才恢复）
_MAX_CONSECUTIVE_FAILS = 5

#: 写入行情表：按 (symbol, date) 幂等 upsert，绝不删整天的数据
_UPSERT_SQL = """
INSERT INTO stock_daily (symbol, date, open, high, low, close, volume, turnover)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(symbol, date) DO UPDATE SET
    open = excluded.open, high = excluded.high, low = excluded.low,
    close = excluded.close, volume = excluded.volume, turnover = excluded.turnover
"""


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""


def _bs_login(bs) -> bool:
    """登录 baostock；任何异常都当成失败返回 False。"""
    try:
        lg = bs.login()
    except Exception:  # baostock 在连接失败时可能直接抛异常，也可能只打印
        return False
    return getattr(lg, "error_code", "1") == "0"


class _StopFlag:
    """worker 内部的中断标志。

    为什么不用 ``multiprocessing.Event``：它无法作为 ``Pool.map`` 的任务参数传递
    （会抛 "Condition objects should only be shared between processes through
    inheritance"），而本场景只需要"本进程提前收工"的语义，用普通对象即可
    （随任务一起 pickle，每个 worker 拿到的是自己的副本）。
    """

    __slots__ = ("_flag",)

    def __init__(self) -> None:
        self._flag = False

    def is_set(self) -> bool:
        return self._flag

    def set(self) -> None:
        self._flag = True


def _bs_fetch_batch(task: tuple) -> tuple[list, dict]:
    """多进程 worker：在预算内批量拉取 baostock 日线。

    为什么需要"预算 + 中断 + 重连"：
    baostock 的 ``send_msg`` 把 socket 异常全部吞掉只打印一行（"timed out"），
    查询本身**不会抛异常**，只会返回一个错误码。所以旧实现遇到网络故障时，
    每只股票都要各自等满 socket 超时（10 s），一个进程 650 只 ≈ 1.8 小时，
    表现为"跑了一晚上没结束"。这里加了三道闸：

    1. ``deadline``：每个 symbol 之前检查，到点立即返回已取到的部分结果；
    2. ``stop_flag``：本进程判定连接彻底不可用时立即收工（不再磨剩余的股票）；
    3. 连续 ``_MAX_CONSECUTIVE_FAILS`` 次失败就 logout + 重新 login 一次，
       因为 socket 超时后连接已错位，不重连后面必然全失败。

    Args:
        task: ``(items, deadline, stop_flag)``，其中 ``items`` 是
            ``(symbol, bs_code, start, end)`` 的列表，``deadline`` 是
            ``time.monotonic()`` 口径的时间戳（None 表示不限）。

    Returns:
        ``(rows, stats)``：``rows`` 为 ``[symbol, date, open, ...]`` 列表，
        ``stats`` 记录 ok / empty / failed / aborted / reconnects。
    """
    import time

    import baostock as bs

    items, deadline, stop_flag = task
    rows: list[list[str]] = []
    stats = {"ok": 0, "empty": 0, "failed": 0, "aborted": 0, "reconnects": 0}

    if not _bs_login(bs):
        stats["aborted"] = len(items)
        return rows, stats

    consecutive_fails = 0
    try:
        for index, (symbol, bs_code, start, end) in enumerate(items):
            if stop_flag.is_set() or (deadline is not None and time.monotonic() >= deadline):
                stats["aborted"] += len(items) - index
                break

            try:
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    _BS_FIELDS,
                    start_date=start,
                    end_date=end,
                    frequency="d",
                    adjustflag="1",  # 后复权
                )
                if rs.error_code != "0":
                    raise RuntimeError(f"{rs.error_code} {rs.error_msg}")

                got = 0
                while rs.next():
                    rows.append([symbol] + rs.get_row_data())
                    got += 1
            except Exception:
                stats["failed"] += 1
                consecutive_fails += 1
                if consecutive_fails >= _MAX_CONSECUTIVE_FAILS:
                    # 连接大概率已错位：重连一次，仍失败就整体放弃
                    stats["reconnects"] += 1
                    try:
                        bs.logout()
                    except Exception:
                        pass
                    time.sleep(0.5)
                    if not _bs_login(bs):
                        stop_flag.set()
                        stats["aborted"] += len(items) - index - 1
                        break
                    consecutive_fails = 0
                continue

            consecutive_fails = 0
            stats["ok"] += 1
            if got == 0:
                stats["empty"] += 1
    finally:
        try:
            bs.logout()
        except Exception:
            pass

    return rows, stats


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self.snapshot_days: int = settings.snapshot_days
        self.sync_workers: int = settings.sync_workers
        self.sync_max_seconds: float = settings.sync_max_seconds
        #: 自选股范围；None 表示全市场
        self.universe: list[str] | None = None
        self._snapshot: MarketSnapshot | None = None
        self._init_db()

    def set_universe(self, symbols: list[str] | None) -> None:
        """限定选股范围（自选股）。

        Args:
            symbols: 6 位股票代码列表；空列表或 None 表示恢复全市场。
        """
        cleaned = sorted({s.strip() for s in (symbols or []) if s and s.strip()})
        self.universe = cleaned or None
        self.invalidate_snapshot()
        if self.universe:
            logger.info(f"选股范围限定为 {len(self.universe)} 只自选股")

    def _apply_universe(self, df: pd.DataFrame) -> pd.DataFrame:
        """把 DataFrame 过滤到自选股范围（未设置范围时原样返回）。"""
        if self.universe is None or df.empty:
            return df
        return df[df["symbol"].isin(self.universe)].reset_index(drop=True)

    def is_trading_day(self, day: str | None = None) -> bool:
        """判断是否为 A 股交易日（用于跳过周末/节假日的空跑）。

        查询失败时**按交易日处理**（fail-open），宁可白跑一次也不要漏掉数据。

        Args:
            day: ``YYYY-MM-DD``；None 表示今天。

        Returns:
            True 表示交易日。
        """
        from datetime import date as _date

        import baostock as bs

        target = day or _date.today().strftime("%Y-%m-%d")
        if not _bs_login(bs):
            logger.warning("交易日判断失败（登录异常），按交易日处理")
            return True
        try:
            rs = bs.query_trade_dates(start_date=target, end_date=target)
            if rs.error_code != "0":
                logger.warning(f"交易日查询失败：{rs.error_msg}，按交易日处理")
                return True
            while rs.next():
                row = rs.get_row_data()
                return row[1] == "1"
            return True
        except Exception as exc:
            logger.warning(f"交易日查询异常：{exc}，按交易日处理")
            return True
        finally:
            try:
                bs.logout()
            except Exception:
                pass

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def _get_last_date(self, symbol: str) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    # ── 全市场快照（策略层的统一读入口）──

    def invalidate_snapshot(self) -> None:
        """丢弃已缓存的行情快照（数据写入后调用）。"""
        self._snapshot = None

    def get_snapshot(self, days: int | None = None) -> MarketSnapshot:
        """返回全市场最近 N 个交易日的行情快照（进程内缓存，只加载一次）。

        这是策略层推荐的读取方式：一次查询取回全市场数据，替代"逐只 get_ohlcv"。
        首次调用时加载，之后的调用直接复用同一份 DataFrame 与指标缓存。

        Args:
            days: 窗口内的交易日数量；None 表示使用 ``Settings.snapshot_days``。

        Returns:
            MarketSnapshot 实例。

        Raises:
            ValueError: 窗口小于 62 个交易日时抛出（60 日均线策略需要 61 根 K 线）。
        """
        want = self.snapshot_days if days is None else days
        if want < _MIN_SNAPSHOT_DAYS:
            raise ValueError(f"快照窗口至少 {_MIN_SNAPSHOT_DAYS} 个交易日（收到 {want}）")
        if self._snapshot is None or self._snapshot.days != want:
            self._snapshot = self._load_snapshot(want)
        return self._snapshot

    def _load_snapshot(self, days: int) -> MarketSnapshot:
        """从 SQLite 载入最近 ``days`` 个交易日的全市场行情。"""
        import time

        t0 = time.perf_counter()

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT ?",
                (days,),
            ).fetchall()

            if not rows:
                # 空库：用恒假条件取回带正确列结构的空 DataFrame
                df = pd.read_sql(f"SELECT {_SNAPSHOT_COLUMNS} FROM stock_daily WHERE 0", conn)
            else:
                cutoff = min(row[0] for row in rows)
                df = pd.read_sql(
                    f"SELECT {_SNAPSHOT_COLUMNS} FROM stock_daily WHERE date >= ?",
                    conn,
                    params=(cutoff,),
                )

        if not df.empty:
            df = df.sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)
            df = self._apply_universe(df)

        snapshot = MarketSnapshot(df, days)
        logger.info(
            f"行情快照加载完成：{snapshot.describe()}，耗时 {time.perf_counter() - t0:.1f}s"
        )
        return snapshot

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    # ── 数据同步 ──

    def sync_today_bulk(
        self,
        max_seconds: float | None = None,
        workers: int | None = None,
    ) -> int:
        """多进程并行拉取增量行情（后复权）并写入 SQLite。

        与旧实现的三个关键区别：

        1. **有预算**：``max_seconds`` 到点即收工，用已经取回的部分数据继续后面的选股，
           不会出现"网络故障导致整晚不结束"；
        2. **会早停**：worker 连续失败到阈值、或重连失败，就立即结束自己的那一片，
           不再为剩余的股票各等 10 秒超时；
        3. **不删数据**：写入改成 ``ON CONFLICT(symbol,date) DO UPDATE`` 幂等 upsert，
           旧实现"先 ``DELETE ... WHERE date = ?`` 再整批插入"，某只股票取数失败就会
           把那天其它股票的数据永久删掉。

        Args:
            max_seconds: 本次同步的时间预算（秒）。None 用 ``Settings.sync_max_seconds``。
            workers: 并发进程数。None 用 ``Settings.sync_workers``。

        Returns:
            实际写入（或更新）的行数；无可更新数据时返回 0。
        """
        import time
        from datetime import date, timedelta
        from multiprocessing import Pool
        from multiprocessing import TimeoutError as MPTimeoutError

        self.invalidate_snapshot()
        today_str = date.today().strftime("%Y-%m-%d")
        budget = self.sync_max_seconds if max_seconds is None else max_seconds
        worker_count = workers or self.sync_workers

        tasks = []
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()

        if not rows:
            logger.warning("本地无股票数据，请先执行 --backfill")
            return 0

        for symbol, last_date in rows:
            if last_date and last_date >= today_str:
                continue
            start = today_str
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info("所有股票已是最新，无需更新")
            return 0

        n_workers = max(1, min(worker_count, len(tasks)))
        logger.info(
            f"需要更新 {len(tasks)} 只股票，{n_workers} 进程并行（时间预算 {budget:.0f}s）..."
        )

        deadline = time.monotonic() + budget
        chunks = [tasks[i::n_workers] for i in range(n_workers)]
        payload = [(chunk, deadline, _StopFlag()) for chunk in chunks]

        started = time.monotonic()
        results: list = []
        pool = Pool(n_workers)
        try:
            async_result = pool.map_async(_bs_fetch_batch, payload)
            # 硬兜底：worker 内部已按 deadline 自行返回，这里再留 20s 余量防死锁
            results = async_result.get(timeout=budget + 20)
        except MPTimeoutError:
            logger.error(f"同步超过 {budget + 20:.0f}s 仍未返回，强制终止子进程")
        except Exception as exc:  # 子进程崩溃等
            logger.error(f"同步子进程异常：{exc}")
        finally:
            pool.terminate()
            pool.join()

        elapsed = time.monotonic() - started
        all_rows: list = []
        totals = {"ok": 0, "empty": 0, "failed": 0, "aborted": 0, "reconnects": 0}
        for batch_rows, batch_stats in results:
            all_rows.extend(batch_rows)
            for key in totals:
                totals[key] += batch_stats.get(key, 0)

        logger.info(
            f"同步结束（{elapsed:.1f}s）：成功 {totals['ok']} | 无数据 {totals['empty']} | "
            f"失败 {totals['failed']} | 未处理 {totals['aborted']} | "
            f"重连 {totals['reconnects']}"
        )
        if totals["failed"] or totals["aborted"]:
            logger.warning(
                "本次同步未覆盖全部股票（网络/接口异常），"
                "选股将基于已入库的数据；稍后可重跑 main.py 补齐"
            )

        if not all_rows:
            logger.info("无新数据（可能非交易日）")
            return 0

        df = pd.DataFrame(
            all_rows,
            columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"],
        )
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0]

        if df.empty:
            logger.info("解析后无有效数据")
            return 0

        records = list(
            df[["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]].itertuples(
                index=False, name=None
            )
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(_UPSERT_SQL, records)
            conn.commit()

        count = len(records)
        logger.info(f"写入 {count} 条行情（{df['date'].nunique()} 个交易日，幂等 upsert）")
        return count

    def backfill(self, symbols: list[str], max_seconds: float | None = None) -> dict:
        """通过 baostock 批量回填历史日 K 线数据（后复权）。

        容错机制：
        - 单只股票失败自动重试 3 次，间隔递增（2s/4s/8s）
        - 每 200 只股票自动重连 baostock（防止长连接超时）
        - 已入库的自动 skip，中断后可重跑续传
        - **预算**：``max_seconds`` 到点即收工；连续 20 只失败判定网络不可用并提前结束
          （旧实现遇到网络故障会逐只等满 socket 超时，等于挂死）

        Args:
            symbols: 待回填的股票代码。
            max_seconds: 时间预算（秒）；None 表示不限制。

        Returns:
            ``{"success": n, "skipped": n, "failed": n, "aborted": bool}``。
        """
        import time
        from datetime import date, timedelta

        import baostock as bs

        self.invalidate_snapshot()
        today_str = date.today().strftime("%Y-%m-%d")
        max_retries = 3
        reconnect_interval = 200  # 每处理 N 只股票重连一次
        max_consecutive_failed = 20
        deadline = None if max_seconds is None else time.monotonic() + max_seconds

        def _login():
            lg = bs.login()
            if lg.error_code != "0":
                logger.error(f"baostock 登录失败: {lg.error_msg}")
                return False
            return True

        if not _login():
            return {"success": 0, "skipped": 0, "failed": 0, "aborted": True}

        success = 0
        skipped = 0
        failed = 0
        since_reconnect = 0
        consecutive_failed = 0
        aborted = False

        try:
            for i, symbol in enumerate(symbols):
                if deadline is not None and time.monotonic() >= deadline:
                    logger.warning(f"回填达到时间预算（{max_seconds:.0f}s），提前结束")
                    aborted = True
                    break
                last_date = self._get_last_date(symbol)
                if last_date and last_date >= today_str:
                    skipped += 1
                    if (i + 1) % 500 == 0:
                        logger.info(
                            f"已处理 {i + 1}/{len(symbols)}，"
                            f"成功 {success} 跳过 {skipped} 失败 {failed}"
                        )
                    continue

                # 定期重连，防止长连接超时
                since_reconnect += 1
                if since_reconnect >= reconnect_interval:
                    bs.logout()
                    time.sleep(1)
                    if not _login():
                        logger.error("重连失败，终止回填")
                        return
                    since_reconnect = 0

                start = last_date or self.start_date
                if last_date:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")

                bs_code = self._to_baostock_code(symbol)

                # 带重试的查询
                rows = []
                query_ok = False
                for attempt in range(max_retries):
                    try:
                        rs = bs.query_history_k_data_plus(
                            bs_code,
                            "date,open,high,low,close,volume,amount",
                            start_date=start,
                            end_date=today_str,
                            frequency="d",
                            adjustflag="1",  # 后复权
                        )

                        if rs.error_code != "0":
                            raise RuntimeError(rs.error_msg)

                        rows = []
                        while rs.next():
                            rows.append(rs.get_row_data())
                        query_ok = True
                        break

                    except Exception as exc:
                        if attempt < max_retries - 1:
                            wait = 2 ** (attempt + 1)
                            logger.warning(
                                f"[{symbol}] 第{attempt + 1}次失败: {exc}，{wait}s 后重试"
                            )
                            time.sleep(wait)
                            # 重连 baostock
                            bs.logout()
                            time.sleep(1)
                            _login()
                        else:
                            logger.warning(f"[{symbol}] {max_retries}次重试均失败，跳过")

                if not query_ok:
                    failed += 1
                    consecutive_failed += 1
                    if consecutive_failed >= max_consecutive_failed:
                        logger.error(
                            f"连续 {consecutive_failed} 只股票回填失败，"
                            "判定网络/接口不可用，提前结束回填"
                        )
                        aborted = True
                        break
                    continue

                consecutive_failed = 0

                if not rows:
                    skipped += 1
                    continue

                df = pd.DataFrame(rows, columns=rs.fields)
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["close"])
                df = df[df["volume"] > 0]

                if df.empty:
                    skipped += 1
                    continue

                df["symbol"] = symbol
                df = df.rename(columns={"amount": "turnover"})
                df = df[["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]]

                try:
                    with sqlite3.connect(self.db_path) as conn:
                        df.to_sql(
                            "stock_daily",
                            conn,
                            if_exists="append",
                            index=False,
                            method="multi",
                            chunksize=500,
                        )
                except sqlite3.IntegrityError:
                    pass

                success += 1

                if (i + 1) % 500 == 0:
                    logger.info(
                        f"已处理 {i + 1}/{len(symbols)}，"
                        f"成功 {success} 跳过 {skipped} 失败 {failed}"
                    )

        finally:
            bs.logout()

        logger.info(f"回填完成 — 成功: {success} | 跳过: {skipped} | 失败: {failed}")
        if aborted:
            logger.warning("回填未完成（预算用尽或网络异常），重跑即可续传")
        return {
            "success": success,
            "skipped": skipped,
            "failed": failed,
            "aborted": aborted,
        }

    # ── 股票列表 ──

    def get_all_symbols(self) -> list[str]:
        """获取全市场 A 股代码列表（带回退链）。

        实测 ``query_stock_basic(code_name="", code="")`` 在某些网络下会超时
        （``err=10002007`` 网络接收异常），此时回填会拿到空清单、什么都不做。
        因此按以下顺序尝试，第一个拿到结果的就返回：

        1. ``query_stock_basic(code_name="", code="")`` —— 历史方式；
        2. ``query_all_stock(day)`` —— 按交易日取全市场名单，再按代码段过滤掉指数/基金；
        3. 本地库中已有的代码 —— 保证断网时 ``--backfill`` 仍能续传。

        Returns:
            6 位股票代码列表；三者都拿不到时返回空列表。
        """
        for label, fetch in (
            ("query_stock_basic", self._symbols_from_basic),
            ("query_all_stock", self._symbols_from_all_stock),
        ):
            try:
                symbols = fetch()
            except Exception as exc:
                logger.warning(f"{label} 获取股票列表失败：{exc}")
                continue
            if symbols:
                logger.info(f"获取股票列表完成，共 {len(symbols)} 只（来源 {label}）")
                return symbols
            logger.warning(f"{label} 未返回股票列表，尝试下一种方式")

        local = self.get_local_symbols()
        logger.warning(f"在线获取股票列表失败，退回本地库中已有的 {len(local)} 只（回填可续传）")
        return local

    @staticmethod
    def _symbols_from_basic() -> list[str]:
        """方式 1：``query_stock_basic`` 全市场查询，按 type/status 过滤。"""
        import baostock as bs

        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")

        try:
            rs = bs.query_stock_basic(code_name="", code="")
            if rs.error_code != "0":
                raise RuntimeError(f"{rs.error_code} {rs.error_msg}")

            symbols: list[str] = []
            while rs.next():
                row = rs.get_row_data()
                # 字段顺序: code, code_name, ipoDate, outDate, type, status
                code, _name, _ipo, _out, stock_type, status = row[:6]
                if status == "1" and stock_type == "1":
                    symbols.append(code.split(".")[1])
            return symbols
        finally:
            bs.logout()

    def _symbols_from_all_stock(self) -> list[str]:
        """方式 2：``query_all_stock`` 按交易日查询，再从最近 10 天里找一个有数据的交易日。"""
        import baostock as bs

        from sequoia_x.data.cache import to_stock_symbol

        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")

        try:
            for day in self._candidate_days():
                rs = bs.query_all_stock(day=day)
                if rs.error_code != "0":
                    continue
                symbols = set()
                while rs.next():
                    data = rs.get_row_data()
                    symbol = to_stock_symbol(data[0]) if data else None
                    if symbol:
                        symbols.add(symbol)
                if symbols:
                    logger.info(f"query_all_stock 命中交易日 {day}")
                    return sorted(symbols)
            return []
        finally:
            bs.logout()

    def _candidate_days(self, lookback: int = 10) -> list[str]:
        """返回用于查全市场名单的候选交易日（本地最新日期 + 最近 10 个自然日）。"""
        from datetime import date, timedelta

        days: list[str] = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
            if row and row[0]:
                days.append(row[0])
        except sqlite3.Error:
            pass

        today = date.today()
        for offset in range(lookback):
            day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            if day not in days:
                days.append(day)
        return days

    def get_local_symbols(self) -> list[str]:
        """返回本地库中已有行情的股票代码（已按自选股范围过滤）。"""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT DISTINCT symbol FROM stock_daily").fetchall()
        symbols = [row[0] for row in rows]
        if self.universe is not None:
            allowed = set(self.universe)
            symbols = [s for s in symbols if s in allowed]
        return symbols
