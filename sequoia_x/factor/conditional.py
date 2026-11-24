"""条件评估：把"策略的入选条件"当成一个 0/1 因子来检验。

要回答的问题
------------
因子层发现 2024–2026 年动量因子 IC 显著为负（越强势越跑输），但策略回测又显示
多数策略 h5 超额为正。到底谁对？只有一种办法能判：

> 把"策略选中的票"与"同期没被选中的票"放在同一个横截面上比。

如果入选票确实优于非入选票（spread 显著为正），说明**过滤器**把因子最差的那一档
剔掉了，策略结论可信；如果 spread ≈ 0 或为负，说明策略没有增量信息。

指标
----
* ``pick_mean`` / ``nonpick_mean``：入选/未入选票的平均未来收益；
* ``spread`` = ``pick_mean − all_mean``：**同日全市场均值**为基准，避免用大盘涨跌充数；
* ``t`` = ``mean(daily_spread) / std(daily_spread) × √days``；
* ``ic``：0/1 指示变量与未来收益的横截面相关（point-biserial）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from sequoia_x.factor.evaluate import cross_sectional_ic


def conditional_stats(
    cond: pd.Series,
    forward: pd.Series,
    dates: pd.Series,
    min_picks: int = 3,
    universe: pd.Series | None = None,
) -> dict:
    """逐日比较"入选 vs 未入选 vs 全市场"的未来收益。

    Args:
        cond: 布尔条件（True=入选）。
        forward: 未来收益（与 ``cond`` 等长）。
        dates: 日期列。
        min_picks: 当日入选数低于此值就跳过（样本太少没有统计意义）。
        universe: 可选的可交易池掩码。**强烈建议传**——否则"未入选"里会混进
            ST、低流动性、涨停等本来就不该买的票，基准被污染，价差会失真。

    Returns:
        含 ``days / picks_per_day / pick_mean / nonpick_mean / spread / spread_t / win_rate``
        的字典（收益单位为 %）。
    """
    frame = pd.DataFrame(
        {
            "date": pd.Series(dates).to_numpy(),
            "pick": pd.Series(cond).fillna(False).to_numpy(dtype=bool),
            "fwd": pd.Series(forward).to_numpy(),
        }
    )

    # 先按可交易池过滤，再丢掉没有未来收益的行（顺序不能反，否则掩码长度对不上）
    if universe is not None:
        frame = frame[pd.Series(universe).fillna(False).to_numpy(dtype=bool)]
    frame = frame.dropna(subset=["fwd"])
    if frame.empty:
        return {"days": 0}

    picks = frame[frame["pick"]]
    nonpicks = frame[~frame["pick"]]
    stats = pd.DataFrame(
        {
            "n_pick": picks.groupby("date")["fwd"].size(),
            "pick_mean": picks.groupby("date")["fwd"].mean(),
            "nonpick_mean": nonpicks.groupby("date")["fwd"].mean(),
            "all_mean": frame.groupby("date")["fwd"].mean(),
        }
    )
    stats = stats[stats["n_pick"] >= min_picks].dropna()
    if stats.empty:
        return {"days": 0}

    stats["spread"] = stats["pick_mean"] - stats["all_mean"]
    n = len(stats)
    spread_mean = float(stats["spread"].mean())
    spread_std = float(stats["spread"].std(ddof=1)) if n > 1 else float("nan")
    t_stat = (
        spread_mean / spread_std * np.sqrt(n)
        if spread_std and spread_std == spread_std
        else float("nan")
    )

    return {
        "days": n,
        "picks_per_day": round(float(stats["n_pick"].mean()), 1),
        "pick_mean": round(float(stats["pick_mean"].mean()) * 100, 2),
        "nonpick_mean": round(float(stats["nonpick_mean"].mean()) * 100, 2),
        "spread": round(spread_mean * 100, 2),
        "spread_t": round(t_stat, 2) if t_stat == t_stat else None,
        "win_rate": round(float((stats["spread"] > 0).mean()) * 100, 1),
    }


def binary_ic(
    cond: pd.Series,
    forward: pd.Series,
    dates: pd.Series,
    min_names: int = 50,
) -> pd.Series:
    """0/1 指示变量与未来收益的逐日横截面相关。

    Args:
        cond: 布尔条件。
        forward: 未来收益。
        dates: 日期列。
        min_names: 当日最少样本数。

    Returns:
        逐日 IC 的 Series。
    """
    indicator = pd.Series(cond).fillna(False).astype(float)
    return cross_sectional_ic(indicator, forward, dates, min_names=min_names)
