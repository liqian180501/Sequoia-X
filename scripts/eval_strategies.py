"""策略条件评估：入选票 vs 未入选票，过滤器到底有没有增量？

用法
----
```bash
python scripts/eval_strategies.py                       # 全部策略，5 日持有
python scripts/eval_strategies.py --horizon 20
python scripts/eval_strategies.py --strategy turtle,rps --start 2024-01-02
```
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
from sequoia_x.factor.conditional import binary_ic, conditional_stats  # noqa: E402
from sequoia_x.factor.evaluate import add_forward_returns, summarize_ic  # noqa: E402
from sequoia_x.strategy.filters import tradable_mask  # noqa: E402
from sequoia_x.strategy.registry import STRATEGIES  # noqa: E402

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="策略入选条件的条件 IC 评估")
    parser.add_argument("--strategy", default="all", help="逗号分隔的策略标识，all=全部")
    parser.add_argument("--horizon", type=int, default=5, help="持有交易日数")
    parser.add_argument("--start", default="2024-01-02", help="评估起始日")
    args = parser.parse_args(argv)

    settings = get_settings()
    df = bt.load_history(settings.db_path)
    df = bt.attach_st_flag(df, settings.db_path)
    df = df[df["date"] >= args.start].reset_index(drop=True)
    snap = bt.build_snapshot(df)
    forward = add_forward_returns(df, args.horizon)
    dates = df["date"]
    # 基准必须用同一个可交易池：否则 ST/低流动性/涨停的票会污染"未入选"组
    universe = tradable_mask(snap, settings)

    logger.info(
        f"区间 {df['date'].min()} ~ {df['date'].max()}，{len(df):,} 行，持有 {args.horizon} 日"
    )

    keys = (
        list(STRATEGIES)
        if args.strategy == "all"
        else [k.strip() for k in args.strategy.split(",") if k.strip()]
    )
    keys = [k for k in keys if k not in bt.SKIP]

    print(
        f"\n{'策略':<18}{'天数':>5}{'日均':>6}{'入选%':>8}{'未入选%':>9}{'价差%':>8}"
        f"{'t值':>7}{'胜率':>7}{'条件IC':>8}"
    )
    print("-" * 78)
    for key in keys:
        try:
            cond, _score = bt.strategy_signals(key, snap, settings)
        except Exception as exc:  # 单个策略失败不影响其它
            logger.warning(f"{key} 计算失败：{type(exc).__name__}: {exc}")
            continue

        stats = conditional_stats(cond, forward, dates, universe=universe)
        if not stats.get("days"):
            print(f"{key:<18}{'—':>5}  样本不足")
            continue

        ic = summarize_ic(binary_ic(cond & universe, forward, dates))
        coverage = round(stats["picks_per_day"] / max(snap.symbol_count, 1) * 100, 2)
        print(
            f"{key:<18}{stats['days']:>5}{stats['picks_per_day']:>6.1f}"
            f"{stats['pick_mean']:>9.2f}{stats['nonpick_mean']:>9.2f}"
            f"{stats['spread']:>8.2f}{stats.get('spread_t') or 0:>7.2f}"
            f"{stats['win_rate']:>6.1f}%{ic.get('ic_mean', 0):>8.2f}"
        )
        print(
            f"{'':<18}{'覆盖率':<8}{coverage}%  （日均 {stats['picks_per_day']:.0f} 只 / "
            f"{snap.symbol_count} 只）"
        )

    print("\n判读：价差 > 0 且 |t| > 2 说明过滤器有增量；价差 ≈ 0 说明选不选都一样。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
