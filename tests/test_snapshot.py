"""MarketSnapshot 属性测试：分组向量化结果必须与 pandas 分组运算完全一致。

这里用 ``groupby.transform`` 作为"参考实现"（它慢但语义直观），
逐元素比对 ``MarketSnapshot`` 的滚动/移位结果，确保优化没有改变语义。
"""

import numpy as np
import pandas as pd
import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from sequoia_x.data.snapshot import MarketSnapshot

COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]


def build_frame(spec: list[tuple[str, int]]) -> pd.DataFrame:
    """按 (symbol, bars) 规格构造规整的行情 DataFrame。"""
    rows = []
    for symbol, bars in spec:
        for i in range(bars):
            rows.append(
                {
                    "symbol": symbol,
                    "date": f"2024-01-{i + 1:02d}",
                    "open": 10.0 + i,
                    "high": 12.0 + i,
                    "low": 9.0 + i,
                    "close": 10.5 + i,
                    "volume": 1000.0 + 3 * i,
                    "turnover": 1e7 + i,
                }
            )
    return pd.DataFrame(rows, columns=COLUMNS)


@st.composite
def frames(draw):
    """随机生成 1~3 只股票、每只 1~30 根 K 线。"""
    n = draw(st.integers(min_value=1, max_value=3))
    spec = [(f"{i:06d}", draw(st.integers(min_value=1, max_value=30))) for i in range(n)]
    return build_frame(spec)


def assert_series_equal(actual: pd.Series, expected: pd.Series) -> None:
    """逐元素比对，NaN 位置必须也一致。"""
    a = actual.to_numpy(dtype=float)
    e = expected.to_numpy(dtype=float)
    assert np.array_equal(np.isnan(a), np.isnan(e)), "NaN 位置不一致"
    np.testing.assert_allclose(a[~np.isnan(a)], e[~np.isnan(e)], rtol=1e-12)


@pytest.mark.parametrize("how", ["mean", "max", "min"])
@given(df=frames(), window=st.integers(min_value=1, max_value=5))
@h_settings(max_examples=50, deadline=None)
def test_rolling_matches_groupby(df: pd.DataFrame, window: int, how: str) -> None:
    """rolling() 必须等于 groupby(...).transform(rolling(...))。"""
    snap = MarketSnapshot(df, days=130)
    expected = df.groupby("symbol")["close"].transform(lambda s: getattr(s.rolling(window), how)())
    assert_series_equal(snap.rolling("close", window, how), expected)


@given(df=frames(), periods=st.integers(min_value=1, max_value=3))
@h_settings(max_examples=50, deadline=None)
def test_shift_matches_groupby(df: pd.DataFrame, periods: int) -> None:
    """shift() 必须等于 groupby(...).shift(n)，且不跨股票串数据。"""
    snap = MarketSnapshot(df, days=130)
    expected = df.groupby("symbol")["close"].shift(periods)
    assert_series_equal(snap.shift("close", periods), expected)


@given(
    df=frames(),
    periods=st.integers(min_value=1, max_value=2),
    window=st.integers(min_value=1, max_value=4),
)
@h_settings(max_examples=50, deadline=None)
def test_rolling_shifted_matches_groupby(df: pd.DataFrame, periods: int, window: int) -> None:
    """rolling_shifted() 必须等于 groupby(...).shift(n).rolling(w).max()。"""
    snap = MarketSnapshot(df, days=130)
    expected = df.groupby("symbol")["high"].transform(
        lambda s: s.shift(periods).rolling(window).max()
    )
    assert_series_equal(snap.rolling_shifted("high", periods, window, "max"), expected)


@given(df=frames(), threshold=st.floats(min_value=1000, max_value=1100))
@h_settings(max_examples=50, deadline=None)
def test_latest_picks_only_last_row_per_symbol(df: pd.DataFrame, threshold: float) -> None:
    """latest() 只应在每只股票的最后一行上取结果。"""
    snap = MarketSnapshot(df, days=130)
    condition = snap.col("volume") > threshold

    expected = sorted(
        symbol for symbol, group in df.groupby("symbol") if group["volume"].iloc[-1] > threshold
    )
    assert sorted(snap.latest(condition)) == expected


def test_enough_bars_counts_per_symbol() -> None:
    """enough_bars 统计的是每只股票自己的 K 线数，而不是全局行数。"""
    df = build_frame([("000001", 10), ("600000", 40)])
    snap = MarketSnapshot(df, days=130)

    enough = snap.enough_bars(20)
    assert not enough.iloc[:10].any(), "10 根 K 线的股票不应满足 20 根门槛"
    assert enough.iloc[10:].all(), "40 根 K 线的股票应满足 20 根门槛"


def test_rejects_missing_columns() -> None:
    """缺少必需列时应立刻报错，而不是静默算出错误结果。"""
    df = pd.DataFrame({"symbol": ["000001"], "date": ["2024-01-01"]})
    with pytest.raises(ValueError, match="缺少必需列"):
        MarketSnapshot(df, days=130)


def test_rejects_bad_arguments() -> None:
    """非法参数应报错。"""
    snap = MarketSnapshot(build_frame([("000001", 5)]), days=130)
    with pytest.raises(ValueError):
        snap.rolling("close", 0)
    with pytest.raises(ValueError):
        snap.rolling("close", 3, "median")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        snap.shift("close", 0)
    with pytest.raises(KeyError):
        snap.col("nope")


def test_empty_frame_is_safe() -> None:
    """空快照的各方法都不应抛异常。"""
    snap = MarketSnapshot(pd.DataFrame(columns=COLUMNS), days=130)
    assert snap.row_count == 0
    assert snap.symbols == []
    assert snap.latest(snap.col("close") > 0) == []
    assert len(snap.rolling("close", 5)) == 0
    assert "空快照" in snap.describe()
