"""通用过滤（可交易性 / 质量）的单元测试。

全部基于合成快照，不碰数据库与网络。
"""

import pandas as pd
import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data.snapshot import MarketSnapshot
from sequoia_x.strategy.filters import (
    at_limit_down,
    at_limit_up,
    lower_shadow_ratio,
    pct_change,
    tradable_mask,
    volume_ratio,
)

COLUMNS = [
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
]


def build(rows: list[dict], extra: dict | None = None) -> MarketSnapshot:
    """按行构造快照；``extra`` 用于附加 is_st / is_stale 等标记列。"""
    frame = pd.DataFrame(rows, columns=COLUMNS)
    for key, values in (extra or {}).items():
        frame[key] = values
    return MarketSnapshot(frame, days=130)


def bar(symbol: str, day: str, close: float, **kwargs) -> dict:
    base = {
        "symbol": symbol,
        "date": day,
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": 1_000_000.0,
        "turnover": 100_000_000.0,
    }
    base.update(kwargs)
    return base


def series(symbol: str, closes: list[float], **kwargs) -> list[dict]:
    """同一只股票连续几天的 K 线。"""
    return [bar(symbol, f"2024-01-{i + 1:02d}", close, **kwargs) for i, close in enumerate(closes)]


def make_settings(min_history_bars: int = 3) -> Settings:
    """测试用配置：把"历史长度"门槛降到最小值 3，避免每个用例都要造 120 根 K 线。"""
    return Settings(
        db_path="data/unused.db",
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
        min_history_bars=min_history_bars,
    )


# ── 涨跌停判定 ──


def test_at_limit_up_only_for_main_board_10pct() -> None:
    """主板 +10% 算涨停；创业板 +10% 不算（其涨停是 +20%）。"""
    rows = [
        bar("600000", "2024-01-01", 10.0),
        bar("600000", "2024-01-02", 11.0),  # +10%
        bar("300750", "2024-01-01", 10.0),
        bar("300750", "2024-01-02", 11.0),  # +10%，但它需要 +20%
    ]
    snap = build(rows)

    flags = at_limit_up(snap).tolist()
    assert flags[1] is True or flags[1] == True  # noqa: E712  主板次日为涨停
    assert not flags[3]  # 创业板次日不是涨停


def test_at_limit_up_flags_one_word_board() -> None:
    """一字板（high == low）即使涨幅不足也算买不到。"""
    rows = [
        bar("600000", "2024-01-01", 10.0),
        bar("600000", "2024-01-02", 10.5, open=10.5, high=10.5, low=10.5),
    ]
    snap = build(rows)

    assert bool(at_limit_up(snap).iloc[-1]) is True


def test_at_limit_down_for_gem() -> None:
    """创业板 -21% 才是跌停，-10% 不是。"""
    rows = [
        bar("300750", "2024-01-01", 10.0),
        bar("300750", "2024-01-02", 9.0),  # -10%
        bar("300750", "2024-01-03", 7.9),  # 约 -12% 于 9.0
    ]
    snap = build(rows)
    flags = at_limit_down(snap).tolist()

    assert not flags[1]
    assert not flags[2]


# ── 通用可交易过滤 ──


def test_tradable_mask_excludes_st_and_stale() -> None:
    rows = (
        series("600001", [10.0, 10.0, 10.0])
        + series("600002", [10.0, 10.0, 10.0])
        + series("600003", [10.0, 10.0, 10.0])
    )
    snap = build(
        rows,
        {
            "is_st": [True] * 3 + [False] * 6,
            "is_stale": [False] * 3 + [True] * 3 + [False] * 3,
        },
    )

    mask = tradable_mask(snap, make_settings())
    by_symbol = dict(zip(snap.df["symbol"], mask, strict=True))
    assert not by_symbol["600001"]  # ST
    assert not by_symbol["600002"]  # 停牌/退市
    assert by_symbol["600003"] is True or by_symbol["600003"] == True  # noqa: E712


def test_tradable_mask_excludes_illiquid_and_low_price() -> None:
    rows = (
        series("600001", [10.0] * 3, turnover=1_000_000.0)  # 成交额太小
        + series("600002", [1.5] * 3)  # 股价太低
        + series("600003", [10.0] * 3)
    )
    snap = build(rows)

    mask = tradable_mask(snap, make_settings())
    by_symbol = dict(zip(snap.df["symbol"], mask, strict=True))
    assert not by_symbol["600001"]
    assert not by_symbol["600002"]
    assert bool(by_symbol["600003"]) is True


def test_tradable_mask_requires_enough_history() -> None:
    """窗口内 K 线不足 min_history_bars 的股票应被过滤（次新/长期停牌）。"""
    settings = make_settings(min_history_bars=20)
    rows = [bar("600001", f"2024-01-{i + 1:02d}", 10.0) for i in range(25)]
    rows += [bar("600002", f"2024-02-{i + 1:02d}", 10.0) for i in range(10)]
    snap = build(rows)

    mask = tradable_mask(snap, settings)
    by_symbol = dict(zip(snap.df["symbol"], mask, strict=True))
    assert bool(by_symbol["600001"]) is True
    assert not by_symbol["600002"]


def test_tradable_mask_can_allow_limit_up() -> None:
    """allow_limit_up=True 时不再剔除涨停（给"昨日涨停"类策略用）。"""
    rows = series("600000", [10.0, 10.0, 11.0])
    snap = build(rows)
    settings = make_settings()

    assert not bool(tradable_mask(snap, settings).iloc[-1])
    assert bool(tradable_mask(snap, settings, allow_limit_up=True).iloc[-1])


def test_tradable_mask_is_idempotent() -> None:
    """连续调用两次必须得到相同结果（防止原地改写快照的缓存对象）。"""
    rows = series("600000", [10.0, 10.0, 11.0]) + series("300750", [10.0, 10.0, 11.0])
    snap = build(rows)
    settings = make_settings()

    first = tradable_mask(snap, settings, allow_limit_up=True).tolist()
    second = tradable_mask(snap, settings, allow_limit_up=True).tolist()
    assert first == second


# ── 派生指标 ──


def test_volume_ratio_excludes_today() -> None:
    """量比基准是"前 20 日均量"，不能被当日巨量抬高。"""
    rows = [bar("600000", f"2024-01-{i + 1:02d}", 10.0, volume=1000.0) for i in range(20)]
    rows.append(bar("600000", "2024-01-21", 10.0, volume=5000.0))
    snap = build(rows)

    ratio = volume_ratio(snap)
    assert ratio.iloc[-1] == pytest.approx(5.0)


def test_lower_shadow_ratio() -> None:
    rows = [
        bar("600000", "2024-01-01", 10.0, open=10.0, high=10.0, low=9.0),
    ]
    snap = build(rows)

    # (close - low) / (high - low) = (10-9)/(10-9) = 1.0（收在最高）
    assert lower_shadow_ratio(snap).iloc[-1] == pytest.approx(1.0)


def test_pct_change() -> None:
    rows = [
        bar("600000", "2024-01-01", 10.0),
        bar("600000", "2024-01-02", 12.0),
    ]
    snap = build(rows)

    assert pct_change(snap, 1).iloc[-1] == pytest.approx(0.2)
