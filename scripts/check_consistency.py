"""回测与线上一致性校验：同一交易日，两条路径必须选出同一批股票。

为什么需要
----------
回测走的是"全历史快照 + ``_signals`` 按行算"，线上走的是"最近 130 天快照 + ``ranked``
取最后一行"。两条路径一旦分叉，就会出现"回测很美、实盘不一样"，而且很难发现。

这个脚本把两条路径在同一交易日的结果逐只对比：

* **回测路径**：``_signals(full_snapshot)`` → 取最后交易日 → 按分数取前 N；
* **线上路径**：``DataEngine.get_snapshot()``（130 天窗口）→ ``strategy.run()``。

用法
----
```bash
python scripts/check_consistency.py                  # 全部 K 线策略
python scripts/check_consistency.py --strategy turtle,rps -v
```

返回码：0=全部一致；1=存在差异（可用于 CI/定时任务）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backtest as bt  # noqa: E402

from sequoia_x.core.config import get_settings  # noqa: E402
from sequoia_x.core.logger import get_logger  # noqa: E402
from sequoia_x.data.engine import DataEngine  # noqa: E402
from sequoia_x.strategy.registry import STRATEGIES  # noqa: E402

logger = get_logger(__name__)


def replay_for_date(key: str, snap, df, settings, top: int, on_date: str) -> list[str]:
    """用**回测路径**算出某个交易日的选股结果。"""
    cond, score = bt.strategy_signals(key, snap, settings)
    picks = bt.picks_by_date(df, cond, score, top)
    picks = picks[picks["date"] == on_date]
    return picks["symbol"].tolist()


def compare(backtest_picks: list[str], live_picks: list[str]) -> dict:
    """对比两批结果，返回差异明细。"""
    bt_set, live_set = set(backtest_picks), set(live_picks)
    order_only = bt_set == live_set and backtest_picks != live_picks
    return {
        "same": backtest_picks == live_picks,
        "order_only": order_only,
        "only_backtest": sorted(bt_set - live_set),
        "only_live": sorted(live_set - bt_set),
        "matched": len(bt_set & live_set),
        "n_backtest": len(backtest_picks),
        "n_live": len(live_picks),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回测与线上选股一致性校验")
    parser.add_argument("--strategy", default="all", help="逗号分隔的策略标识，all=全部")
    parser.add_argument("--top", type=int, default=0, help="每日取前 N 只（0=用 max_picks）")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印差异代码")
    args = parser.parse_args(argv)

    settings = get_settings()
    df = bt.load_history(settings.db_path)
    df = bt.attach_st_flag(df, settings.db_path)
    snap = bt.build_snapshot(df)
    last_date = str(df["date"].max())

    engine = DataEngine(settings)
    live_snap = engine.get_snapshot()
    logger.info(
        f"对比日期 {last_date}：全历史 {len(df):,} 行 / 线上快照 "
        f"{live_snap.row_count:,} 行（{live_snap.days} 个交易日）"
    )

    keys = (
        list(STRATEGIES)
        if args.strategy == "all"
        else [k.strip() for k in args.strategy.split(",") if k.strip()]
    )
    keys = [k for k in keys if k not in bt.SKIP]

    top = args.top or settings.max_picks or 0
    bad = 0
    for key in keys:
        backtest_picks = replay_for_date(key, snap, df, settings, top, last_date)
        live_picks = STRATEGIES[key](engine=engine, settings=settings).run()
        diff = compare(backtest_picks, live_picks)

        ok = diff["same"]
        if not ok:
            bad += 1
        status = "一致" if ok else ("仅顺序不同" if diff["order_only"] else "集合不同")
        logger.info(
            f"{key:<18} 回测 {diff['n_backtest']:>3} / 线上 {diff['n_live']:>3} "
            f"交集 {diff['matched']:>3} → {status}"
        )
        if not diff["same"] and args.verbose:
            if diff["only_backtest"]:
                logger.info(f"    只在回测：{', '.join(diff['only_backtest'][:10])}")
            if diff["only_live"]:
                logger.info(f"    只在线上：{', '.join(diff['only_live'][:10])}")

    if bad:
        logger.error(
            f"{bad}/{len(keys)} 个策略与线上不一致（顺序不同=浮点精度/并列排序；"
            f"集合不同=两条路径的判定条件真的分叉了，必须修）"
        )
        return 1
    logger.info(f"全部 {len(keys)} 个策略一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
