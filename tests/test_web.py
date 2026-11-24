"""网页控制台的后端逻辑测试（不起 HTTP 服务、不联网）。

只测纯函数与"给一个临时 SQLite 就能验证"的部分：
- ``db_stats`` 统计
- ``candles`` 读 K 线
- ``enrich_picks`` 补名称/涨跌幅/成交额
- ``strategy_meta`` 与注册表一致
"""

import sqlite3

import pandas as pd
import pytest

from sequoia_x.data.snapshot import MarketSnapshot
from web.server import candles, db_stats, enrich_picks, strategy_meta

COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]


@pytest.fixture
def db(tmp_path):
    """建一个只有 3 根 K 线的临时库。"""
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE stock_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, "
        "date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, turnover REAL, "
        "UNIQUE(symbol,date))"
    )
    conn.executemany(
        "INSERT INTO stock_daily (symbol,date,open,high,low,close,volume,turnover) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            ("600000", "2024-01-02", 10.0, 10.2, 9.9, 10.0, 1e6, 1e8),
            ("600000", "2024-01-03", 10.0, 10.5, 10.0, 11.0, 2e6, 2e8),
            ("600000", "2024-01-04", 11.0, 11.2, 10.8, 11.0, 1.5e6, 1.6e8),
            ("000001", "2024-01-04", 5.0, 5.1, 4.9, 5.0, 1e6, 5e7),
        ],
    )
    conn.commit()
    conn.close()
    return str(path)


# ── 数据库统计 ──


def test_db_stats(db) -> None:
    stats = db_stats(db)
    assert stats["rows"] == 4
    assert stats["symbols"] == 2
    assert stats["first_date"] == "2024-01-02"
    assert stats["last_date"] == "2024-01-04"
    assert stats["trading_days"] == 3
    assert stats["stale_symbols"] == 0  # 两只股票都更新到全表最大日
    assert stats["size_mb"] > 0


def test_db_stats_counts_lagging_symbols(db) -> None:
    """停牌/退市的股票（最后一根 K 线早于全表最大日）应被统计出来。"""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO stock_daily (symbol,date,open,high,low,close,volume,turnover) "
        "VALUES ('600519','2024-01-02',100,101,99,100,1e6,1e8)"
    )
    conn.commit()
    conn.close()

    assert db_stats(db)["stale_symbols"] == 1


def test_db_stats_missing_file(tmp_path) -> None:
    stats = db_stats(str(tmp_path / "nope.db"))
    assert stats["exists"] is False
    assert stats["rows"] == 0


# ── K 线 ──


def test_candles_returns_ascending(db) -> None:
    data = candles(db, "600000", limit=2)
    assert data["symbol"] == "600000"
    assert [b["d"] for b in data["bars"]] == ["2024-01-03", "2024-01-04"]
    assert data["bars"][-1]["c"] == 11.0


def test_candles_unknown_symbol(db) -> None:
    assert candles(db, "999999")["bars"] == []


# ── 选股结果富化 ──


def make_snapshot() -> MarketSnapshot:
    rows = []
    for i, (d, close) in enumerate(
        [("2024-01-02", 10.0), ("2024-01-03", 11.0)]
    ):
        rows.append(
            {
                "symbol": "600000",
                "date": d,
                "open": close,
                "high": close * 1.02,
                "low": close * 0.98,
                "close": close,
                "volume": 1e6,
                "turnover": 1e8,
            }
        )
    return MarketSnapshot(pd.DataFrame(rows, columns=COLUMNS), days=2)


def test_enrich_picks_adds_name_and_pct() -> None:
    snap = make_snapshot()
    rows = enrich_picks(snap, ["600000"], {"600000": "浦发银行"})

    assert len(rows) == 1
    assert rows[0]["name"] == "浦发银行"
    assert rows[0]["close"] == 11.0
    assert rows[0]["pct"] == pytest.approx(10.0, abs=0.01)
    assert rows[0]["turnover"] == pytest.approx(1.0)


def test_enrich_picks_unknown_symbol_keeps_row() -> None:
    snap = make_snapshot()
    rows = enrich_picks(snap, ["999999"], {})
    assert rows == [{"symbol": "999999", "name": "", "close": None}]


def test_enrich_picks_empty_inputs() -> None:
    snap = make_snapshot()
    assert enrich_picks(snap, [], {}) == []
    empty = MarketSnapshot(pd.DataFrame(columns=COLUMNS), days=0)
    assert enrich_picks(empty, ["600000"], {}) == []


def test_enrich_picks_preserves_order() -> None:
    snap = make_snapshot()
    rows = enrich_picks(snap, ["600000", "999999"], {})
    assert [r["symbol"] for r in rows] == ["600000", "999999"]


# ── 策略清单 ──


def test_strategy_meta_matches_registry() -> None:
    from sequoia_x.strategy.registry import STRATEGIES

    meta = strategy_meta()
    assert [m["key"] for m in meta] == list(STRATEGIES)
    assert all(m["doc"] for m in meta)
    assert {m["key"] for m in meta if not m["needs_market_data"]} == {"private_placement"}
