"""信号留痕 + 纸面账本：把"每天推了哪些票"变成可复盘的事实。

为什么需要
----------
推送发出去就没了。没有留痕，就永远回答不了三个问题：

1. 这些策略历史上到底选出了什么？
2. 它们后来涨了还是跌了？（不是回测假设，是真实行情）
3. Top 1 和 Top 30 有区别吗？

设计
----
* 表 ``pick_history``：``(run_date, strategy, symbol)`` 唯一，重复运行幂等；
* **不存储收益**：收益永远由 ``stock_daily`` 现算，避免"结算任务没跑就永远缺一天"；
* 口径与回测一致：**T+1 开盘买入，T+h 收盘卖出**，扣 ``cost`` 往返成本；
* 基准是同一天全市场等权收益中位数，用来看超额。

用法
----
```python
ledger = SignalLedger(settings.db_path)
ledger.record("2026-11-23", "ma_volume", ["600000", "000001"])
ledger.report(days=60)
```
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pandas as pd

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

#: 记录表：主键保证同一天同一策略重复运行不会产生重复行
_CREATE_PICK_SQL = """
CREATE TABLE IF NOT EXISTS pick_history (
    run_date   TEXT    NOT NULL,
    strategy   TEXT    NOT NULL,
    symbol     TEXT    NOT NULL,
    rank       INTEGER,
    close      REAL,
    source     TEXT    DEFAULT 'live',
    created_at TEXT,
    PRIMARY KEY (run_date, strategy, symbol)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_pick_date ON pick_history (run_date);
"""

#: 报告里展示的持有期（交易日）
HORIZONS = (1, 5)


class SignalLedger:
    """选股留痕与业绩统计。

    Args:
        db_path: SQLite 路径（与行情同库）。
        cost: 单次往返交易成本（小数），从每笔收益里扣除。
    """

    def __init__(self, db_path: str, cost: float = 0.002) -> None:
        self.db_path = db_path
        self.cost = cost
        self._ensure_table()

    # ── 建表 ──

    def _ensure_table(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_CREATE_PICK_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            # 兼容旧库：早期版本没有 source 列
            columns = {row[1] for row in conn.execute("PRAGMA table_info(pick_history)")}
            if "source" not in columns:
                conn.execute("ALTER TABLE pick_history ADD COLUMN source TEXT DEFAULT 'live'")
            conn.commit()

    # ── 写入 ──

    def record(
        self,
        run_date: str,
        strategy: str,
        symbols: list[str],
        prices: dict[str, float] | None = None,
        source: str = "live",
    ) -> int:
        """记录一次选股结果（幂等）。

        Args:
            run_date: 信号日（应取行情的最后一个交易日，而不是 ``today()``）。
            strategy: 策略标识（``webhook_key``）。
            symbols: 按强度排序的代码列表。
            prices: 可选 ``{symbol: close}``；不给就按 ``run_date`` 从库里补。
            source: ``live``（当天跑出来的）或 ``replay``（历史回放，用于补历史样本）。

        Returns:
            实际新增的行数。
        """
        if not symbols:
            return 0

        if prices is None:
            places = ",".join("?" * len(symbols))
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    f"SELECT symbol, close FROM stock_daily "  # noqa: S608 占位符数量固定
                    f"WHERE date = ? AND symbol IN ({places})",
                    (run_date, *symbols),
                ).fetchall()
            prices = {row[0]: row[1] for row in rows}

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        records = [
            (run_date, strategy, symbol, rank, prices.get(symbol), now, source)
            for rank, symbol in enumerate(symbols, start=1)
        ]
        with sqlite3.connect(self.db_path) as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO pick_history "
                "(run_date, strategy, symbol, rank, close, created_at, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                records,
            )
            conn.commit()
            added = conn.total_changes - before
        logger.info(f"留痕 {strategy} @ {run_date}：{len(records)} 只（新增 {added} 行）")
        return added

    # ── 读取 ──

    def _load_picks(self, since: str, strategy: str | None) -> pd.DataFrame:
        sql = (
            "SELECT run_date, strategy, symbol, rank, close, COALESCE(source,'live') AS source "
            "FROM pick_history WHERE run_date >= ?"
        )
        params: list = [since]
        if strategy:
            sql += " AND strategy = ?"
            params.append(strategy)
        with sqlite3.connect(self.db_path) as conn:
            return pd.read_sql(sql, conn, params=params)

    def _forward_returns(self, since: str) -> pd.DataFrame:
        """算出全部股票在 ``since`` 之后的 fwd_1 / fwd_5（T+1 开盘 → T+h 收盘）。"""
        with sqlite3.connect(self.db_path) as conn:
            prices = pd.read_sql(
                "SELECT symbol, date, open, close FROM stock_daily WHERE date >= ? "
                "ORDER BY symbol, date",
                conn,
                params=[since],
            )
        if prices.empty:
            return pd.DataFrame(columns=["symbol", "date"] + [f"fwd_{h}" for h in HORIZONS])

        grouped = prices.groupby("symbol", sort=False)
        next_open = grouped["open"].shift(-1)
        for h in HORIZONS:
            prices[f"fwd_{h}"] = grouped["close"].shift(-h) / next_open - 1.0
        return prices[["symbol", "date"] + [f"fwd_{h}" for h in HORIZONS]]

    # ── 报告 ──

    def report(self, days: int = 60, strategy: str | None = None) -> dict:
        """生成纸面账本报告。

        Args:
            days: 回看多少个自然日。
            strategy: 只看某个策略；None 表示全部。

        Returns:
            ``{"rows": [...], "recent": [...], "since": str, "cost": float}``；
            每行含次数、日均、h1/h5 的净收益与胜率、超额收益。
        """
        since = (datetime.now() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
        picks = self._load_picks(since, strategy)
        if picks.empty:
            return {"rows": [], "recent": [], "since": since, "cost": self.cost}

        fwd = self._forward_returns(since)
        merged = picks.merge(
            fwd.rename(columns={"date": "run_date"}), on=["symbol", "run_date"], how="left"
        )

        # 基准：同一天全市场等权收益的中位数
        market = fwd.groupby("date")[[f"fwd_{h}" for h in HORIZONS]].median()

        rows = []
        for key, group in merged.groupby("strategy"):
            item = {
                "strategy": key,
                "trades": int(len(group)),
                "days": int(group["run_date"].nunique()),
                "pending": int(group["fwd_1"].isna().sum()),
            }
            for h in HORIZONS:
                net = group[f"fwd_{h}"] - self.cost
                bench = group["run_date"].map(market[f"fwd_{h}"])
                valid = net.dropna()
                item[f"h{h}_mean"] = round(float(valid.mean()) * 100, 2) if len(valid) else None
                item[f"h{h}_win"] = (
                    round(float((valid > 0).mean()) * 100, 1) if len(valid) else None
                )
                excess = (net - bench).dropna()
                item[f"h{h}_exc"] = round(float(excess.mean()) * 100, 2) if len(excess) else None
            rows.append(item)
        rows.sort(key=lambda r: (r.get("h5_exc") is None, -(r.get("h5_exc") or 0)))

        recent = (
            merged.sort_values(["run_date", "strategy", "rank"], ascending=[False, True, True])
            .head(80)
            .assign(
                fwd_1=lambda d: (d["fwd_1"] - self.cost) * 100,
                fwd_5=lambda d: (d["fwd_5"] - self.cost) * 100,
            )
        )
        recent_rows = [
            {
                "run_date": r.run_date,
                "strategy": r.strategy,
                "source": r.source,
                "symbol": r.symbol,
                "rank": int(r.rank) if pd.notna(r.rank) else None,
                "close": None if pd.isna(r.close) else round(float(r.close), 2),
                "fwd_1": None if pd.isna(r.fwd_1) else round(float(r.fwd_1), 2),
                "fwd_5": None if pd.isna(r.fwd_5) else round(float(r.fwd_5), 2),
            }
            for r in recent.itertuples(index=False)
        ]

        return {"rows": rows, "recent": recent_rows, "since": since, "cost": self.cost}
