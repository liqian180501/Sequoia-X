"""历史回放：把过去 N 个交易日的选股"补录"进 pick_history。

为什么需要
----------
留痕是从今天开始记的，但"策略有没有效"需要历史样本。回放用**同一份策略代码**
（``_signals``）在历史行上逐日判定，把结果写进同一条留痕表，标记 ``source='replay'``，
这样：

* 立刻就能在网页「跟踪」页看到最近 60~120 天的纸面业绩；
* 不用等两个月才攒够样本；
* 回放数据与实盘留痕**可区分**（``source`` 列），不会被误当成当时的真实信号。

口径与 ``scripts/backtest.py`` 完全一致（T+1 开盘买、T+h 收盘卖、扣往返成本）。

用法
----
```bash
python scripts/replay_picks.py --days 120                 # 全部策略
python scripts/replay_picks.py --days 60 --strategy turtle,rps
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
from sequoia_x.data.ledger import SignalLedger  # noqa: E402
from sequoia_x.strategy.registry import STRATEGIES  # noqa: E402

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回放历史选股结果到 pick_history")
    parser.add_argument("--days", type=int, default=120, help="回放最近多少个交易日")
    parser.add_argument("--strategy", default="all", help="逗号分隔的策略标识，all=全部")
    parser.add_argument("--top", type=int, default=0, help="每日最多取前 N 只（0=用 max_picks）")
    args = parser.parse_args(argv)

    settings = get_settings()
    df = bt.load_history(settings.db_path)
    df = bt.attach_st_flag(df, settings.db_path)
    snap = bt.build_snapshot(df)

    dates = sorted(df["date"].unique())[-args.days :]
    since = dates[0]
    logger.info(f"回放 {len(dates)} 个交易日（{since} ~ {dates[-1]}），共 {len(df):,} 根 K 线")

    keys = (
        list(STRATEGIES)
        if args.strategy == "all"
        else [k.strip() for k in args.strategy.split(",") if k.strip()]
    )
    keys = [k for k in keys if k not in bt.SKIP]

    ledger = SignalLedger(settings.db_path)
    top = args.top or settings.max_picks or 0
    total = 0
    for key in keys:
        cond, score = bt.strategy_signals(key, snap, settings)
        picks = bt.picks_by_date(df, cond, score, top)
        picks = picks[picks["date"] >= since]
        added = 0
        for run_date, group in picks.groupby("date", sort=True):
            added += ledger.record(str(run_date), key, group["symbol"].tolist(), source="replay")
        total += added
        logger.info(f"{key:<18} 回放 {len(picks):>5} 笔，新增 {added:>5} 行")

    logger.info(f"回放完成：合计新增 {total} 行（source=replay）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
