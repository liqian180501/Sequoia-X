"""本地缓存表（股票目录 / 流通市值）的单元测试。

用 ``sqlite3.connect(":memory:")`` 作为共享连接，因此不需要临时目录、也不打网络。
"""

import sqlite3
from unittest.mock import MagicMock

import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data.cache import MarketCapCache, StockDirectory, to_stock_symbol
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def directory(conn) -> StockDirectory:
    return StockDirectory(":memory:", conn=conn)


@pytest.fixture
def cap_cache(conn) -> MarketCapCache:
    return MarketCapCache(":memory:", conn=conn)


def insert_names(conn, pairs) -> None:
    conn.executemany("INSERT INTO stock_info (symbol, name) VALUES (?, ?)", pairs)
    conn.commit()


# ── StockDirectory ──


def test_returns_local_names_without_refresh(directory, conn, monkeypatch) -> None:
    """本地命中时不应触发任何刷新。"""
    insert_names(conn, [("000001", "平安银行"), ("600000", "浦发银行")])

    refresh = MagicMock(return_value=1)
    monkeypatch.setattr(directory, "refresh", refresh)

    assert directory.get_names(["000001", "600000"]) == {
        "000001": "平安银行",
        "600000": "浦发银行",
    }
    refresh.assert_not_called()


def test_refreshes_once_when_missing(directory, conn, monkeypatch) -> None:
    """本地缺失时应刷新一次，并把新数据返回给调用方。"""

    def fake_refresh() -> int:
        insert_names(conn, [("600000", "浦发银行")])
        return 1

    refresh = MagicMock(side_effect=fake_refresh)
    monkeypatch.setattr(directory, "refresh", refresh)

    assert directory.get_names(["600000", "999999"]) == {"600000": "浦发银行"}
    refresh.assert_called_once()

    # 第二次全部命中，不应再刷新
    again = MagicMock(return_value=1)
    monkeypatch.setattr(directory, "refresh", again)
    assert directory.get_names(["600000"]) == {"600000": "浦发银行"}
    again.assert_not_called()


def test_refresh_failure_degrades_to_empty(directory, monkeypatch) -> None:
    """刷新失败（返回 0）时应安静降级，而不是抛异常。"""
    monkeypatch.setattr(directory, "refresh", MagicMock(return_value=0))
    assert directory.get_names(["600000"]) == {}


def test_deduplicates_input(directory, conn) -> None:
    """重复代码不应导致重复查询或报错。"""
    insert_names(conn, [("600000", "浦发银行")])
    assert directory.get_names(["600000", "600000"]) == {"600000": "浦发银行"}


def test_handles_more_symbols_than_sql_parameter_limit(directory, conn) -> None:
    """批量查询要分批，避免超出 SQLite 的绑定参数上限。"""
    symbols = [f"{i:06d}" for i in range(1200)]
    insert_names(conn, [(s, f"name-{s}") for s in symbols])

    result = directory.get_names(symbols)
    assert len(result) == 1200
    assert result["000000"] == "name-000000"


def test_empty_input(directory) -> None:
    assert directory.get_names([]) == {}


def test_count(directory, conn) -> None:
    assert directory.count() == 0
    insert_names(conn, [("600000", "浦发银行")])
    assert directory.count() == 1


@pytest.mark.parametrize(
    ("bs_code", "expected"),
    [
        ("sh.600000", "600000"),  # 上交所主板
        ("sh.688001", "688001"),  # 科创板
        ("sz.000001", "000001"),  # 深交所主板
        ("sz.300750", "300750"),  # 创业板
        ("sz.002594", "002594"),  # 中小板
        ("sh.000001", None),  # 上证综合指数（数字部分与深市 000001 冲突）
        ("sz.399001", None),  # 深证成指
        ("sh.510300", None),  # ETF
        ("600000", None),  # 缺少市场前缀
    ],
)
def test_to_stock_symbol_filters_non_stocks(bs_code: str, expected: str | None) -> None:
    """全市场名单里混有指数/基金，必须过滤掉，否则会覆盖同名股票。"""
    assert to_stock_symbol(bs_code) == expected


def test_single_missing_symbol_does_not_trigger_refresh(directory, conn, monkeypatch) -> None:
    """只缺一两个代码时不应重建目录（重建约 30 秒）。"""
    insert_names(conn, [("600000", "浦发银行")])
    refresh = MagicMock(return_value=1)
    monkeypatch.setattr(directory, "refresh", refresh)

    assert directory.get_names(["600000", "999999"]) == {"600000": "浦发银行"}
    refresh.assert_not_called()


def test_many_missing_symbols_trigger_refresh(directory, conn, monkeypatch) -> None:
    """缺失较多时重建目录，避免每次都缺一大批。"""
    insert_names(conn, [("600000", "浦发银行")])

    def fake_refresh() -> int:
        insert_names(conn, [(f"00000{i}", f"n{i}") for i in range(1, 4)])
        return 3

    monkeypatch.setattr(directory, "refresh", MagicMock(side_effect=fake_refresh))

    result = directory.get_names(["600000", "000001", "000002", "000003"])
    assert len(result) == 4


# ── MarketCapCache ──


def test_market_cap_roundtrip_and_date_isolation(cap_cache) -> None:
    assert cap_cache.get(["600000"], "2026-11-19") == {}

    cap_cache.put("2026-11-19", {"600000": 3.5e11})
    assert cap_cache.get(["600000"], "2026-11-19") == {"600000": 3.5e11}
    # 不同日期互不影响
    assert cap_cache.get(["600000"], "2026-11-20") == {}


def test_market_cap_upsert_overwrites(cap_cache) -> None:
    cap_cache.put("2026-11-19", {"600000": 1.0})
    cap_cache.put("2026-11-19", {"600000": 2.0})
    assert cap_cache.get(["600000"], "2026-11-19") == {"600000": 2.0}


def test_market_cap_empty_inputs(cap_cache) -> None:
    assert cap_cache.get([], "2026-11-19") == {}
    cap_cache.put("2026-11-19", {})  # 不应报错


# ── 海龟策略接入缓存 ──


def test_turtle_uses_market_cap_cache(conn, monkeypatch) -> None:
    """第一次调用联网取市值并落库，第二次直接命中缓存。"""
    cache = MarketCapCache(":memory:", conn=conn)
    monkeypatch.setattr("sequoia_x.strategy.turtle_trade.MarketCapCache", lambda _p: cache)

    strategy = TurtleTradeStrategy(
        engine=MagicMock(),
        settings=Settings(
            db_path="data/unused.db",
            start_date="2024-01-01",
            feishu_webhook_url="https://example.com/hook",
        ),
    )

    calls: list[list[str]] = []

    def fake_fetch(symbols: list[str], today: str) -> dict[str, float]:
        calls.append(symbols)
        return {"600000": 3.5e11}

    monkeypatch.setattr(strategy, "_fetch_market_caps", fake_fetch)

    assert strategy._get_market_caps(["600000"]) == {"600000": 3.5e11}
    assert len(calls) == 1

    # 第二次同一天：直接读缓存，不再联网
    assert strategy._get_market_caps(["600000"]) == {"600000": 3.5e11}
    assert len(calls) == 1


def test_turtle_market_caps_empty_input(monkeypatch) -> None:
    strategy = TurtleTradeStrategy(engine=MagicMock(), settings=MagicMock())
    assert strategy._get_market_caps([]) == {}
