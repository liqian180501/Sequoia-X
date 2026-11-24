"""DuckDB 分析库的测试：从 SQLite 建表、从 Parquet 建表、查询与统计。"""

import sqlite3

import duckdb
import pandas as pd
import pytest

from sequoia_x.data.analytics import AnalyticsStore

SCHEMA = """
CREATE TABLE stock_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
    volume REAL, turnover REAL, UNIQUE(symbol, date)
);
"""


@pytest.fixture
def sqlite_db(tmp_path):
    path = tmp_path / "src.db"
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    rows = [
        ("600000", "2024-01-02", 10.0, 10.2, 9.9, 10.0, 1e6, 1e8),
        ("600000", "2024-01-03", 10.0, 11.0, 10.0, 10.8, 2e6, 2e8),
        ("000001", "2024-01-02", 5.0, 5.1, 4.9, 5.0, 1e6, 5e7),
    ]
    conn.executemany(
        "INSERT INTO stock_daily (symbol,date,open,high,low,close,volume,turnover) "
        "VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return str(path)


def test_build_from_sqlite(sqlite_db, tmp_path) -> None:
    store = AnalyticsStore(str(tmp_path / "a.duckdb"))
    try:
        assert store.build_from_sqlite(sqlite_db) == 3
        assert store.query("SELECT COUNT(*) AS n FROM bars").iloc[0]["n"] == 3
    finally:
        store.close()


def test_query_returns_dataframe(sqlite_db, tmp_path) -> None:
    store = AnalyticsStore(str(tmp_path / "a.duckdb"))
    try:
        store.build_from_sqlite(sqlite_db)
        df = store.query(
            "SELECT symbol, COUNT(*) AS n FROM bars GROUP BY symbol ORDER BY symbol"
        )
        assert isinstance(df, pd.DataFrame)
        assert df["symbol"].tolist() == ["000001", "600000"]
        assert df["n"].tolist() == [1, 2]
    finally:
        store.close()


def test_stats_lists_tables(sqlite_db, tmp_path) -> None:
    store = AnalyticsStore(str(tmp_path / "a.duckdb"))
    try:
        store.build_from_sqlite(sqlite_db)
        info = store.stats()
        assert info["tables"]["bars"] == 3
    finally:
        store.close()


def test_build_from_parquet(tmp_path) -> None:
    """Parquet 走 date_ms → 'YYYY-MM-DD' 的转换，这里验证转换结果。"""
    parquet = tmp_path / "d.parquet"
    conn = duckdb.connect()
    conn.execute(
        """
        COPY (
            SELECT '600000.SH' AS thscode, 'CNY' AS currency, '1d' AS interval,
                   'none' AS adjusted,
                   epoch_ms(TIMESTAMP '2024-01-02 00:00:00') AS date_ms,
                   10.0 AS open_price, 10.5 AS high_price, 9.5 AS low_price,
                   10.2 AS close_price, 1000.0 AS volume, 10200.0 AS turnover
        ) TO '""" + str(parquet).replace("\\", "/") + """' (FORMAT PARQUET)
        """
    )
    conn.close()

    store = AnalyticsStore(str(tmp_path / "b.duckdb"))
    try:
        assert store.build_from_parquet(str(parquet)) == 1
        row = store.query("SELECT symbol, date, close FROM bars_raw").iloc[0]
        assert row["symbol"] == "600000.SH"
        assert row["date"].startswith("2024-01-02")
        assert row["close"] == pytest.approx(10.2)
    finally:
        store.close()


def test_rebuild_is_idempotent(sqlite_db, tmp_path) -> None:
    """重复构建不会把行数翻倍（CREATE OR REPLACE）。"""
    store = AnalyticsStore(str(tmp_path / "a.duckdb"))
    try:
        store.build_from_sqlite(sqlite_db)
        store.build_from_sqlite(sqlite_db)
        assert store.query("SELECT COUNT(*) AS n FROM bars").iloc[0]["n"] == 3
    finally:
        store.close()
