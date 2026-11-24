"""Sequoia-X V2 主程序入口。

常用命令
--------
```bash
python main.py                      # 日常：同步（有预算）+ 全部策略 + 飞书推送
python main.py --push-now           # ★ 立即选股并推送，跳过数据同步（约 7 秒）
python main.py --dry-run            # 只选股、不推送，用来验证
python main.py --strategy turtle,rps --push-now
python main.py --symbols 600519,000001 --dry-run   # 只看自选股
python main.py --backfill           # 首次回填 / 补历史
python main.py --list-strategies    # 列出策略标识
```

设计要点
--------
* 数据同步有**时间预算**（默认 180 s），到点用已有数据继续选股，不再出现"网络故障挂死"；
* 策略通过 ``sequoia_x.strategy.registry`` 按标识选择，新增策略只需登记一处；
* 退出码：0 正常、1 未捕获异常、2 回填有失败/未完成、130 Ctrl+C。
"""

from __future__ import annotations

import argparse
import socket
import sys
import time

from dotenv import load_dotenv

from sequoia_x.core.config import Settings, get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.registry import STRATEGIES, all_strategy_ids, resolve_strategies

# baostock 是裸 socket，不设全局超时会被半开连接挂死
socket.setdefaulttimeout(10.0)

#: 退出码
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PARTIAL = 2
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Sequoia-X V2 A 股量化选股系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python main.py --push-now\n"
            "  python main.py --dry-run --strategy ma_volume,turtle\n"
            "  python main.py --symbols 600519,000001 --dry-run\n"
            "  python main.py --backfill\n"
        ),
    )
    mode = parser.add_argument_group("运行模式")
    mode.add_argument("--backfill", action="store_true", help="回填历史 K 线（不选股、不推送）")
    mode.add_argument(
        "--push-now",
        "--no-sync",
        dest="push_now",
        action="store_true",
        help="跳过数据同步，直接用库里的数据选股并推送（秒级）",
    )
    mode.add_argument(
        "--extend-history",
        metavar="YYYY-MM-DD",
        default=None,
        help="历史起点向前补齐到该日期（只补数据，不选股；可反复运行续传）",
    )
    mode.add_argument("--dry-run", action="store_true", help="只选股、不推送")
    mode.add_argument(
        "--force",
        action="store_true",
        help="非交易日也执行同步与推送（默认非交易日直接退出）",
    )

    scope = parser.add_argument_group("选股范围")
    scope.add_argument(
        "--strategy",
        default="all",
        metavar="IDS",
        help=f"要运行的策略，逗号分隔；all=全部。可用：{', '.join(all_strategy_ids())}",
    )
    scope.add_argument(
        "--symbols",
        default=None,
        metavar="CODES",
        help="只对这些股票选股（6 位代码，逗号分隔）",
    )
    scope.add_argument(
        "--symbols-file",
        default=None,
        metavar="PATH",
        help="从文件读自选股，每行一个代码（# 开头为注释）",
    )
    scope.add_argument(
        "--no-board-limits",
        dest="board_limits",
        action="store_false",
        help="涨跌停按旧的固定 9.5%% 判定（不区分创业板/科创板/北交所）",
    )
    scope.set_defaults(board_limits=None)

    perf = parser.add_argument_group("性能")
    perf.add_argument(
        "--sync-timeout",
        type=float,
        default=None,
        metavar="SEC",
        help="本次同步的时间预算（秒），默认取配置 sync_max_seconds",
    )
    perf.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help="同步并发进程数，默认取配置 sync_workers（4）",
    )
    perf.add_argument(
        "--sync-rounds",
        type=int,
        default=1,
        metavar="N",
        help="最多分几轮同步（每轮用一次预算；停机多天后可加大以补齐，默认 1）",
    )

    info = parser.add_argument_group("信息")
    info.add_argument("--list-strategies", action="store_true", help="列出策略标识后退出")
    return parser


def _read_symbols_file(path: str) -> list[str]:
    """从文件读取自选股代码（每行一个，支持 # 注释）。"""
    symbols: list[str] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if line:
                symbols.append(line)
    return symbols


def _resolve_universe(args: argparse.Namespace) -> list[str] | None:
    """合并 --symbols 与 --symbols-file。"""
    if args.symbols_file:
        return _read_symbols_file(args.symbols_file)
    if args.symbols:
        return [item.strip() for item in args.symbols.split(",") if item.strip()]
    return None


def _print_strategies() -> None:
    """打印策略清单。"""
    print(f"{'标识':20s} {'类名':30s} {'需要K线':8s} 说明")
    print("-" * 88)
    needs_data = {"private_placement": "否"}
    for key, cls in STRATEGIES.items():
        summary = (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else ""
        print(f"{key:20s} {cls.__name__:30s} {needs_data.get(key, '是'):8s} {summary}")


def _log_plan(logger, settings: Settings, args, strategy_classes, universe) -> None:
    """启动时把关键配置打一行，方便事后对照。"""
    logger.info(
        f"配置：db={settings.db_path} 窗口={settings.snapshot_days}日 "
        f"并发={args.workers or settings.sync_workers} "
        f"同步预算={args.sync_timeout or settings.sync_max_seconds:.0f}s "
        f"板块涨跌停={'开' if args.board_limits is not False else '关'}"
    )
    names = ", ".join(cls.webhook_key for cls in strategy_classes)
    logger.info(f"策略（{len(strategy_classes)} 个）：{names}")
    if universe:
        logger.info(f"自选股范围：{len(universe)} 只")


def _run_strategies(
    strategy_classes: list[type[BaseStrategy]],
    engine: DataEngine,
    settings: Settings,
    notifier: FeishuNotifier | None,
    dry_run: bool,
) -> list[tuple[str, int, float]]:
    """依次运行策略并推送，返回 (策略名, 命中数, 耗时) 列表。"""
    logger = get_logger(__name__)
    results: list[tuple[str, int, float]] = []

    for cls in strategy_classes:
        name = cls.__name__
        logger.info(f"执行策略：{name}")
        started = time.perf_counter()
        try:
            picks = cls(engine=engine, settings=settings).run()
        except Exception as exc:  # 单个策略失败不应拖垮其它策略
            logger.exception(f"{name} 运行失败：{exc}")
            results.append((name, 0, time.perf_counter() - started))
            continue
        elapsed = time.perf_counter() - started

        if not picks:
            logger.info(f"{name} 无选股结果，跳过推送")
            results.append((name, 0, elapsed))
            continue

        if dry_run:
            logger.info(f"{name} 选出 {len(picks)} 只（dry-run，不推送）：{', '.join(picks)}")
        elif notifier is not None:
            notifier.send(
                symbols=picks,
                strategy_name=name,
                webhook_key=cls.webhook_key,
            )
        results.append((name, len(picks), elapsed))

    return results


def _print_summary(results: list[tuple[str, int, float]], total: float, dry_run: bool) -> None:
    """打印运行汇总。"""
    print()
    print("=" * 62)
    print(f"{'策略':30s} {'命中':>6s} {'耗时':>10s}")
    print("-" * 62)
    for name, count, elapsed in results:
        print(f"{name:30s} {count:6d} {elapsed:9.2f}s")
    print("-" * 62)
    print(
        f"{'合计 ' + str(len(results)) + ' 个策略':30s} "
        f"{sum(c for _, c, _ in results):6d} {total:9.2f}s"
    )
    if dry_run:
        print("（dry-run：未推送任何消息）")
    print("=" * 62)


def main(argv: list[str] | None = None) -> None:
    """程序入口。

    Args:
        argv: 命令行参数；None 表示取 ``sys.argv[1:]``（便于测试注入）。
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_strategies:
        _print_strategies()
        return

    started = time.perf_counter()
    try:
        # 把 .env 注入 os.environ：Settings 读字段直接用 .env，但
        # STRATEGY_WEBHOOK_* 的合并走的是 os.environ，所以这一步不能省
        load_dotenv(".env")
        settings = get_settings()
        if args.board_limits is not None:
            # 命令行覆盖配置（pydantic 模型默认允许赋值）
            settings.board_aware_limits = args.board_limits

        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")

        universe = _resolve_universe(args)
        try:
            strategy_classes = resolve_strategies(args.strategy)
        except ValueError as exc:
            logger.error(str(exc))
            sys.exit(EXIT_ERROR)

        engine = DataEngine(settings)
        if universe:
            engine.set_universe(universe)
        _log_plan(logger, settings, args, strategy_classes, universe)

        # ── 向前补齐历史 ──
        if args.extend_history:
            rounds = max(1, args.sync_rounds)
            for index in range(rounds):
                written = engine.extend_history(
                    args.extend_history,
                    max_seconds=args.sync_timeout,
                    workers=args.workers,
                    symbols=engine.universe,
                )
                if written == 0:
                    break
                logger.info(
                    f"第 {index + 1} 轮补齐写入 {written} 行"
                    + ("，继续下一轮..." if index + 1 < rounds else "")
                )
            logger.info(f"向前补齐结束，总耗时 {time.perf_counter() - started:.1f}s")
            return

        # ── 回填模式 ──
        if args.backfill:
            symbols = engine.universe or engine.get_all_symbols()
            if not symbols:
                logger.error("没有可回填的股票代码，退出")
                sys.exit(EXIT_ERROR)
            stats = engine.backfill(symbols, max_seconds=args.sync_timeout)
            if stats["aborted"] or stats["failed"]:
                logger.warning(f"回填未完全成功：{stats}")
                sys.exit(EXIT_PARTIAL)
            logger.info(f"回填完成，用时 {time.perf_counter() - started:.1f}s")
            return

        # ── 数据同步（可跳过）──
        if args.push_now:
            logger.info("已指定 --push-now，跳过数据同步")
        elif not args.force and not engine.is_trading_day():
            logger.info("今天不是 A 股交易日，跳过本次运行（--force 可强制）")
            return
        else:
            for round_index in range(max(1, args.sync_rounds)):
                written = engine.sync_today_bulk(
                    max_seconds=args.sync_timeout,
                    workers=args.workers,
                )
                if written == 0 or round_index + 1 >= max(1, args.sync_rounds):
                    break
                logger.info(
                    f"第 {round_index + 1} 轮同步写入 {written} 条，"
                    f"继续第 {round_index + 2} 轮补齐..."
                )

        # ── 选股 + 推送 ──
        notifier = None if args.dry_run else FeishuNotifier(settings)
        results = _run_strategies(strategy_classes, engine, settings, notifier, args.dry_run)
        total = time.perf_counter() - started
        _print_summary(results, total, args.dry_run)
        logger.info(f"Sequoia-X V2 运行完成，总耗时 {total:.1f}s")

    except KeyboardInterrupt:
        get_logger(__name__).warning("收到中断信号，退出")
        sys.exit(EXIT_INTERRUPTED)
    except SystemExit:
        raise
    except Exception:
        try:
            get_logger(__name__).exception("主流程发生未捕获异常，程序终止")
        except Exception:
            import traceback

            traceback.print_exc()
        sys.exit(EXIT_ERROR)


if __name__ == "__main__":
    main()
