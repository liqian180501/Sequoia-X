"""主程序入口属性测试。"""

import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

# 预先导入 main 模块，避免在 @given 循环中重复导入
import main as main_module


def test_parser_defaults() -> None:
    """默认参数：跑同步、跑全部策略、推送。"""
    args = main_module.build_parser().parse_args([])

    assert args.backfill is False
    assert args.push_now is False
    assert args.dry_run is False
    assert args.strategy == "all"
    assert args.symbols is None
    assert args.board_limits is None


def test_parser_push_now_aliases() -> None:
    """``--push-now`` 与 ``--no-sync`` 等价。"""
    parser = main_module.build_parser()
    assert parser.parse_args(["--push-now"]).push_now is True
    assert parser.parse_args(["--no-sync"]).push_now is True


def test_parser_scope_options() -> None:
    """选股范围相关参数能被解析。"""
    args = main_module.build_parser().parse_args(
        ["--strategy", "turtle,rps", "--symbols", "600519,000001", "--no-board-limits"]
    )

    assert args.strategy == "turtle,rps"
    assert args.symbols == "600519,000001"
    assert args.board_limits is False


def test_list_strategies_is_side_effect_free(capsys) -> None:
    """``--list-strategies`` 只打印清单，不触碰数据库与网络。"""
    main_module.main(["--list-strategies"])

    out = capsys.readouterr().out
    assert "ma_volume" in out
    assert "rps" in out


def test_unknown_strategy_exits_nonzero(capsys) -> None:
    """未知策略标识应以非 0 退出，并打印可用标识。"""
    from unittest.mock import patch

    from sequoia_x.core.config import Settings

    settings = Settings(
        db_path=":memory:",
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
    )
    with patch.object(main_module, "get_settings", return_value=settings):
        with pytest.raises(SystemExit) as exc_info:
            main_module.main(["--strategy", "nope", "--push-now"])

    assert exc_info.value.code == main_module.EXIT_ERROR
    captured = capsys.readouterr()
    assert "nope" in captured.err + captured.out


def test_read_symbols_file(tmp_path) -> None:
    """自选股文件：每行一个代码，忽略空行与 # 注释。"""
    path = tmp_path / "watch.txt"
    path.write_text("# 我的自选\n600519\n\n000001 # 平安\n", encoding="utf-8")

    assert main_module._read_symbols_file(str(path)) == ["600519", "000001"]


# Feature: sequoia-x-v2, Property 13: 主程序异常以非零退出码终止
@given(error_msg=st.text(min_size=1, max_size=100))
@h_settings(max_examples=30, deadline=None)
def test_main_exits_nonzero_on_exception(error_msg: str) -> None:
    """P13：main() 中任意未捕获异常应导致 sys.exit(1)。"""
    from unittest.mock import patch

    # 显式传空 argv，避免 pytest 自己的命令行参数影响解析
    with patch.object(main_module, "get_settings", side_effect=RuntimeError(error_msg)):
        with pytest.raises(SystemExit) as exc_info:
            main_module.main([])

        assert exc_info.value.code == main_module.EXIT_ERROR
