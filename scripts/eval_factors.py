"""因子评估：算每个因子的 IC / ICIR / 分层收益。

回答的问题是："这些因子里，哪些真的有排序能力？"

用法
----
```bash
python scripts/eval_factors.py                       # 全部因子，5 日持有
python scripts/eval_factors.py --horizon 20          # 20 日
python scripts/eval_factors.py --factor mom_60,mom_120 --layers
python scripts/eval_factors.py --start 2024-01-02
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
from sequoia_x.factor.evaluate import (  # noqa: E402
    add_forward_returns,
    cross_sectional_ic,
    layered_returns,
    summarize_ic,
)
from sequoia_x.factor.library import FACTORS  # noqa: E402

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="因子 IC / 分层收益评估")
    parser.add_argument("--factor", default="all", help="逗号分隔的因子名，all=全部")
    parser.add_argument("--horizon", type=int, default=5, help="持有交易日数")
    parser.add_argument("--start", default="2024-01-02", help="评估起始日")
    parser.add_argument("--layers", action="store_true", help="额外打印分层收益")
    args = parser.parse_args(argv)

    settings = get_settings()
    df = bt.load_history(settings.db_path)
    df = bt.attach_st_flag(df, settings.db_path)
    df = df[df["date"] >= args.start].reset_index(drop=True)
    if df.empty:
        logger.error("指定区间没有数据")
        return 1

    logger.info(
        f"评估区间 {df['date'].min()} ~ {df['date'].max()}，"
        f"{df['symbol'].nunique():,} 只 / {len(df):,} 行，持有 {args.horizon} 日"
    )

    snap = bt.build_snapshot(df)
    forward = add_forward_returns(df, args.horizon)
    dates = df["date"]

    keys = (
        list(FACTORS)
        if args.factor == "all"
        else [k.strip() for k in args.factor.split(",") if k.strip()]
    )
    unknown = [k for k in keys if k not in FACTORS]
    if unknown:
        logger.error(f"未知因子：{unknown}")
        return 1

    print(
        f"\n{'因子':<18}{'天数':>6}{'IC均值':>9}{'IC标准差':>10}{'ICIR':>8}{'t值':>7}{'IC>0占比':>10}"
    )
    print("-" * 70)
    results = {}
    for key in keys:
        factor = FACTORS[key](snap)
        ic = cross_sectional_ic(factor, forward, dates)
        stats = summarize_ic(ic)
        results[key] = (ic, stats)
        print(
            f"{key:<18}{stats.get('n', 0):>6}{stats.get('ic_mean', 0):>9.2f}"
            f"{stats.get('ic_std') or 0:>10.2f}{stats.get('icir') or 0:>8.3f}"
            f"{stats.get('t_stat') or 0:>7.2f}{stats.get('win_rate', 0):>9.1f}%"
        )

    if args.layers:
        for key in keys:
            factor = FACTORS[key](snap)
            layers = layered_returns(factor, forward, dates)
            if layers.empty:
                continue
            print(f"\n--- {key} 分层收益（1=因子值最小，5=最大，单位 %）---")
            for group, row in layers.iterrows():
                label = "spread(5-1)" if group == "spread" else f"Q{group}"
                print(f"  {label:<12}{row['ret']:>8.2f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
