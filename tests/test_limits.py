"""涨跌停乘数与策略注册表的单元测试。"""

import pandas as pd
import pytest

from sequoia_x.strategy.limits import (
    limit_down_multiplier,
    limit_ratio,
    limit_up_multiplier,
)
from sequoia_x.strategy.registry import (
    STRATEGIES,
    all_strategy_ids,
    resolve_strategies,
)

# ── limits ──


@pytest.mark.parametrize(
    ("symbol", "ratio"),
    [
        ("600000", 0.10),  # 上交所主板
        ("000001", 0.10),  # 深交所主板
        ("300750", 0.20),  # 创业板
        ("301001", 0.20),  # 创业板
        ("688001", 0.20),  # 科创板
        ("430047", 0.30),  # 北交所
        ("830799", 0.30),  # 北交所
    ],
)
def test_limit_ratio_by_board(symbol: str, ratio: float) -> None:
    assert limit_ratio(symbol) == pytest.approx(ratio)


def test_main_board_multipliers_match_legacy() -> None:
    """主板乘数必须与旧实现完全一致（1.095 / 0.905），不能悄悄改变行为。"""
    symbols = pd.Series(["600000", "000001"])
    assert limit_up_multiplier(symbols).tolist() == pytest.approx([1.095, 1.095])
    assert limit_down_multiplier(symbols).tolist() == pytest.approx([0.905, 0.905])


def test_gem_and_star_multipliers() -> None:
    """创业板/科创板应按 20% 判定：涨 10% 不算涨停，跌 10% 不算跌停。"""
    symbols = pd.Series(["300750", "688001"])
    assert limit_up_multiplier(symbols).tolist() == pytest.approx([1.195, 1.195])
    assert limit_down_multiplier(symbols).tolist() == pytest.approx([0.805, 0.805])


def test_bse_multipliers() -> None:
    """北交所按 30% 判定。"""
    symbols = pd.Series(["430047", "871981"])
    assert limit_up_multiplier(symbols).tolist() == pytest.approx([1.295, 1.295])
    assert limit_down_multiplier(symbols).tolist() == pytest.approx([0.705, 0.705])


def test_gem_10pct_day_is_not_limit_up() -> None:
    """同样是"+10%"，主板算涨停、创业板不算——这正是本次修正的问题。"""
    symbols = pd.Series(["600000", "300750"])
    prev_close = pd.Series([10.0, 10.0])
    today_close = pd.Series([11.0, 11.0])  # 都是 +10%

    is_limit_up = today_close >= prev_close * limit_up_multiplier(symbols)
    assert is_limit_up.tolist() == [True, False]


# ── registry ──


def test_strategy_ids_match_webhook_keys() -> None:
    """注册表的键必须与策略的 webhook_key 一致（否则选择与路由会对不上）。"""
    for key, cls in STRATEGIES.items():
        assert cls.webhook_key == key


def test_resolve_all() -> None:
    assert resolve_strategies(None) == list(STRATEGIES.values())
    assert resolve_strategies("all") == list(STRATEGIES.values())
    assert resolve_strategies("*") == list(STRATEGIES.values())


def test_resolve_subset_preserves_default_order() -> None:
    """按默认执行顺序返回，而不是命令行里的书写顺序。"""
    picked = resolve_strategies("rps,ma_volume")
    assert [cls.webhook_key for cls in picked] == ["ma_volume", "rps"]


def test_resolve_unknown_raises_with_hint() -> None:
    with pytest.raises(ValueError, match="可用"):
        resolve_strategies("ma_volume,nope")


def test_all_strategy_ids_non_empty() -> None:
    ids = all_strategy_ids()
    assert "ma_volume" in ids
    assert len(ids) == len(STRATEGIES)
