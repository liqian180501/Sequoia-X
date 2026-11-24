"""回测/线上一致性工具的单元测试（不读真实库、不联网）。

``scripts/check_consistency.py`` 的 ``compare()`` 负责判定"一致 / 仅顺序不同 / 集合不同"，
三种状态的严重性完全不同：顺序不同通常只是浮点并列，集合不同说明两条路径的判定条件分叉了。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_consistency import compare  # noqa: E402


def test_identical_lists() -> None:
    diff = compare(["600000", "000001"], ["600000", "000001"])
    assert diff["same"] is True
    assert diff["order_only"] is False
    assert diff["matched"] == 2


def test_order_only_difference() -> None:
    diff = compare(["600000", "000001"], ["000001", "600000"])
    assert diff["same"] is False
    assert diff["order_only"] is True
    assert diff["only_backtest"] == []
    assert diff["only_live"] == []


def test_set_divergence() -> None:
    diff = compare(["600000", "000001", "000002"], ["600000", "000003"])
    assert diff["same"] is False
    assert diff["order_only"] is False
    assert diff["only_backtest"] == ["000001", "000002"]
    assert diff["only_live"] == ["000003"]
    assert diff["matched"] == 1


def test_both_empty_is_consistent() -> None:
    diff = compare([], [])
    assert diff["same"] is True
    assert diff["n_backtest"] == 0
