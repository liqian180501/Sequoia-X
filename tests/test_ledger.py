"""信号留痕与纸面账本的测试：幂等写入、收益口径、报告结构。"""

import sqlite3

import pandas as pd
import pytest

from sequoia_x.data.ledger import SignalLedger


@pytest.fixture
def db(tmp_path):
    """建一个含 6 个交易日、2 只股票的临时库。"""
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE stock_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, "
        "date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, turnover REAL, "
        "UNIQUE(symbol,date))"
    )
    # 600000：每天 +10%（open=前收，close=+10%）
    # 000001：每天 0%
    rows = []
    closes = {"600000": 10.0, "000001": 20.0}
    for day in range(1, 7):
        date = f"2024-01-{day:02d}"
        for symbol, prev in list(closes.items()):
            open_ = prev
            close = prev * (1.1 if symbol == "600000" else 1.0)
            rows.append((symbol, date, open_, close, close, close, 1e6, 1e8))
            closes[symbol] = close
    conn.executemany(
        "INSERT OR IGNORE INTO stock_daily (symbol,date,open,high,low,close,volume,turnover) "
        "VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return str(path)


# ── 写入 ──


def test_record_is_idempotent(db) -> None:
    ledger = SignalLedger(db)
    assert ledger.record("2024-01-01", "ma_volume", ["600000", "000001"]) == 2
    # 同一天同一策略重复记录：不新增
    assert ledger.record("2024-01-01", "ma_volume", ["600000", "000001"]) == 0

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pick_history").fetchone()[0] == 2


def test_record_source_defaults_to_live(db) -> None:
    """实盘留痕与历史回放必须可区分。"""
    ledger = SignalLedger(db)
    ledger.record("2024-01-01", "ma_volume", ["600000"])
    ledger.record("2024-01-02", "ma_volume", ["600000"], source="replay")

    with sqlite3.connect(db) as conn:
        rows = dict(conn.execute("SELECT run_date, source FROM pick_history").fetchall())
    assert rows == {"2024-01-01": "live", "2024-01-02": "replay"}


def test_record_preserves_rank_and_price(db) -> None:
    ledger = SignalLedger(db)
    ledger.record("2024-01-01", "turtle", ["000001", "600000"])

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT symbol, rank, close FROM pick_history ORDER BY rank"
        ).fetchall()
    assert rows[0] == ("000001", 1, pytest.approx(20.0))
    assert rows[1] == ("600000", 2, pytest.approx(11.0))  # 01-01 当日已 +10%


def test_record_empty_symbols(db) -> None:
    ledger = SignalLedger(db)
    assert ledger.record("2024-01-01", "ma_volume", []) == 0


def test_record_with_explicit_prices(db) -> None:
    """传入 prices 时不再查库（信号日可能不是库里的最后一天）。"""
    ledger = SignalLedger(db)
    ledger.record("2024-01-03", "rps", ["999999"], prices={"999999": 12.34})

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT close FROM pick_history WHERE symbol='999999'"
        ).fetchone()[0] == pytest.approx(12.34)


# ── 报告 ──


def test_report_empty(db) -> None:
    report = SignalLedger(db).report(days=30)
    assert report["rows"] == []
    assert report["recent"] == []


def test_report_computes_forward_returns(db) -> None:
    """留痕在 01-01：T+1 开盘买、T+2 收盘卖，600000 应约 +10% 减成本。"""
    ledger = SignalLedger(db, cost=0.0)
    ledger.record("2024-01-01", "ma_volume", ["600000"])

    report = ledger.report(days=3650)
    row = report["rows"][0]
    assert row["strategy"] == "ma_volume"
    assert row["trades"] == 1
    # 01-02 开盘 = 01-01 收盘 = 10.0；01-02 收盘 = 11.0 → fwd_1 = +10%
    assert row["h1_mean"] == pytest.approx(10.0, abs=0.01)
    assert row["h1_win"] == pytest.approx(100.0)


def test_report_applies_cost(db) -> None:
    ledger = SignalLedger(db, cost=0.01)  # 1% 成本
    ledger.record("2024-01-01", "ma_volume", ["600000"])

    row = ledger.report(days=3650)["rows"][0]
    assert row["h1_mean"] == pytest.approx(9.0, abs=0.01)  # 10% - 1%


def test_report_excess_is_relative_to_market(db) -> None:
    """基准是同一天全市场等权中位数：600000 +10%、000001 0% → 中位数 5%。"""
    ledger = SignalLedger(db, cost=0.0)
    ledger.record("2024-01-01", "ma_volume", ["600000"])

    row = ledger.report(days=3650)["rows"][0]
    assert row["h1_exc"] == pytest.approx(5.0, abs=0.01)


def test_report_pending_for_latest_signal(db) -> None:
    """信号日之后没有行情（T+1 还没发生）→ 收益为空，记入 pending。"""
    ledger = SignalLedger(db)
    ledger.record("2024-01-06", "ma_volume", ["600000"])

    row = ledger.report(days=3650)["rows"][0]
    assert row["pending"] == 1
    assert row["h1_mean"] is None


def test_report_filters_by_strategy(db) -> None:
    ledger = SignalLedger(db)
    ledger.record("2024-01-01", "ma_volume", ["600000"])
    ledger.record("2024-01-01", "turtle", ["000001"])

    report = ledger.report(days=3650, strategy="turtle")
    assert [r["strategy"] for r in report["rows"]] == ["turtle"]


def test_report_recent_rows_shape(db) -> None:
    ledger = SignalLedger(db)
    ledger.record("2024-01-01", "ma_volume", ["600000", "000001"])

    recent = ledger.report(days=3650)["recent"]
    assert len(recent) == 2
    assert set(recent[0]) == {
        "run_date",
        "strategy",
        "source",
        "symbol",
        "rank",
        "close",
        "fwd_1",
        "fwd_5",
    }
    assert recent[0]["rank"] == 1


def test_report_sorted_by_h5_excess(db) -> None:
    ledger = SignalLedger(db, cost=0.0)
    ledger.record("2024-01-01", "ma_volume", ["000001"])  # 跑输
    ledger.record("2024-01-01", "turtle", ["600000"])  # 跑赢

    rows = ledger.report(days=3650)["rows"]
    assert rows[0]["strategy"] == "turtle"


def test_report_handles_missing_price(db) -> None:
    """留痕里的代码在行情表里不存在时，不能崩，按未结算处理。"""
    ledger = SignalLedger(db)
    ledger.record("2024-01-01", "ma_volume", ["999999"], prices={"999999": 1.0})

    row = ledger.report(days=3650)["rows"][0]
    assert row["trades"] == 1
    assert row["h1_mean"] is None


def test_dataframe_dtypes_are_numeric(db) -> None:
    """防止 SQLite 读出 object 类型导致比较/均值出错。"""
    ledger = SignalLedger(db, cost=0.0)
    ledger.record("2024-01-01", "ma_volume", ["600000"])
    fwd = ledger._forward_returns("2024-01-01")  # noqa: SLF001
    assert pd.api.types.is_float_dtype(fwd["fwd_1"])
