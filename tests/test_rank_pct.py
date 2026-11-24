"""横截面排名（``MarketSnapshot.rank_pct``）的测试。

这个函数是"回测与线上一致"的关键：策略打分必须在**同一交易日的横截面内**排名，
否则回测用全历史快照排名、线上只用最后一天排名，两边分数不同 → Top-N 选出不同股票。
"""

import pandas as pd
import pytest

from sequoia_x.data.snapshot import MarketSnapshot

COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]


def frame() -> pd.DataFrame:
    """两天、三天、四只股票，收益差异明显。"""
    rows = []
    for date, closes in (
        ("2024-01-02", [10.0, 20.0, 30.0]),
        ("2024-01-03", [100.0, 200.0, 300.0]),  # 数值放大 10 倍，但横截面排序不变
    ):
        for symbol, close in zip(("600000", "000001", "000002"), closes, strict=False):
            rows.append(
                {
                    "symbol": symbol,
                    "date": date,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1e6,
                    "turnover": 1e8,
                }
            )
    return pd.DataFrame(rows, columns=COLUMNS)


def test_rank_pct_is_cross_sectional() -> None:
    """同一只股票在两天的分位应当相同（横截面内的相对位置没变）。"""
    snap = MarketSnapshot(frame(), days=2)
    ranked = snap.rank_pct(snap.col("close"))

    small = ranked[snap.df["date"] == "2024-01-02"].tolist()
    large = ranked[snap.df["date"] == "2024-01-03"].tolist()
    assert small == pytest.approx(large)
    assert small == pytest.approx([1 / 3, 2 / 3, 1.0])


def test_rank_pct_differs_from_global_rank() -> None:
    """全局排名会被不同日期的量级差异带偏——这正是要避免的行为。"""
    snap = MarketSnapshot(frame(), days=2)
    close = snap.col("close")

    cross = snap.rank_pct(close)
    global_rank = close.rank(pct=True)

    # 第二天的价格是十倍的，全局排名会把它们全排到前面
    day2 = snap.df["date"] == "2024-01-03"
    assert (global_rank[day2] > 0.5).all()
    # 而横截面排名里，第二天最小的一只仍然是 1/3
    assert cross[day2].min() == pytest.approx(1 / 3)


def test_rank_pct_keeps_nan() -> None:
    """缺失值不参与排名，也不产生分位。"""
    data = frame()
    data.loc[0, "close"] = None
    snap = MarketSnapshot(data, days=2)

    ranked = snap.rank_pct(snap.col("close"))
    assert pd.isna(ranked.iloc[0])
