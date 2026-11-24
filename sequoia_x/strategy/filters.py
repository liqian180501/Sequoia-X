"""选股通用过滤与打分工具。

为什么需要这一层
----------------
改造前每个策略只判断"形态条件"，完全不管这只票**能不能买、值不值得买**，于是：

* 选到涨停/一字板 —— 收盘封板根本买不进，信号不可执行；
* 选到 ST 股 —— 5% 涨跌幅下均线/突破形态全部失真，且有退市风险；
* 选到刚上市的次新股 —— 60 日均线只有 20 根数据，统计上没有意义；
* 选到日成交额几百万的票 —— 滑点远大于策略收益；
* 选到停牌/退市的票 —— 最后一根 K 线是几周前的，形态是"历史遗迹"。

这些不是策略逻辑问题，而是**可交易性/质量**问题，应该统一处理，而不是在每个
策略里重复写。本模块把它们做成一组向量化掩码，所有 K 线策略在判定形态前先过一遍。

打分（score）与 Top-N
---------------------
布尔筛选只能回答"符合/不符合"，无法排序。实践中需要按信号强度排序后取前 N 只
才可执行（一个推送里 200 多只股票没有意义）。因此每个策略都会给出一个 score，
用 ``MarketSnapshot.ranked`` 排序截断。
"""

from __future__ import annotations

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data.snapshot import MarketSnapshot
from sequoia_x.strategy.limits import limit_down_multiplier, limit_up_multiplier


def at_limit_up(snap: MarketSnapshot) -> pd.Series:
    """今日**收盘涨停**（含一字板）——正常无法买入。

    用板块感知的乘数（主板 9.95%、创业板/科创板 19.5%、北交所 29.5% 以上视为涨停），
    另有 ``high == low`` 的一字板判断（一字板无论涨跌都难以成交）。
    """
    prev_close = snap.shift("close", 1)
    close = snap.col("close")
    touch_limit = close >= prev_close * limit_up_multiplier(snap.col("symbol"))
    one_word = snap.col("high") <= snap.col("low") * 1.001
    return (touch_limit | one_word).fillna(False)


def at_limit_down(snap: MarketSnapshot) -> pd.Series:
    """今日**收盘跌停**（封死）——同样难以买入，且次日惯性下跌概率高。"""
    prev_close = snap.shift("close", 1)
    close = snap.col("close")
    return (close <= prev_close * limit_down_multiplier(snap.col("symbol"))).fillna(False)


def tradable_mask(
    snap: MarketSnapshot,
    settings: Settings,
    *,
    allow_limit_up: bool = False,
) -> pd.Series:
    """通用"可交易 + 质量"过滤掩码（全部条件同时满足才为 True）。

    包含：非 ST、非停牌/退市、上市足够久（窗口内 K 线数）、股价下限、
    成交额下限、成交额不低于 20 日均额的一半、非涨停（默认）。

    Args:
        snap: 行情快照。
        settings: 配置（阈值来自 ``Settings``）。
        allow_limit_up: 为 True 时不过滤涨停（用于"昨日涨停"这类需要涨停的策略，
            它们会在自己的逻辑里排除**今日**涨停）。

    Returns:
        与快照等长的 bool Series。
    """
    close = snap.col("close")

    # 注意：snap.* 返回的是**缓存对象**，这里必须用非原地运算（& 而不是 &=），
    # 否则会改写缓存，导致同一进程内第二次调用得到被污染的结果。
    mask = snap.enough_bars(settings.min_history_bars).copy()

    if settings.exclude_st:
        mask = mask & ~snap.flag("is_st")

    # 最后一根 K 线不是最新交易日 → 停牌/退市，形态是历史遗迹
    mask = mask & ~snap.flag("is_stale")

    mask = mask & (close >= settings.min_price)
    mask = mask & (snap.col("turnover") >= settings.min_turnover)
    # 相对量能：今日成交额不应显著低于自身近 20 日均值（避免"无人问津"）
    # 历史不足 20 根时均值为 NaN，此时不做相对判断（绝对成交额门槛已经生效）
    avg_turnover = snap.rolling("turnover", 20)
    mask = mask & (avg_turnover.isna() | (snap.col("turnover") >= avg_turnover * 0.5))

    if not allow_limit_up:
        mask = mask & ~at_limit_up(snap)

    return mask.fillna(False)


def volume_ratio(snap: MarketSnapshot) -> pd.Series:
    """今日成交量 / 前 20 日均量（不含当日）。<1 表示缩量，>1.5 表示明显放量。"""
    vol_ma20_prev = snap.rolling_shifted("volume", 1, 20)
    return (snap.col("volume") / vol_ma20_prev).replace([float("inf")], float("nan"))


def lower_shadow_ratio(snap: MarketSnapshot) -> pd.Series:
    """下影线长度 / 当日振幅，0~1。越大表示下方承接越强。"""
    high = snap.col("high")
    low = snap.col("low")
    close = snap.col("close")
    span = (high - low).replace(0, float("nan"))
    return (close - low) / span


def pct_change(snap: MarketSnapshot, periods: int) -> pd.Series:
    """N 个交易日涨跌幅（小数，如 0.12 表示 +12%）。"""
    prev = snap.shift("close", periods)
    return snap.col("close") / prev - 1.0
