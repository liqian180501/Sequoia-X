"""因子库：把"形态规则"拆成可单独度量的因子。

为什么要有这一层
----------------
原来的 8 个策略是"一堆阈值 + 一个形态"，只能整体看收益，回答不了：

* 20 日动量在这套规则里贡献了多少？
* "距一年新高 5% 以内"这条到底有没有用？
* 某个策略赚的钱是来自动量，还是来自小市值/低波动？

因子化的价值是：**每个因子可以单独度量 IC（信息系数）与分层收益**，
于是"猜阈值"变成"选因子"。

约定
----
* 所有因子都是**逐行**计算的：输入 ``MarketSnapshot``，输出与 ``snap.df`` 等长的 Series；
* 因子值本身不做横截面标准化——标准化在评估/合成时按日做（见 ``evaluate.py``）；
* 因子方向不预先假定：IC 的正负直接告诉你方向（本文件不做 ``-1`` 翻转）。
"""

from __future__ import annotations

import pandas as pd

from sequoia_x.data.snapshot import MarketSnapshot
from sequoia_x.strategy.filters import lower_shadow_ratio, pct_change, volume_ratio


def mom(snap: MarketSnapshot, window: int) -> pd.Series:
    """N 日动量（累计涨幅）。"""
    return pct_change(snap, window)


def near_high(snap: MarketSnapshot, window: int = 250) -> pd.Series:
    """现价 / N 日最高价（1.0 表示正好在新高）。"""
    high = snap.rolling("high", window, "max")
    return snap.col("close") / high


def volatility(snap: MarketSnapshot, window: int = 20) -> pd.Series:
    """N 日收益波动率（日频标准差，未年化）。

    用平方和均值减均值平方的恒等式，避免对全历史做 groupby.
    """
    close = snap.col("close")
    returns = close.groupby(snap.df["symbol"]).pct_change()
    mean = snap.rolling_series(returns, window, "mean")
    sq_mean = snap.rolling_series(returns * returns, window, "mean")
    return (sq_mean - mean * mean).clip(lower=0.0) ** 0.5


def turnover_z(snap: MarketSnapshot, window: int = 60) -> pd.Series:
    """换手率（成交额）相对自身 N 日均值的偏离（z 分数）。"""
    turnover = snap.col("turnover")
    mean = snap.rolling("turnover", window, "mean")
    std = snap.rolling("turnover", window, "std")
    return (turnover - mean) / std.replace(0.0, pd.NA)


def ma_alignment(snap: MarketSnapshot) -> pd.Series:
    """均线多头程度：``(ma5-ma20)/ma20 + (ma20-ma60)/ma60``。"""
    ma5 = snap.rolling("close", 5)
    ma20 = snap.rolling("close", 20)
    ma60 = snap.rolling("close", 60)
    return (ma5 - ma20) / ma20 + (ma20 - ma60) / ma60


def position_in_range(snap: MarketSnapshot, window: int = 60) -> pd.Series:
    """收盘价在 N 日高低区间中的位置（0=最低，1=最高）。"""
    high = snap.rolling("high", window, "max")
    low = snap.rolling("low", window, "min")
    span = (high - low).replace(0.0, pd.NA)
    return (snap.col("close") - low) / span


def lower_shadow(snap: MarketSnapshot) -> pd.Series:
    """下影线占振幅比例（承接力度）。"""
    return lower_shadow_ratio(snap)


#: 因子登记表：名字 → 计算函数。新增因子只要加一行。
FACTORS: dict[str, callable] = {
    "mom_5": lambda snap: mom(snap, 5),
    "mom_20": lambda snap: mom(snap, 20),
    "mom_60": lambda snap: mom(snap, 60),
    "mom_120": lambda snap: mom(snap, 120),
    "near_high_250": lambda snap: near_high(snap, 250),
    "vol_20": lambda snap: volatility(snap, 20),
    "volume_ratio": lambda snap: volume_ratio(snap),
    "turnover_z_60": lambda snap: turnover_z(snap, 60),
    "ma_alignment": lambda snap: ma_alignment(snap),
    "position_60": lambda snap: position_in_range(snap, 60),
    "lower_shadow": lambda snap: lower_shadow(snap),
}
