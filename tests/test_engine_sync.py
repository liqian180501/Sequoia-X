"""同步 worker 的容错行为与自选股范围过滤（全部离线）。

`_bs_fetch_batch` 内部 ``import baostock``，所以这里用假模块替换 ``sys.modules``，
不需要网络、也不需要多进程（直接同步调用）。
"""

import sys
import time
import types
from types import SimpleNamespace

import pandas as pd
import pytest

from sequoia_x.data.engine import DataEngine, _bs_fetch_batch


class FakeResultSet:
    """模拟 baostock 的 ResultData。"""

    def __init__(self, rows, error_code="0", error_msg=""):
        self._rows = list(rows)
        self._index = 0
        self.error_code = error_code
        self.error_msg = error_msg

    def next(self) -> bool:
        if self._index < len(self._rows):
            self._index += 1
            return True
        return False

    def get_row_data(self) -> list[str]:
        return self._rows[self._index - 1]


class FakeBaostock(types.ModuleType):
    """可编排的假 baostock：登录结果、查询结果都可控。"""

    def __init__(self, login_results=None, query_rows=None, fail_queries=0):
        super().__init__("baostock")
        self.login_results = list(login_results or ["0"])
        self.query_rows = query_rows or ["2026-11-20", "1", "2", "3", "4", "5", "6"]
        self.fail_queries = fail_queries
        self.logins = 0
        self.logouts = 0
        self.queries = 0

    def login(self):
        code = self.login_results[min(self.logins, len(self.login_results) - 1)]
        self.logins += 1
        return SimpleNamespace(error_code=code, error_msg="" if code == "0" else "login failed")

    def logout(self):
        self.logouts += 1
        return SimpleNamespace(error_code="0", error_msg="")

    def query_history_k_data_plus(self, code, fields, **kwargs):
        self.queries += 1
        if self.queries <= self.fail_queries:
            return FakeResultSet([], error_code="10002007", error_msg="网络接收异常")
        return FakeResultSet([self.query_rows])


class FakeStopEvent:
    """替代 multiprocessing.Event。"""

    def __init__(self):
        self._flag = False

    def is_set(self) -> bool:
        return self._flag

    def set(self) -> None:
        self._flag = True


def make_items(count: int) -> list[tuple]:
    return [(f"{i:06d}", f"sz.{i:06d}", "2026-11-20", "2026-11-23") for i in range(count)]


@pytest.fixture
def fake_bs(monkeypatch):
    fake = FakeBaostock()
    monkeypatch.setitem(sys.modules, "baostock", fake)
    return fake


def test_happy_path_returns_rows(fake_bs) -> None:
    """正常路径：每只股票返回一行，计数器正确。"""
    rows, stats = _bs_fetch_batch((make_items(3), None, FakeStopEvent()))

    assert len(rows) == 3
    assert stats["ok"] == 3
    assert stats["failed"] == 0
    assert fake_bs.logins == 1
    assert fake_bs.logouts == 1


def test_login_failure_aborts_immediately(monkeypatch) -> None:
    """登录失败时不应再逐只查询（旧行为是每只都打印一行错误）。"""
    fake = FakeBaostock(login_results=["1"])
    monkeypatch.setitem(sys.modules, "baostock", fake)

    rows, stats = _bs_fetch_batch((make_items(5), None, FakeStopEvent()))

    assert rows == []
    assert stats["aborted"] == 5
    assert fake.queries == 0, "登录失败后不应发起任何查询"


def test_deadline_stops_early(fake_bs) -> None:
    """预算已过期时立即收工，返回已取到的部分结果。"""
    rows, stats = _bs_fetch_batch((make_items(4), time.monotonic() - 1, FakeStopEvent()))

    assert rows == []
    assert stats["aborted"] == 4
    assert fake_bs.queries == 0


def test_stop_event_stops_early(fake_bs) -> None:
    """其它进程发出中断信号后，本进程也应尽快退出。"""
    stop = FakeStopEvent()
    stop.set()

    rows, stats = _bs_fetch_batch((make_items(4), None, stop))

    assert rows == []
    assert stats["aborted"] == 4


def test_reconnects_after_consecutive_failures(monkeypatch) -> None:
    """连续失败达到阈值后应重连一次并继续（socket 超时后必须重连才能恢复）。"""
    fake = FakeBaostock(fail_queries=5)  # 前 5 次查询全失败
    monkeypatch.setitem(sys.modules, "baostock", fake)

    rows, stats = _bs_fetch_batch((make_items(8), None, FakeStopEvent()))

    assert stats["reconnects"] == 1, "第 5 次失败后应触发一次重连"
    assert fake.logins == 2
    assert len(rows) > 0, "重连成功后应能取到数据"


def test_gives_up_when_reconnect_fails(monkeypatch) -> None:
    """重连也失败时，应通知所有进程停止，而不是继续磨完剩余的股票。"""
    fake = FakeBaostock(login_results=["0", "1"], fail_queries=5)
    monkeypatch.setitem(sys.modules, "baostock", fake)
    stop = FakeStopEvent()

    rows, stats = _bs_fetch_batch((make_items(200), None, stop))

    assert rows == []
    assert stop.is_set(), "应通过 stop_event 通知其它进程"
    assert stats["aborted"] >= 1
    assert fake.queries <= 10, "不应继续查询剩余的一百多只股票"


# ── 自选股范围 ──


@pytest.fixture
def bare_engine() -> DataEngine:
    """绕过 __init__，只测范围过滤（不建库）。"""
    engine = object.__new__(DataEngine)
    engine.universe = None
    engine._snapshot = None
    return engine


def test_apply_universe_filters_rows(bare_engine) -> None:
    df = pd.DataFrame({"symbol": ["600000", "000001", "300750"], "close": [1.0, 2.0, 3.0]})

    assert len(bare_engine._apply_universe(df)) == 3  # 未设置范围时原样返回

    bare_engine.universe = ["000001"]
    filtered = bare_engine._apply_universe(df)
    assert filtered["symbol"].tolist() == ["000001"]


def test_set_universe_normalizes_and_dedupes(bare_engine) -> None:
    bare_engine.set_universe([" 600000", "600000", "", "000001"])

    assert bare_engine.universe == ["000001", "600000"]


def test_set_universe_empty_restores_full_market(bare_engine) -> None:
    bare_engine.set_universe(["600000"])
    bare_engine.set_universe([])

    assert bare_engine.universe is None


# ── 向前补齐的任务生成 ──


def test_build_extend_tasks_basic() -> None:
    """起点晚于目标日的股票才需要补齐，区间是 [目标日, 库内最早日-1]。"""
    rows = [("600000", "2024-01-02"), ("000001", "2016-03-01")]
    tasks = DataEngine.build_extend_tasks(rows, "2015-01-01")

    assert tasks == [
        ("600000", "sh.600000", "2015-01-01", "2024-01-01"),
        ("000001", "sz.000001", "2015-01-01", "2016-02-29"),  # 闰年
    ]


def test_build_extend_tasks_skips_already_covered() -> None:
    """已经早于目标日、或起点为空/无区间的股票应跳过。"""
    rows = [("600000", "2014-12-31"), ("000001", None), ("000002", "2015-01-01")]
    assert DataEngine.build_extend_tasks(rows, "2015-01-01") == []


def test_build_extend_tasks_empty() -> None:
    assert DataEngine.build_extend_tasks([], "2015-01-01") == []
