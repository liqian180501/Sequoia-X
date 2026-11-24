"""定增破发监控的选股逻辑测试（纯 pandas，不联网）。"""

import datetime as dt

import pandas as pd
import pytest

from sequoia_x.core.config import Settings
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy


def make_settings(max_picks: int = 30) -> Settings:
    return Settings(
        db_path="data/unused.db",
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
        max_picks=max_picks,
    )


def make_strategy(**kwargs) -> PrivatePlacementStrategy:
    class _Engine:
        universe = None

    return PrivatePlacementStrategy(engine=_Engine(), settings=make_settings(**kwargs))


def make_frame(rows: list[dict]) -> pd.DataFrame:
    today = dt.date.today()
    base = {
        "股票代码": "600000",
        "股票简称": "测试股份",
        "发行方式": "定向增发",
        "发行价格": 10.0,
        "最新价": 10.0,
        "发行日期": today.isoformat(),
    }
    return pd.DataFrame([{**base, **row} for row in rows])


def test_empty_input() -> None:
    assert make_strategy()._select(None) == []
    assert make_strategy()._select(pd.DataFrame()) == []


def test_missing_columns_is_rejected() -> None:
    """akshare 改接口（缺列）时应返回空，而不是 KeyError 崩溃。"""
    assert make_strategy()._select(pd.DataFrame({"股票代码": ["600000"]})) == []


def test_only_private_placement_is_kept() -> None:
    frame = make_frame(
        [
            {"股票代码": "600001", "发行方式": "定向增发"},
            {"股票代码": "600002", "发行方式": "公开增发"},
        ]
    )
    assert make_strategy()._select(frame) == ["600001"]


def test_broken_issue_ranks_first() -> None:
    """现价/发行价 越低（越破发）越靠前。"""
    frame = make_frame(
        [
            {"股票代码": "600001", "最新价": 11.0},  # 溢价 10%
            {"股票代码": "600002", "最新价": 8.0},  # 破发 20%
            {"股票代码": "600003", "最新价": 9.5},  # 破发 5%
        ]
    )
    assert make_strategy()._select(frame) == ["600002", "600003", "600001"]


def test_far_above_issue_price_is_excluded() -> None:
    """现价远高于发行价（+30%）不属于关注范围。"""
    frame = make_frame([{"股票代码": "600001", "最新价": 13.0}])
    assert make_strategy()._select(frame) == []


def test_st_is_excluded() -> None:
    frame = make_frame(
        [
            {"股票代码": "600001", "股票简称": "*ST 测试", "最新价": 9.0},
            {"股票代码": "600002", "股票简称": "正常股份", "最新价": 9.0},
        ]
    )
    assert make_strategy()._select(frame) == ["600002"]


def test_old_issue_is_excluded() -> None:
    """超过 30 天的定增不再关注。"""
    old = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    frame = make_frame([{"股票代码": "600001", "发行日期": old, "最新价": 9.0}])
    assert make_strategy()._select(frame) == []


def test_deduplicates_same_symbol() -> None:
    recent = (dt.date.today() - dt.timedelta(days=5)).isoformat()
    older = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    frame = make_frame(
        [
            {"股票代码": "600001", "发行日期": recent, "最新价": 9.0},
            {"股票代码": "600001", "发行日期": older, "最新价": 9.5},
        ]
    )
    result = make_strategy()._select(frame)
    assert result == ["600001"]


def test_max_picks_is_respected() -> None:
    frame = make_frame([{"股票代码": f"60000{i}", "最新价": 9.0} for i in range(5)])
    assert len(make_strategy(max_picks=3)._select(frame)) == 3


def test_invalid_prices_are_dropped() -> None:
    frame = make_frame(
        [
            {"股票代码": "600001", "发行价格": 0.0, "最新价": 9.0},
            {"股票代码": "600002", "最新价": None},
            {"股票代码": "600003", "最新价": 9.0},
        ]
    )
    assert make_strategy()._select(frame) == ["600003"]


def test_short_name_extraction_handles_prefix() -> None:
    """代码列带市场前缀时也能提取 6 位数字。"""
    frame = make_frame([{"股票代码": "SH600001", "最新价": 9.0}])
    assert make_strategy()._select(frame) == ["600001"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
