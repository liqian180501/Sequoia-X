"""组合构建：把"各策略各自选出的 N 只票"变成一个可执行的持仓。

为什么需要这一层
----------------
8 个策略各自输出 Top-N，互不知情。直接照着买会出现三个问题：

1. **重复下注**：同一只票被 3 个策略选中，实际仓位被动放大 3 倍；
2. **风险集中**：30 只可能全是同一个行业（或同一种风格），组合波动远高于单票波动；
3. **等权不合理**：高波动小盘股和低波动大盘股给一样的权重，等于让波动大的票主导盈亏。

这一层做的事：**去重 → 定权 → 约束**。它不改选股逻辑，只决定"买多少"。

权重方案
--------
默认 **波动率倒数**（inverse volatility）：``w_i ∝ 1/σ_i``。
这是最朴素也最稳健的风险平价近似——不需要估计收益率，只需要估计风险，
而波动率比收益率好估得多。

约束
----
* ``max_weight``：单票上限（默认 10%）；
* ``max_industry``：单行业上限（默认 25%），需要行业映射；
* 超额权重按"还有余量"的比例重新分配（water-filling），直到收敛。

行业数据缺失时只做单票约束，并给出告警——**不假装做了行业中性**。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

#: 估计波动率用的交易日数
VOL_WINDOW = 20

#: 年化系数（A 股一年约 244 个交易日）
ANNUALIZE = 244


@dataclass
class Position:
    """一个持仓。

    Attributes:
        symbol: 股票代码。
        weight: 组合权重（0~1，全部持仓加起来约等于 1）。
        vol: 年化波动率（小数）。
        strategies: 选中它的策略标识（多个说明是"共识票"）。
        consensus: 被几个策略选中。
    """

    symbol: str
    weight: float
    vol: float
    strategies: list[str] = field(default_factory=list)

    @property
    def consensus(self) -> int:
        return len(self.strategies)


def estimate_volatility(snap, symbols: list[str] | None = None) -> dict[str, float]:
    """用最近 ``VOL_WINDOW`` 个交易日的日收益标准差估年化波动率。

    Args:
        snap: ``MarketSnapshot``。
        symbols: 只算这些代码（None 表示全部）。

    Returns:
        ``{symbol: 年化波动率}``；数据不足的股票不出现在结果里。
    """
    df = snap.df
    if symbols is not None:
        df = df[df["symbol"].isin(set(symbols))]
    if df.empty:
        return {}

    # 取每只股票最后 VOL_WINDOW+1 根，避免对全历史做 groupby
    tail = df.groupby("symbol", sort=False).tail(VOL_WINDOW + 1)
    if tail.empty:
        return {}

    returns = tail.groupby("symbol", sort=False)["close"].pct_change()
    vol = returns.groupby(tail["symbol"], sort=False).std()
    # 样本不足（少于 5 个收益）不给估计，宁可不配
    counts = returns.groupby(tail["symbol"], sort=False).count()
    vol = vol[counts >= 5]

    return {
        str(symbol): float(value) * np.sqrt(ANNUALIZE)
        for symbol, value in vol.items()
        if value == value and value > 0  # 过滤 NaN 与 0
    }


def _water_fill(raw: dict[str, float], cap: float, total: float) -> dict[str, float]:
    """把权重按比例放大到 ``total``，同时保证每项不超过 ``cap``。

    超额部分按"还有余量"的项再分配，直到没有可分配的量（或全部顶到 cap）。

    Args:
        raw: 初始权重（未归一化）。
        cap: 单项上限。
        total: 目标总和。

    Returns:
        ``{key: weight}``。
    """
    if not raw:
        return {}

    keys = list(raw)
    weights = {k: 0.0 for k in keys}
    remaining = total
    active = set(keys)

    for _ in range(len(keys) * 3 + 3):
        if not active or remaining <= 1e-12:
            break
        base = sum(raw[k] for k in active)
        if base <= 0:
            break

        # 本轮按比例分配 remaining
        capped_now = []
        for k in active:
            proposed = remaining * raw[k] / base
            if proposed > cap:
                capped_now.append(k)

        if not capped_now:
            for k in active:
                weights[k] = remaining * raw[k] / base
            remaining = 0.0
            break

        for k in capped_now:
            weights[k] = cap
            remaining -= cap
            active.discard(k)

    # 兜底：还有剩余额度就按比例分给仍在池子里的（它们都还没触顶）
    if remaining > 1e-9 and active:
        base = sum(raw[k] for k in active)
        if base > 0:
            for k in active:
                weights[k] += remaining * raw[k] / base

    return weights


def build(
    picks: dict[str, list[str]],
    snap,
    *,
    max_weight: float = 0.10,
    max_industry: float | None = 0.25,
    industry: dict[str, str] | None = None,
    weighting: str = "inv_vol",
    total: float = 1.0,
) -> list[Position]:
    """把多策略选股结果合成一个带权重的组合。

    Args:
        picks: ``{策略标识: [代码...]}``，代码按策略内强度排序。
        snap: ``MarketSnapshot``（用来估波动率与取最新价）。
        max_weight: 单票权重上限。
        max_industry: 单行业权重上限；``industry`` 缺失时忽略。
        industry: ``{代码: 行业名}``。
        weighting: ``inv_vol``（默认）或 ``equal``。
        total: 目标总仓位（1.0=满仓）。

    Returns:
        按权重降序的持仓列表；无可用标的时返回空列表。
    """
    # ① 去重：同一只票被多个策略选中时合并，保留全部来源
    merged: dict[str, list[str]] = {}
    for key, symbols in picks.items():
        for symbol in symbols:
            merged.setdefault(symbol, []).append(key)
    if not merged:
        return []

    vol_map = estimate_volatility(snap, list(merged))

    # ② 初始权重
    raw: dict[str, float] = {}
    for symbol in merged:
        if weighting == "equal":
            raw[symbol] = 1.0
        else:
            vol = vol_map.get(symbol)
            if vol is None:
                # 估不出波动率就不配——宁可少买一只，也不要瞎给权重
                continue
            raw[symbol] = 1.0 / vol
    if not raw:
        logger.warning("没有任何股票的波动率可用，组合为空")
        return []

    weights = _water_fill(raw, cap=max_weight, total=total)

    # ③ 行业上限（如果提供了行业映射）
    if max_industry is not None and industry:
        weights = _apply_industry_cap(weights, industry, max_industry, total)
    elif max_industry is not None:
        logger.warning("没有行业映射，跳过行业上限（只做了单票上限）")

    positions = [
        Position(
            symbol=symbol,
            weight=round(weight, 4),
            vol=round(vol_map.get(symbol, float("nan")), 4),
            strategies=merged[symbol],
        )
        for symbol, weight in weights.items()
        if weight > 1e-6
    ]
    positions.sort(key=lambda p: (-p.weight, p.symbol))
    return positions


def _apply_industry_cap(
    weights: dict[str, float],
    industry: dict[str, str],
    cap: float,
    total: float,
) -> dict[str, float]:
    """把超限行业的权重整体压缩，压缩出来的额度按比例分给其它行业。"""
    by_industry: dict[str, list[str]] = {}
    for symbol, weight in weights.items():
        if weight <= 0:
            continue
        by_industry.setdefault(industry.get(symbol, "未知"), []).append(symbol)

    overflow = 0.0
    for group in by_industry.values():
        group_total = sum(weights[s] for s in group)
        if group_total > cap:
            scale = cap / group_total
            for symbol in group:
                overflow += weights[symbol] * (1 - scale)
                weights[symbol] *= scale

    if overflow <= 0:
        return weights

    # 把腾出来的额度分给还没到顶的行业
    room = {
        name: cap - sum(weights[s] for s in group)
        for name, group in by_industry.items()
        if cap - sum(weights[s] for s in group) > 0
    }
    base = sum(room.values())
    if base <= 0:
        logger.warning("行业上限过严，腾出的额度无法分配，组合仓位低于目标")
        return weights

    for name, group in by_industry.items():
        if name not in room:
            continue
        for symbol in group:
            weights[symbol] += overflow * room[name] / base
    return weights


def summarize(positions: list[Position]) -> dict:
    """组合概览：总仓位、持仓数、单票最大权重、行业集中度（若有权重）。"""
    if not positions:
        return {"positions": 0, "gross": 0.0, "max_weight": 0.0}
    return {
        "positions": len(positions),
        "gross": round(sum(p.weight for p in positions), 4),
        "max_weight": round(max(p.weight for p in positions), 4),
        "consensus": sum(1 for p in positions if p.consensus > 1),
    }
