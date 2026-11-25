"""资金流向存储与连续性检测。

设计要点
--------
* **口径可追溯**：每条记录带 ``source``，来源不同不覆盖（主键含 source），
  避免"代理指标"和"真实主力资金"混在一起算；
* **只存事实，不算结论**：表里存每日净额；"连续 3 日流入"这类判断放在检测函数里，
  规则改了不用重刷历史；
* **检测规则不依赖未来数据**：只用 T 日及之前的数据，可以直接拿去实盘用。

为什么不做成选股策略
--------------------
我们已经用 11 年数据验证过：任何信号塞进"每天选 30 只"都会被 0.2% 往返成本吃掉
（见 wiki 13 §8）。资金监控更适合当**提醒**（"这只票今天有大额异动"），
而不是"每天买一批"。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pandas as pd

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS capital_flow (
    symbol     TEXT NOT NULL,
    date       TEXT NOT NULL,
    net_main   REAL,           -- 主力净额（元，正=净流入）
    net_large  REAL,           -- 大单净额
    net_super  REAL,           -- 超大单净额
    turnover   REAL,           -- 当日成交额（元），用于算"流入占成交额比例"
    source     TEXT NOT NULL,  -- proxy / dragon_tiger / akshare
    PRIMARY KEY (symbol, date, source)
);
"""

#: 默认规则参数
DEFAULT_STREAK = 3
DEFAULT_MIN_NET = 50_000_000.0
DEFAULT_MIN_RATIO = 0.0


@dataclass
class Signal:
    """一条资金异动信号。

    Attributes:
        symbol: 股票代码。
        kind: ``inflow`` / ``outflow``。
        days: 连续天数。
        total_net: 连续期间的累计净额（元）。
        ratio: 累计净额 / 累计成交额。
        last_date: 最后一个交易日。
    """

    symbol: str
    kind: str
    days: int
    total_net: float
    ratio: float
    last_date: str


class CapitalFlowStore:
    """资金流向的读写。

    Args:
        db_path: SQLite 路径（与行情同库）。
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        with sqlite3.connect(db_path) as conn:
            conn.execute(CREATE_SQL)
            conn.commit()

    def upsert(self, df: pd.DataFrame, source: str) -> int:
        """写入一批资金流数据（幂等）。

        Args:
            df: 需含 ``symbol / date / net_main``，可选 ``net_large / net_super / turnover``。
            source: 数据来源标识。

        Returns:
            写入行数。
        """
        if df.empty:
            return 0
        frame = df.copy()
        for col in ("net_large", "net_super", "turnover"):
            if col not in frame:
                frame[col] = None
        records = [
            (
                row.symbol,
                row.date,
                float(row.net_main),
                None if pd.isna(row.net_large) else float(row.net_large),
                None if pd.isna(row.net_super) else float(row.net_super),
                None if pd.isna(row.turnover) else float(row.turnover),
                source,
            )
            for row in frame[
                ["symbol", "date", "net_main", "net_large", "net_super", "turnover"]
            ].itertuples(index=False)
        ]
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO capital_flow "
                "(symbol,date,net_main,net_large,net_super,turnover,source) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol,date,source) DO UPDATE SET "
                "net_main=excluded.net_main, net_large=excluded.net_large, "
                "net_super=excluded.net_super, turnover=excluded.turnover",
                records,
            )
            conn.commit()
        logger.info(f"资金流写入 {len(records)} 行（source={source}）")
        return len(records)

    def load(self, source: str, since: str | None = None) -> pd.DataFrame:
        """读出某个来源的资金流（按 symbol, date 升序）。"""
        sql = (
            "SELECT symbol, date, net_main, net_large, net_super, turnover "
            "FROM capital_flow WHERE source = ?"
        )
        params: list = [source]
        if since:
            sql += " AND date >= ?"
            params.append(since)
        with sqlite3.connect(self.db_path) as conn:
            return pd.read_sql(sql + " ORDER BY symbol, date", conn, params=params)


def detect(
    flows: pd.DataFrame,
    streak: int = DEFAULT_STREAK,
    min_net: float = DEFAULT_MIN_NET,
    min_ratio: float = DEFAULT_MIN_RATIO,
) -> list[Signal]:
    """找出"连续 N 日净流入/流出且累计金额足够大"的股票。

    规则（只用当日及之前的数据）：

    1. 连续 ``streak`` 天 ``net_main`` 同号；
    2. 累计净额绝对值 ≥ ``min_net``；
    3. 若给了 ``turnover``，累计净额 / 累计成交额 ≥ ``min_ratio``（跨股票可比）。

    Args:
        flows: ``CapitalFlowStore.load()`` 的结果。
        streak: 连续天数。
        min_net: 累计净额下限（元）。
        min_ratio: 累计净额占成交额的最小比例（0 表示不约束）。

    Returns:
        按 |累计净额| 降序的信号列表。
    """
    if flows.empty:
        return []

    out: list[Signal] = []
    for symbol, group in flows.groupby("symbol", sort=False):
        group = group.sort_values("date")
        net = group["net_main"].astype(float).to_numpy()
        dates = group["date"].tolist()
        if len(net) < streak:
            continue

        tail = net[-streak:]
        if not (tail > 0).all() and not (tail < 0).all():
            continue

        total = float(tail.sum())
        if abs(total) < min_net:
            continue

        turnover = group["turnover"].astype(float).to_numpy()[-streak:]
        amount = float(turnover.sum()) if not pd.isna(turnover).all() else 0.0
        ratio = abs(total) / amount if amount > 0 else 0.0
        if min_ratio and ratio < min_ratio:
            continue

        out.append(
            Signal(
                symbol=str(symbol),
                kind="inflow" if total > 0 else "outflow",
                days=streak,
                total_net=total,
                ratio=round(ratio, 4),
                last_date=dates[-1],
            )
        )

    out.sort(key=lambda s: -abs(s.total_net))
    return out
