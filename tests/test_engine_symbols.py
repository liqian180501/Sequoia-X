"""``DataEngine.get_all_symbols()`` 的回退链测试（不联网、不建库）。"""

import pytest

from sequoia_x.data.engine import DataEngine


@pytest.fixture
def engine() -> DataEngine:
    """绕过 ``__init__``，只测代码清单的回退顺序（不碰数据库）。"""
    return object.__new__(DataEngine)


def test_uses_query_stock_basic_first(engine, monkeypatch) -> None:
    """方式 1 有结果时直接返回，不应再尝试其它方式。"""
    monkeypatch.setattr(DataEngine, "_symbols_from_basic", staticmethod(lambda: ["600000"]))
    fallback = pytest.fail  # 被调用即失败
    monkeypatch.setattr(DataEngine, "_symbols_from_all_stock", lambda self: fallback("不应回退"))

    assert engine.get_all_symbols() == ["600000"]


def test_falls_back_to_all_stock_when_basic_raises(engine, monkeypatch) -> None:
    """方式 1 抛异常（例如全市场查询超时）时，应改用 ``query_all_stock``。"""

    def boom() -> list[str]:
        raise RuntimeError("10002007 网络接收异常")

    monkeypatch.setattr(DataEngine, "_symbols_from_basic", staticmethod(boom))
    monkeypatch.setattr(DataEngine, "_symbols_from_all_stock", lambda self: ["600000", "000001"])

    assert engine.get_all_symbols() == ["600000", "000001"]


def test_falls_back_to_local_when_online_fails(engine, monkeypatch) -> None:
    """两种在线方式都拿不到时，退回本地库已有代码，保证回填可续传。"""
    monkeypatch.setattr(DataEngine, "_symbols_from_basic", staticmethod(lambda: []))
    monkeypatch.setattr(DataEngine, "_symbols_from_all_stock", lambda self: [])
    monkeypatch.setattr(DataEngine, "get_local_symbols", lambda self: ["600519"])

    assert engine.get_all_symbols() == ["600519"]


def test_returns_empty_when_everything_fails(engine, monkeypatch) -> None:
    """全部失败且本地也没有数据时，返回空列表而不是抛异常。"""

    def boom() -> list[str]:
        raise RuntimeError("timeout")

    monkeypatch.setattr(DataEngine, "_symbols_from_basic", staticmethod(boom))
    monkeypatch.setattr(DataEngine, "_symbols_from_all_stock", lambda self: [])
    monkeypatch.setattr(DataEngine, "get_local_symbols", lambda self: [])

    assert engine.get_all_symbols() == []
