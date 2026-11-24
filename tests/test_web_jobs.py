"""网页控制台新增能力的测试：可调参数表、后台任务管理。"""

import sys
import time
from pathlib import Path

import pytest

from sequoia_x.strategy.registry import STRATEGIES
from web.server import TUNING, App, JobManager

# ── 可调参数表 ──


def test_tuning_only_exposes_existing_attributes() -> None:
    """暴露给网页的参数必须真的存在于策略类上，否则前端改了也没用。"""
    for key, params in TUNING.items():
        assert key in STRATEGIES, f"{key} 不在注册表里"
        cls = STRATEGIES[key]
        for attr, (label, default, lo, hi) in params.items():
            assert hasattr(cls, attr), f"{cls.__name__} 没有 {attr}"
            assert label
            assert lo <= cls.__dict__.get(attr, default) <= hi, f"{attr} 默认值不在范围内"


def test_tuning_meta_matches_class_defaults() -> None:
    meta = App.tuning_meta()
    by_key = {item["key"]: item for item in meta}
    assert "rps" in by_key
    rps = {p["attr"]: p for p in by_key["rps"]["params"]}
    assert rps["rps_threshold"]["default"] == STRATEGIES["rps"].rps_threshold


def test_tuning_meta_has_bounds() -> None:
    for item in App.tuning_meta():
        for param in item["params"]:
            assert param["min"] < param["max"]
            assert param["min"] <= param["default"] <= param["max"]


# ── 后台任务 ──


def test_job_runs_to_completion(tmp_path) -> None:
    """启动一个子进程任务，轮询到 done，并能读到日志。"""
    manager = JobManager(tmp_path)
    job = manager.start("测试", [sys.executable, "-c", "print('hello')"])
    assert job["status"] == "running"

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = manager.get(job["id"])
        if current["status"] != "running":
            break
        time.sleep(0.1)

    final = manager.get(job["id"])
    assert final["status"] == "done"
    assert final["exit_code"] == 0
    assert any("hello" in line for line in manager.tail(job["id"]))


def test_job_reports_failure(tmp_path) -> None:
    manager = JobManager(tmp_path)
    job = manager.start("失败", [sys.executable, "-c", "raise SystemExit(3)"])

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if manager.get(job["id"])["status"] != "running":
            break
        time.sleep(0.1)

    final = manager.get(job["id"])
    assert final["status"] == "failed"
    assert final["exit_code"] == 3


def test_unknown_job_returns_none(tmp_path) -> None:
    assert JobManager(tmp_path).get("nope") is None


def test_public_job_hides_process_handle(tmp_path) -> None:
    manager = JobManager(tmp_path)
    job = manager.start("x", [sys.executable, "-c", "pass"])
    assert "proc" not in job
    assert {"id", "name", "status", "log"} <= set(job)


def test_log_dir_is_created(tmp_path) -> None:
    target = tmp_path / "logs" / "nested"
    JobManager(target)
    assert target.exists()
    assert Path(target).is_dir()


@pytest.fixture(autouse=True)
def _no_leftover_processes():
    yield
