"""行情富化数据的本地缓存：股票名称、流通市值。

为什么需要
----------
推送卡片要显示股票名称、海龟策略要按流通市值排序。这两件事原先都是"每只股票发一次
baostock 查询"，实测成本：

* ``query_stock_basic``（查名称）：约 180 ms/只。RPS 一天选出 208 只，光查名字就 **37.7 s**；
* ``query_history_k_data_plus``（查市值）：约 230 ms/只，54 个候选约 **12 s**。

而这些数据变化很慢，所以改为本地 SQLite 表缓存：

* ``stock_info``：用**一次** ``query_all_stock`` 拉全市场名单（实测约 30 s）填满 5,000+ 只，
  之后名称查询全部走本地表（毫秒级）。列表按"锚定最近交易日"取，避免非交易日为空；
* ``market_cap``：按 ``(symbol, date)`` 缓存，同一天重复运行（调参、补推送）零网络成本。

为什么不做"每次运行都刷新"
--------------------------
实测这台机器上全市场名单的两个来源都不便宜：``query_stock_basic(code_name="")`` 会
**超时**（err=10002007），``akshare.stock_info_a_code_name()`` 要 27 s 且
``stock_zh_a_spot_em()`` 直接断连。所以策略是"冷启动付一次约 30 s，之后长期命中"，
并且只在**缓存为空**或**缺失较多**时才触发刷新，避免为了一只新股票每次多花 30 s。

设计说明
--------
* 两张表都用 ``CREATE TABLE IF NOT EXISTS`` 建，老库自动迁移，无需手工 ALTER；
* 构造函数可接受一个已存在的 ``sqlite3.Connection``（测试用 ``:memory:``），
  不传则每个操作自行开关连接（生产用，与 ``DataEngine`` 的风格一致）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

#: SQLite 单条语句的绑定参数上限较保守，分批查询更稳
_CHUNK = 400

#: 缺失达到这个数量才值得重建整个目录（否则直接让调用方回落显示代码）
_MIN_MISSING_TO_REFRESH = 3

_CREATE_STOCK_INFO = """
CREATE TABLE IF NOT EXISTS stock_info (
    symbol     TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

_CREATE_MARKET_CAP = """
CREATE TABLE IF NOT EXISTS market_cap (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    value  REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);
"""


def to_stock_symbol(bs_code: str) -> str | None:
    """把 baostock 代码转成 6 位股票代码；指数/基金等非股票返回 None。

    ``query_all_stock`` 会把指数、ETF 一起返回，其中 ``sh.000001``（上证指数）的数字
    部分与深市股票 ``000001`` 冲突，必须按"市场 + 代码段"过滤掉：

    * 上交所：``6xxxxx``（主板/科创板）、``9xxxxx``（B 股）
    * 深交所：``00xxxx``（主板/中小板）、``30xxxx``（创业板）、``20xxxx``（B 股）
    * 其它（``sh.000xxx`` 指数、``sz.399xxx`` 指数、``5xxxxx``/``1xxxxx`` 基金）一律排除

    Args:
        bs_code: 形如 ``'sh.600000'`` / ``'sz.000001'``。

    Returns:
        6 位代码，或 None（非股票）。
    """
    if "." not in bs_code:
        return None
    market, number = bs_code.split(".", 1)
    if market == "sh" and number[:1] in ("6", "9"):
        return number
    if market == "sz" and number[:2] in ("00", "30", "20"):
        return number
    return None


class _SqliteTable:
    """带可选外部连接的 SQLite 表基类。"""

    _DDL: str = ""

    def __init__(
        self,
        db_path: str,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """
        Args:
            db_path: 数据库文件路径（``conn`` 为 None 时使用）。
            conn: 可选的共享连接；传入时本对象不会开关它（测试用）。
        """
        self.db_path = db_path
        self._conn = conn
        if conn is None:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """产出连接：外部连接直接复用，否则临时开关一个。"""
        if self._conn is not None:
            with self._conn:  # 只提交事务，不关闭
                yield self._conn
        else:
            with sqlite3.connect(self.db_path) as conn:
                yield conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(self._DDL)
            conn.commit()


class StockDirectory(_SqliteTable):
    """股票代码 → 名称的本地目录。"""

    _DDL = _CREATE_STOCK_INFO

    def get_names(self, symbols: list[str]) -> dict[str, str]:
        """返回 ``{代码: 名称}``；本地缺失较多时重建一次全市场目录。

        刷新策略：缓存为空、或缺失数量达到 ``_MIN_MISSING_TO_REFRESH`` 时才重建
        （重建约 30 s）。只缺一两个代码（大概率是已退市/异常代码）时直接返回部分结果，
        由调用方回落显示代码，避免为一只票付出 30 s。

        Args:
            symbols: 需要查询的股票代码（6 位纯数字）。

        Returns:
            已命中的映射；查不到的代码不会出现在结果里（调用方自行回落）。
        """
        if not symbols:
            return {}

        wanted = list(dict.fromkeys(symbols))  # 去重且保持顺序
        found = self._select(wanted)

        missing = [s for s in wanted if s not in found]
        if not missing:
            return found

        if self.count() == 0 or len(missing) >= _MIN_MISSING_TO_REFRESH:
            if self.refresh():
                found.update(self._select(missing))

        return found

    def _select(self, symbols: list[str]) -> dict[str, str]:
        """批量查本地表（分批，避免超出绑定参数上限）。"""
        result: dict[str, str] = {}
        with self._connect() as conn:
            for i in range(0, len(symbols), _CHUNK):
                chunk = symbols[i : i + _CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT symbol, name FROM stock_info WHERE symbol IN ({placeholders})",
                    chunk,
                ).fetchall()
                result.update({row[0]: row[1] for row in rows})
        return result

    def _latest_trade_day(self) -> str | None:
        """取本地行情表里最新的交易日，作为``query_all_stock`` 的锚点。

        Returns:
            日期字符串；库里还没有数据时返回 None。
        """
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
        except sqlite3.Error as exc:
            logger.warning(f"读取本地交易日失败：{exc}")
            return None
        return row[0] if row and row[0] else None

    def refresh(self) -> int:
        """用一次 baostock 全市场名单查询重建名称缓存（约 30 s）。

        名称极少变化；需要强制刷新（例如 ST 摘帽后）时直接再调用一次即可。

        Returns:
            写入（或更新）的记录数；失败时返回 0，不抛异常。
        """
        import baostock as bs

        day = self._latest_trade_day()
        if day is None:
            logger.warning("股票目录刷新跳过：本地还没有行情数据（请先 --backfill）")
            return 0

        lg = bs.login()
        if lg.error_code != "0":
            logger.warning(f"股票目录刷新失败，baostock 登录错误：{lg.error_msg}")
            return 0

        logger.info(f"正在拉取全市场股票名单（锚定 {day}，约 30 秒）...")
        try:
            rs = bs.query_all_stock(day=day)
            if rs.error_code != "0":
                logger.warning(f"股票目录刷新失败：{rs.error_msg}")
                return 0
            rows: list[tuple[str, str]] = []
            while rs.next():
                data = rs.get_row_data()
                if len(data) < 3:
                    continue
                symbol = to_stock_symbol(data[0])
                if symbol and data[2]:
                    rows.append((symbol, data[2]))
        except Exception as exc:
            logger.warning(f"股票目录刷新失败：{exc}")
            return 0
        finally:
            bs.logout()

        if not rows:
            logger.warning("股票目录刷新失败：baostock 未返回任何股票")
            return 0

        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO stock_info (symbol, name) VALUES (?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET "
                "name = excluded.name, updated_at = datetime('now', 'localtime')",
                rows,
            )
            conn.commit()

        logger.info(f"股票目录已刷新：{len(rows)} 只")
        return len(rows)

    def count(self) -> int:
        """本地缓存中的股票数量。"""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM stock_info").fetchone()
        return int(row[0]) if row else 0


class MarketCapCache(_SqliteTable):
    """按 ``(symbol, date)`` 缓存流通市值。"""

    _DDL = _CREATE_MARKET_CAP

    def get(self, symbols: list[str], day: str) -> dict[str, float]:
        """读取指定日期的缓存市值。

        Args:
            symbols: 股票代码列表。
            day: 日期字符串 ``YYYY-MM-DD``。

        Returns:
            ``{代码: 流通市值}``，未命中的代码不出现。
        """
        if not symbols:
            return {}

        result: dict[str, float] = {}
        with self._connect() as conn:
            for i in range(0, len(symbols), _CHUNK):
                chunk = symbols[i : i + _CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT symbol, value FROM market_cap "
                    f"WHERE date = ? AND symbol IN ({placeholders})",
                    (day, *chunk),
                ).fetchall()
                result.update({row[0]: row[1] for row in rows})
        return result

    def put(self, day: str, values: dict[str, float]) -> None:
        """写入（或覆盖）某一天的多只股票市值。

        Args:
            day: 日期字符串 ``YYYY-MM-DD``。
            values: ``{代码: 流通市值}``。
        """
        if not values:
            return
        rows = [(symbol, day, value) for symbol, value in values.items()]
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO market_cap (symbol, date, value) VALUES (?, ?, ?) "
                "ON CONFLICT(symbol, date) DO UPDATE SET value = excluded.value",
                rows,
            )
            conn.commit()
