"""数据健康检查与告警的测试（临时 SQLite，不联网）。"""

import sqlite3
from datetime import date

import pytest

from sequoia_x.monitor.health import check, expected_trade_date

SCHEMA = """
CREATE TABLE stock_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
    volume REAL, turnover REAL, UNIQUE(symbol, date)
);
"""


def make_db(tmp_path, rows=None, with_picks=False):
    """建一个最小库。``rows`` 形如 [(symbol, date)]。"""
    path = tmp_path / "h.db"
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    for symbol, dt in rows or []:
        conn.execute(
            "INSERT INTO stock_daily (symbol,date,open,high,low,close,volume,turnover) "
            "VALUES (?,?,10,10,10,10,1e6,1e8)",
            (symbol, dt),
        )
    if with_picks:
        conn.execute(
            "CREATE TABLE pick_history (run_date TEXT, strategy TEXT, symbol TEXT, "
            "rank INTEGER, close REAL, created_at TEXT, source TEXT, "
            "PRIMARY KEY (run_date, strategy, symbol))"
        )
        conn.execute(
            "INSERT INTO pick_history VALUES ('2024-01-05','turtle','600000',1,10,'','live')"
        )
    conn.commit()
    conn.close()
    return str(path)


# ── 应有交易日 ──


def test_expected_trade_date_skips_weekend() -> None:
    assert expected_trade_date(date(2024, 1, 6)) == date(2024, 1, 5)  # 周六 → 周五
    assert expected_trade_date(date(2024, 1, 7)) == date(2024, 1, 5)  # 周日 → 周五
    assert expected_trade_date(date(2024, 1, 8)) == date(2024, 1, 8)  # 周一 → 当天


# ── 健康 ──


def test_healthy_db(tmp_path) -> None:
    db = make_db(
        tmp_path,
        rows=[
            ("600000", "2024-01-04"),
            ("600000", "2024-01-05"),
            ("000001", "2024-01-04"),
            ("000001", "2024-01-05"),
        ],
        with_picks=True,
    )
    result = check(db, max_lag_days=1, today=date(2024, 1, 5))

    assert result["ok"] is True
    assert result["stats"]["rows"] == 4
    assert result["stats"]["symbols"] == 2
    assert result["stats"]["lag_days"] == 0
    assert result["stats"]["coverage"] == 1.0
    assert result["stats"]["last_pick_date"] == "2024-01-05"


def test_stale_data_is_unhealthy(tmp_path) -> None:
    db = make_db(tmp_path, rows=[("600000", "2024-01-02")])
    result = check(db, max_lag_days=1, today=date(2024, 1, 10))

    assert result["ok"] is False
    assert any("落后" in issue for issue in result["issues"])


def test_low_coverage_is_unhealthy(tmp_path) -> None:
    """最新交易日只有一半股票有数据 → 同步可能失败。"""
    db = make_db(
        tmp_path,
        rows=[("600000", "2024-01-05"), ("000001", "2024-01-04")],
    )
    result = check(db, today=date(2024, 1, 5))

    assert result["ok"] is False
    assert any("同步不完整" in issue for issue in result["issues"])
    assert result["stats"]["coverage"] == pytest.approx(0.5)


def test_empty_db_is_unhealthy(tmp_path) -> None:
    db = make_db(tmp_path, rows=[])
    result = check(db, today=date(2024, 1, 5))
    assert result["ok"] is False
    assert "空" in result["issues"][0]


def test_missing_file_is_unhealthy(tmp_path) -> None:
    result = check(str(tmp_path / "nope.db"))
    assert result["ok"] is False


# ── 告警 ──


def test_alert_is_noop_without_webhook(settings_without_alert) -> None:
    """没配置告警 webhook 时只写日志、不抛异常、返回 False。"""
    from sequoia_x.notify.alert import send_alert

    assert send_alert(settings_without_alert, "测试", ["一行"]) is False


def test_alert_swallows_network_errors(settings_with_bad_alert) -> None:
    """webhook 地址不可达时也不能抛异常。"""
    from sequoia_x.notify.alert import send_alert

    assert send_alert(settings_with_bad_alert, "测试", ["一行"]) is False


@pytest.fixture
def settings_without_alert():
    from sequoia_x.core.config import Settings

    return Settings(feishu_webhook_url="https://example.invalid/hook", alert_webhook_url=None)


@pytest.fixture
def settings_with_bad_alert():
    from sequoia_x.core.config import Settings

    return Settings(
        feishu_webhook_url="https://example.invalid/hook",
        alert_webhook_url="http://127.0.0.1:9/never",
    )
