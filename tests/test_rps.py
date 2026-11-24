"""RPS 选股逻辑测试（多周期排名 + 新高判定，纯 pandas）。"""

import pandas as pd
import pytest

from sequoia_x.core.config import Settings
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy


class _Engine:
    universe = None


def make_settings(max_picks: int = 30) -> Settings:
    return Settings(
        db_path="data/unused.db",
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
        max_picks=max_picks,
    )


def make_strategy(**kwargs) -> RpsBreakoutStrategy:
    return RpsBreakoutStrategy(engine=_Engine(), settings=make_settings(**kwargs))


def agg(rows: list[dict]) -> pd.DataFrame:
    base = {
        "close_now": 100.0,
        "close_short": 80.0,
        "close_long": 50.0,
        "high_long": 101.0,
        "bars_long": 250,
    }
    return pd.DataFrame([{**base, **row} for row in rows])


def test_empty_returns_empty() -> None:
    assert make_strategy()._select(pd.DataFrame()) == []


def test_rows_without_full_history_are_dropped() -> None:
    frame = agg([{"symbol": "600000", "close_long": None}])
    assert make_strategy()._select(frame) == []


def test_near_high_is_required() -> None:
    """「距一年新高 5% 以内」是硬条件：跌到 -20% 的票即使 RS 高也不选。"""
    frame = agg(
        [
            {"symbol": "600001", "high_long": 100.0, "close_now": 99.0},
            {"symbol": "600002", "high_long": 100.0, "close_now": 80.0},
        ]
    )
    assert "600002" not in make_strategy()._select(frame)


def test_positive_long_term_is_required() -> None:
    """一年收益为负的票不选（哪怕近期反弹到高位附近）。"""
    frame = agg([{"symbol": "600001", "close_now": 90.0, "close_long": 95.0, "high_long": 91.0}])
    assert make_strategy()._select(frame) == []


def test_ranks_by_composite_strength() -> None:
    """更强（涨幅更大且更贴近新高）的票应排在前面。"""
    frame = agg(
        [
            {"symbol": "600001", "close_now": 120.0, "close_short": 100.0, "high_long": 120.0},
            {"symbol": "600002", "close_now": 110.0, "close_short": 105.0, "high_long": 120.0},
            {"symbol": "600003", "close_now": 130.0, "close_short": 100.0, "high_long": 130.0},
        ]
    )
    result = make_strategy()._select(frame)
    assert result[0] == "600003"


def test_max_picks_is_respected() -> None:
    strategy = make_strategy(max_picks=2)
    strategy.rps_threshold = 0  # 只测截断，不放阈值干扰
    frame = agg(
        [{"symbol": f"60000{i}", "close_now": 100.0 + i, "high_long": 101.0} for i in range(6)]
    )
    assert len(strategy._select(frame)) == 2


def test_threshold_can_be_loosened() -> None:
    """把阈值降到 0 时，所有满足新高与正收益的票都应入选。"""
    strategy = make_strategy(max_picks=0)
    strategy.rps_threshold = 0
    frame = agg(
        [
            {"symbol": "600001", "close_now": 100.0, "high_long": 100.0},
            {"symbol": "600002", "close_now": 101.0, "high_long": 101.0},
        ]
    )
    assert len(strategy._select(frame)) == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
