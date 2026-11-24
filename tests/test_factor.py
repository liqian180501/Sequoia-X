"""因子层测试：收益口径、IC 的符号与数值、分层单调性、因子库冒烟。"""

import numpy as np
import pandas as pd
import pytest

from sequoia_x.data.snapshot import MarketSnapshot
from sequoia_x.factor.evaluate import (
    add_forward_returns,
    cross_sectional_ic,
    layered_returns,
    summarize_ic,
)
from sequoia_x.factor.library import FACTORS

COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]


def price_frame(symbols: int = 60, days: int = 6) -> pd.DataFrame:
    """``symbols`` 只股票、``days`` 天，每天涨跌幅按序号递增（截面单调）。"""
    rows = []
    for day in range(1, days + 1):
        for i in range(symbols):
            close = 10.0 + i * 0.1 + day * i * 0.01
            rows.append(
                {
                    "symbol": f"{600000 + i}",
                    "date": f"2024-01-{day:02d}",
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": 1e6 + i * 1e3,
                    "turnover": 1e8 + i * 1e5,
                }
            )
    return (
        pd.DataFrame(rows, columns=COLUMNS).sort_values(["symbol", "date"]).reset_index(drop=True)
    )


# ── 收益口径 ──


def test_forward_return_uses_next_open_and_horizon_close() -> None:
    """fwd = close[t+h] / open[t+1] - 1。"""
    df = pd.DataFrame(
        {
            "symbol": ["600000"] * 4,
            "date": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
            "open": [10.0, 10.5, 11.0, 11.5],
            "close": [10.0, 11.0, 12.0, 13.0],
        }
    )
    fwd = add_forward_returns(df, horizon=1)
    # t=0：次日开盘 10.5 买入，次日收盘 11.0 卖出
    assert fwd.iloc[0] == pytest.approx(11.0 / 10.5 - 1)


def test_forward_return_nan_at_tail() -> None:
    df = pd.DataFrame(
        {
            "symbol": ["600000"] * 3,
            "date": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "open": [10.0, 11.0, 12.0],
            "close": [10.0, 11.0, 12.0],
        }
    )
    fwd = add_forward_returns(df, horizon=2)
    assert fwd.iloc[-1] != fwd.iloc[-1]  # 末尾没有未来数据


# ── IC ──


def test_ic_is_positive_for_monotonic_factor() -> None:
    frame = price_frame()
    forward = frame["symbol"].astype(int) - 600000  # 与因子同向
    ic = cross_sectional_ic(frame["close"], forward, frame["date"], min_names=10)

    assert len(ic) >= 5
    assert ic.mean() == pytest.approx(1.0, abs=1e-6)


def test_ic_is_negative_when_reversed() -> None:
    frame = price_frame()
    forward = -(frame["symbol"].astype(int) - 600000)
    ic = cross_sectional_ic(frame["close"], forward, frame["date"], min_names=10)
    assert ic.mean() == pytest.approx(-1.0, abs=1e-6)


def test_ic_skips_small_cross_sections() -> None:
    frame = price_frame(symbols=5)
    ic = cross_sectional_ic(frame["close"], frame["close"], frame["date"], min_names=50)
    assert ic.empty


def test_ic_ignores_nan() -> None:
    frame = price_frame()
    forward = (frame["symbol"].astype(int) - 600000).astype(float)
    forward.iloc[:10] = np.nan
    ic = cross_sectional_ic(frame["close"], forward, frame["date"], min_names=10)
    assert not ic.empty
    assert ic.notna().all()


# ── 汇总 ──


def test_summarize_ic() -> None:
    ic = pd.Series([0.1, 0.2, 0.05, -0.02])
    stats = summarize_ic(ic)
    assert stats["n"] == 4
    assert stats["ic_mean"] == pytest.approx(8.25, abs=0.01)
    assert stats["icir"] is not None
    assert stats["win_rate"] == pytest.approx(75.0)


def test_summarize_ic_empty() -> None:
    assert summarize_ic(pd.Series(dtype="float64")) == {"n": 0}


# ── 分层 ──


def test_layered_returns_monotonic() -> None:
    frame = price_frame(symbols=100)
    forward = (frame["symbol"].astype(int) - 600000) * 1e-4
    layers = layered_returns(frame["close"], forward, frame["date"], quantiles=5)

    assert layers.loc[5, "ret"] > layers.loc[1, "ret"]
    assert layers.loc["spread", "ret"] > 0


def test_layered_returns_empty() -> None:
    empty = pd.Series(dtype="float64")
    assert layered_returns(empty, empty, empty).empty


# ── 因子库 ──


def test_all_factors_are_series_of_right_length() -> None:
    frame = price_frame(symbols=20, days=5)
    snap = MarketSnapshot(frame, days=5)

    for name, fn in FACTORS.items():
        series = fn(snap)
        assert len(series) == len(frame), name


def test_factor_names_are_unique_and_documented() -> None:
    assert len(FACTORS) == len(set(FACTORS))
    assert {"mom_20", "mom_60", "near_high_250", "volume_ratio"} <= set(FACTORS)
