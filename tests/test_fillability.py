"""撮合现实性测试：次日一字板买不进、资金量超过成交额买不到。"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from backtest import add_fillability, fillable  # noqa: E402


def bars(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows).sort_values(["symbol", "date"]).reset_index(drop=True)


def test_add_fillability_computes_prev_close_and_next_open() -> None:
    df = bars(
        [
            {"symbol": "600000", "date": "2024-01-02", "open": 10.0, "close": 10.0},
            {"symbol": "600000", "date": "2024-01-03", "open": 11.0, "close": 11.5},
            {"symbol": "600000", "date": "2024-01-04", "open": 12.0, "close": 12.0},
        ]
    )
    out = add_fillability(df)

    assert out.loc[0, "next_open"] == 11.0
    assert out.loc[1, "prev_close"] == 10.0
    assert pd.isna(out.loc[2, "next_open"])  # 最后一根没有次日


def test_fillable_drops_next_day_limit_up() -> None:
    """主板：T+1 开盘较 T 日收盘涨 9.5% 以上 → 一字板买不进。"""
    merged = bars(
        [
            {
                "symbol": "600000",
                "date": "2024-01-02",
                "turnover": 1e9,
                "prev_close": 10.0,
                "next_open": 11.0,  # +10% 涨停
            },
            {
                "symbol": "000001",
                "date": "2024-01-02",
                "turnover": 1e9,
                "prev_close": 10.0,
                "next_open": 10.2,  # 正常开盘
            },
        ]
    )
    out, stats = fillable(merged, capital=None, participation=None)

    assert stats["dropped_limit_up"] == 1
    assert out["symbol"].tolist() == ["000001"]


def test_fillable_drops_capacity_overflow() -> None:
    """1000 万资金买 2 只 → 每只 500 万；成交额 1 亿 × 1% = 100 万，装不下。"""
    merged = bars(
        [
            {
                "symbol": "600000",
                "date": "2024-01-02",
                "turnover": 1e8,
                "prev_close": 10.0,
                "next_open": 10.0,
            },
            {
                "symbol": "000001",
                "date": "2024-01-02",
                "turnover": 1e10,  # 100 亿，装得下
                "prev_close": 10.0,
                "next_open": 10.0,
            },
        ]
    )
    out, stats = fillable(merged, capital=1e7, participation=0.01)

    assert stats["dropped_capacity"] == 1
    assert out["symbol"].tolist() == ["000001"]


def test_fillable_keeps_everything_when_liquid() -> None:
    merged = bars(
        [
            {
                "symbol": "600000",
                "date": "2024-01-02",
                "turnover": 1e10,
                "prev_close": 10.0,
                "next_open": 10.1,
            }
        ]
    )
    out, stats = fillable(merged, capital=1e7, participation=0.01)

    assert len(out) == 1
    assert stats == {"dropped_capacity": 0, "dropped_limit_up": 0}


def test_fillable_without_capital_only_checks_limit_up() -> None:
    merged = bars(
        [
            {
                "symbol": "600000",
                "date": "2024-01-02",
                "turnover": 1e6,
                "prev_close": 10.0,
                "next_open": 10.0,
            }
        ]
    )
    out, stats = fillable(merged, capital=None, participation=None)
    assert len(out) == 1
    assert stats["dropped_capacity"] == 0


def test_fillable_empty() -> None:
    out, stats = fillable(pd.DataFrame(), capital=1e7, participation=0.01)
    assert out.empty
    assert stats == {"dropped_capacity": 0, "dropped_limit_up": 0}


def test_chinext_limit_is_20_percent() -> None:
    """创业板 30 开头：涨 10% 不算涨停，应当可以成交。"""
    merged = bars(
        [
            {
                "symbol": "300001",
                "date": "2024-01-02",
                "turnover": 1e10,
                "prev_close": 10.0,
                "next_open": 11.0,  # +10%
            }
        ]
    )
    out, stats = fillable(merged, capital=None, participation=None)
    assert stats["dropped_limit_up"] == 0
    assert len(out) == 1
