"""扫描资金异动：连续大额流入 / 流出。

用法
----
```bash
python scripts/capital_scan.py                      # 价量代理，最近 3 日
python scripts/capital_scan.py --source dragon      # 同花顺龙虎榜
python scripts/capital_scan.py --streak 5 --min-net 100000000
```

返回码：0=正常（有无信号都是 0）；1=数据源不可用。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sequoia_x.capital.monitor import DEFAULT_MIN_NET, DEFAULT_STREAK, CapitalFlowStore, detect  # noqa: E402
from sequoia_x.capital.sources import dragon_tiger, price_volume_proxy  # noqa: E402
from sequoia_x.core.config import get_settings  # noqa: E402
from sequoia_x.core.logger import get_logger  # noqa: E402
from sequoia_x.data.cache import StockDirectory  # noqa: E402

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="资金异动扫描")
    parser.add_argument("--source", choices=("proxy", "dragon"), default="proxy")
    parser.add_argument("--streak", type=int, default=DEFAULT_STREAK, help="连续天数")
    parser.add_argument("--min-net", type=float, default=DEFAULT_MIN_NET, help="累计净额下限（元）")
    parser.add_argument("--days", type=int, default=10, help="代理模式回看交易日数")
    parser.add_argument("--top", type=int, default=15, help="每类显示多少条")
    args = parser.parse_args(argv)

    settings = get_settings()
    store = CapitalFlowStore(settings.db_path)

    if args.source == "proxy":
        flows = price_volume_proxy(settings.db_path, days=args.days)
        source = "proxy"
    else:
        flows = dragon_tiger("all")
        source = "dragon_tiger"

    if flows.empty:
        logger.error(f"没有取到数据（source={source}）")
        return 1

    store.upsert(flows, source)
    history = store.load(source)
    signals = detect(history, streak=args.streak, min_net=args.min_net)

    names = StockDirectory(settings.db_path).get_names(sorted({s.symbol for s in signals}))
    inflow = [s for s in signals if s.kind == "inflow"][: args.top]
    outflow = [s for s in signals if s.kind == "outflow"][: args.top]

    print(f"\n数据源 {source} · 连续 {args.streak} 日 · 累计净额 ≥ {args.min_net / 1e4:.0f} 万")
    print(
        f"命中 {len(signals)} 只（流入 {sum(1 for s in signals if s.kind == 'inflow')} / "
        f"流出 {sum(1 for s in signals if s.kind == 'outflow')}）\n"
    )

    for title, items in (("持续流入", inflow), ("持续流出", outflow)):
        print(f"--- {title} ---")
        if not items:
            print("  （无）")
            continue
        print(f"  {'代码':<8}{'名称':<10}{'累计净额(万)':>14}{'占成交额':>10}  最后日期")
        for s in items:
            print(
                f"  {s.symbol:<8}{(names.get(s.symbol) or '')[:8]:<10}"
                f"{s.total_net / 1e4:>14.0f}{s.ratio:>9.1%}  {s.last_date}"
            )
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
