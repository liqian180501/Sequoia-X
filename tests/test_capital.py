"""资金监控的测试：存储幂等、连续性规则、代理指标口径。"""

import sqlite3

import pandas as pd
import pytest

from sequoia_x.capital.monitor import CapitalFlowStore, detect
from sequoia_x.capital.sources import price_volume_proxy


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "c.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE stock_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, "
        "date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, "
        "turnover REAL, UNIQUE(symbol,date))"
    )
    conn.commit()
    conn.close()
    return str(path)


def flows(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["symbol", "date", "net_main", "turnover"])


# ── 检测规则 ──


def test_detects_three_day_inflow() -> None:
    frame = flows(
        [
            ("600000", "2024-01-02", 2e7, 1e8),
            ("600000", "2024-01-03", 2e7, 1e8),
            ("600000", "2024-01-04", 2e7, 1e8),
        ]
    )
    signals = detect(frame, streak=3, min_net=5e7)

    assert len(signals) == 1
    assert signals[0].kind == "inflow"
    assert signals[0].total_net == pytest.approx(6e7)
    assert signals[0].ratio == pytest.approx(0.2, abs=1e-3)


def test_ignores_mixed_direction() -> None:
    frame = flows(
        [
            ("600000", "2024-01-02", 2e7, 1e8),
            ("600000", "2024-01-03", -1e7, 1e8),
            ("600000", "2024-01-04", 2e7, 1e8),
        ]
    )
    assert detect(frame, streak=3, min_net=0) == []


def test_ignores_below_threshold() -> None:
    frame = flows([("600000", f"2024-01-0{d}", 1e6, 1e8) for d in (2, 3, 4)])
    assert detect(frame, streak=3, min_net=5e7) == []


def test_ignores_short_history() -> None:
    frame = flows([("600000", "2024-01-02", 1e8, 1e8)])
    assert detect(frame, streak=3, min_net=0) == []


def test_outflow_is_detected_and_sorted_by_size() -> None:
    frame = flows(
        [("600000", f"2024-01-0{d}", -1e7, 1e8) for d in (2, 3, 4)]
        + [("000001", f"2024-01-0{d}", -9e7, 1e8) for d in (2, 3, 4)]
    )
    signals = detect(frame, streak=3, min_net=0)

    assert [s.kind for s in signals] == ["outflow", "outflow"]
    assert signals[0].symbol == "000001"  # 金额更大的排前面


def test_min_ratio_filter() -> None:
    """净额够大但占成交额比例太低 → 过滤掉。"""
    frame = flows([("600000", f"2024-01-0{d}", 2e7, 1e10) for d in (2, 3, 4)])
    assert detect(frame, streak=3, min_net=5e7, min_ratio=0.01) == []


# ── 存储 ──


def test_store_upsert_is_idempotent(db) -> None:
    store = CapitalFlowStore(db)
    frame = flows([("600000", "2024-01-02", 1e7, 1e8)])

    assert store.upsert(frame, "proxy") == 1
    assert store.upsert(frame, "proxy") == 1  # 覆盖而不是新增
    assert len(store.load("proxy")) == 1


def test_store_keeps_sources_separate(db) -> None:
    """代理指标与真实资金流不能混在一起算。"""
    store = CapitalFlowStore(db)
    frame = flows([("600000", "2024-01-02", 1e7, 1e8)])
    store.upsert(frame, "proxy")
    store.upsert(frame, "akshare")

    assert len(store.load("proxy")) == 1
    assert len(store.load("akshare")) == 1


def test_store_load_since(db) -> None:
    store = CapitalFlowStore(db)
    store.upsert(flows([("600000", "2024-01-02", 1e7, 1e8)]), "proxy")
    store.upsert(flows([("600000", "2024-01-05", 1e7, 1e8)]), "proxy")

    assert len(store.load("proxy", since="2024-01-04")) == 1


# ── 代理指标 ──


def test_proxy_uses_price_change(db) -> None:
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO stock_daily (symbol,date,open,high,low,close,volume,turnover) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            ("600000", "2024-01-02", 10, 10, 10, 10.0, 1e6, 1e8),
            ("600000", "2024-01-03", 10, 11, 10, 11.0, 1e6, 1e8),  # +10%
            ("600000", "2024-01-04", 11, 11, 11, 11.0, 1e6, 1e8),  # 0%
            ("600000", "2024-01-05", 11, 11, 10, 10.45, 1e6, 1e8),  # -5%
        ],
    )
    conn.commit()
    conn.close()

    out = price_volume_proxy(db, days=4)
    assert len(out) == 3
    values = out["net_main"].round(0).tolist()
    assert values[0] == pytest.approx(1e7)  # +10% × 1 亿
    assert values[1] == pytest.approx(0.0)
    assert values[2] == pytest.approx(-5e6)
