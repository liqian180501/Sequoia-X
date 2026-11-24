"""组合构建的测试：去重、单票上限、行业上限、波动率加权。"""

import numpy as np
import pandas as pd
import pytest

from sequoia_x.data.snapshot import MarketSnapshot
from sequoia_x.portfolio.builder import Position, build, estimate_volatility, summarize

COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]


def make_snapshot(volatility: dict[str, float], days: int = 25) -> MarketSnapshot:
    """按给定日波动率合成行情（``volatility`` 是日收益标准差）。"""
    rng = np.random.default_rng(42)
    rows = []
    dates = [f"2024-01-{d:02d}" for d in range(1, days + 1)]
    for symbol, sigma in volatility.items():
        price = 10.0
        for date in dates:
            price *= 1 + rng.normal(0, sigma)
            price = max(price, 1.0)
            rows.append(
                {
                    "symbol": symbol,
                    "date": date,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 1e6,
                    "turnover": 1e8,
                }
            )
    return MarketSnapshot(pd.DataFrame(rows, columns=COLUMNS), days=days)


# ── 波动率估计 ──


def test_estimate_volatility_orders_by_risk() -> None:
    snap = make_snapshot({"600000": 0.01, "000001": 0.05})
    vol = estimate_volatility(snap)

    assert set(vol) == {"600000", "000001"}
    assert vol["000001"] > vol["600000"]


def test_estimate_volatility_skips_short_history() -> None:
    snap = make_snapshot({"600000": 0.02}, days=3)  # 只有 3 天，估不出来
    assert estimate_volatility(snap) == {}


# ── 去重与权重 ──


def test_same_symbol_from_two_strategies_is_merged() -> None:
    snap = make_snapshot({"600000": 0.02, "000001": 0.02})
    positions = build({"turtle": ["600000"], "rps": ["600000", "000001"]}, snap)

    by_symbol = {p.symbol: p for p in positions}
    assert by_symbol["600000"].consensus == 2
    assert sorted(by_symbol["600000"].strategies) == ["rps", "turtle"]
    assert by_symbol["000001"].consensus == 1


def test_weights_sum_to_total() -> None:
    snap = make_snapshot({f"60000{i}": 0.02 for i in range(5)})
    picks = {"turtle": [f"60000{i}" for i in range(5)]}

    positions = build(picks, snap, max_weight=0.5)
    assert sum(p.weight for p in positions) == pytest.approx(1.0, abs=1e-6)


def test_max_weight_is_respected() -> None:
    """单票上限 10% 时，没有任何一只超过 10%，且总仓位仍然接近满仓。"""
    snap = make_snapshot({f"60000{i}": 0.02 for i in range(20)})
    picks = {"turtle": [f"60000{i}" for i in range(20)]}

    positions = build(picks, snap, max_weight=0.10)

    assert max(p.weight for p in positions) <= 0.10 + 1e-9
    # 权重保留 4 位小数，20 只票的累计舍入误差可达 1e-3
    assert sum(p.weight for p in positions) == pytest.approx(1.0, abs=2e-3)


def test_too_few_names_cannot_reach_full_position() -> None:
    """只有 3 只票、单票上限 10% —— 最多只能建到 30%，不能凭空放大。"""
    snap = make_snapshot({"600000": 0.02, "000001": 0.02, "000002": 0.02})
    positions = build({"turtle": ["600000", "000001", "000002"]}, snap, max_weight=0.10)

    assert sum(p.weight for p in positions) == pytest.approx(0.30, abs=1e-6)


def test_inverse_vol_gives_lower_weight_to_risky_stock() -> None:
    snap = make_snapshot({"600000": 0.01, "000001": 0.06})
    positions = build({"turtle": ["600000", "000001"]}, snap, max_weight=0.9)

    by_symbol = {p.symbol: p for p in positions}
    assert by_symbol["600000"].weight > by_symbol["000001"].weight


def test_equal_weighting_option() -> None:
    snap = make_snapshot({"600000": 0.01, "000001": 0.06})
    positions = build(
        {"turtle": ["600000", "000001"]}, snap, max_weight=0.9, weighting="equal"
    )
    assert positions[0].weight == pytest.approx(positions[1].weight)


# ── 行业上限 ──


def test_industry_cap_limits_concentration() -> None:
    codes = [f"60000{i}" for i in range(4)]
    snap = make_snapshot({c: 0.02 for c in codes})
    industry = {codes[0]: "银行", codes[1]: "银行", codes[2]: "银行", codes[3]: "医药"}

    positions = build(
        {"turtle": codes}, snap, max_weight=0.6, max_industry=0.30, industry=industry
    )
    by_symbol = {p.symbol: p for p in positions}
    bank = sum(by_symbol[c].weight for c in codes[:3])

    assert bank <= 0.30 + 1e-9
    # 腾出来的额度分给了医药，总仓位不变
    assert sum(p.weight for p in positions) == pytest.approx(1.0, abs=2e-3)


def test_industry_cap_skipped_without_map() -> None:
    """没有行业映射时只做单票上限，不能假装做了行业中性。"""
    snap = make_snapshot({"600000": 0.02, "000001": 0.02})
    positions = build({"turtle": ["600000", "000001"]}, snap, max_weight=0.9)
    assert len(positions) == 2


# ── 边界 ──


def test_empty_picks() -> None:
    snap = make_snapshot({"600000": 0.02})
    assert build({}, snap) == []
    assert build({"turtle": []}, snap) == []


def test_unknown_symbol_is_skipped() -> None:
    """行情里没有的代码估不出波动率，直接不配。"""
    snap = make_snapshot({"600000": 0.02})
    positions = build({"turtle": ["600000", "999999"]}, snap)
    assert [p.symbol for p in positions] == ["600000"]


def test_positions_sorted_by_weight() -> None:
    snap = make_snapshot({"600000": 0.01, "000001": 0.05, "000002": 0.03})
    positions = build({"turtle": ["600000", "000001", "000002"]}, snap, max_weight=0.5)
    assert [p.symbol for p in positions] == ["600000", "000002", "000001"]


def test_summarize() -> None:
    positions = [
        Position("600000", 0.12, 0.3, ["turtle", "rps"]),
        Position("000001", 0.08, 0.4, ["rps"]),
    ]
    info = summarize(positions)
    assert info == {"positions": 2, "gross": 0.2, "max_weight": 0.12, "consensus": 1}
    assert summarize([])["positions"] == 0
