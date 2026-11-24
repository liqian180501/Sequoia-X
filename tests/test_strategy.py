"""策略属性测试：run() 必须返回合法的股票代码列表。

这些用例全部离线：行情用合成快照，引擎用 mock，不碰数据库与网络。
"""

from unittest.mock import MagicMock

import pandas as pd
import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data.snapshot import MarketSnapshot
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.reversal_bounce import ReversalBounceStrategy
from sequoia_x.strategy.trend_pullback import TrendPullbackStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy

SNAPSHOT_COLUMNS = [
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
]

ALL_STRATEGIES = [
    MaVolumeStrategy,
    TurtleTradeStrategy,
    HighTightFlagStrategy,
    LimitUpShakeoutStrategy,
    UptrendLimitDownStrategy,
    TrendPullbackStrategy,
    ReversalBounceStrategy,
]


def make_settings() -> Settings:
    return Settings(
        db_path="data/unused.db",
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
    )


def make_snapshot(symbols: list[str], bars: int = 130) -> MarketSnapshot:
    """构造一个每只股票都有 ``bars`` 根 K 线的合成快照（价格单调上行）。"""
    rows = []
    for symbol in symbols:
        for i in range(bars):
            base = 10.0 + i * 0.1
            rows.append(
                {
                    "symbol": symbol,
                    "date": f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                    "open": base * 0.99,
                    "high": base * 1.02,
                    "low": base * 0.98,
                    "close": base,
                    "volume": 2_000_000.0 + i,
                    "turnover": 150_000_000.0 + i,
                }
            )
    return MarketSnapshot(pd.DataFrame(rows, columns=SNAPSHOT_COLUMNS), days=130)


def make_engine(snapshot: MarketSnapshot) -> MagicMock:
    """策略只依赖 engine.get_snapshot() 与 engine.universe。"""
    engine = MagicMock()
    engine.get_snapshot.return_value = snapshot
    engine.universe = None
    return engine


@pytest.mark.parametrize("strategy_cls", ALL_STRATEGIES)
@given(
    symbols=st.lists(
        st.text(min_size=6, max_size=6, alphabet="0123456789"),
        min_size=0,
        max_size=3,
        unique=True,
    )
)
@h_settings(max_examples=20, deadline=None)
def test_strategy_run_returns_valid_symbols(strategy_cls, symbols: list[str]) -> None:
    """run() 返回 list[str]；元素非空、不重复，且必须是快照里的股票。"""
    engine = make_engine(make_snapshot(symbols))

    result = strategy_cls(engine=engine, settings=make_settings()).run()

    assert isinstance(result, list)
    assert all(isinstance(s, str) and len(s) > 0 for s in result)
    assert set(result) <= set(symbols)
    assert len(result) == len(set(result)), "不应出现重复代码"


@pytest.mark.parametrize("strategy_cls", ALL_STRATEGIES)
def test_empty_snapshot_returns_empty_list(strategy_cls) -> None:
    """行情为空（例如首次运行未回填）时策略应返回空列表而不是抛异常。"""
    engine = make_engine(MarketSnapshot(pd.DataFrame(columns=SNAPSHOT_COLUMNS), days=130))

    assert strategy_cls(engine=engine, settings=make_settings()).run() == []


@pytest.mark.parametrize("strategy_cls", ALL_STRATEGIES)
def test_short_history_is_filtered(strategy_cls) -> None:
    """K 线不足的股票不应被选中（次新/停牌保护）。"""
    settings = make_settings()
    snapshot = make_snapshot(["000001"], bars=settings.min_history_bars - 1)

    result = strategy_cls(engine=make_engine(snapshot), settings=settings).run()
    assert result == []


@pytest.mark.parametrize("strategy_cls", ALL_STRATEGIES)
def test_max_picks_is_respected(strategy_cls) -> None:
    """输出数量不得超过 max_picks。"""
    settings = make_settings()
    settings.max_picks = 2
    symbols = [f"{i:06d}" for i in range(10)]
    snapshot = make_snapshot(symbols)

    result = strategy_cls(engine=make_engine(snapshot), settings=settings).run()
    assert len(result) <= 2
