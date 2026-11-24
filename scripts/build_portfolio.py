"""用最近一次留痕的选股结果，构建一个带权重的组合。

与 ``main.py`` 的区别：``main.py`` 是"选股 + 推送"，本脚本是"选股之后怎么买"——
去重、定权、加约束。数据来自 ``pick_history``（P0-1 的留痕表），所以不需要重跑策略。

用法
----
```bash
python scripts/build_portfolio.py                      # 最新一天的留痕
python scripts/build_portfolio.py --date 2026-11-23 --max-weight 0.08
python scripts/build_portfolio.py --total 0.6          # 只用 60% 仓位
```
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sequoia_x.core.config import get_settings  # noqa: E402
from sequoia_x.core.logger import get_logger  # noqa: E402
from sequoia_x.data.cache import StockDirectory  # noqa: E402
from sequoia_x.data.engine import DataEngine  # noqa: E402
from sequoia_x.portfolio.builder import build, summarize  # noqa: E402

logger = get_logger(__name__)


def latest_run_date(db_path: str) -> str | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT MAX(run_date) FROM pick_history").fetchone()
    return row[0] if row else None


def load_picks(db_path: str, run_date: str) -> dict[str, list[str]]:
    """按策略分组读取留痕，组内按 rank 升序（策略内强度顺序）。"""
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT strategy, symbol FROM pick_history WHERE run_date = ? ORDER BY strategy, rank",
            (run_date,),
        ).fetchall()
    picks: dict[str, list[str]] = {}
    for strategy, symbol in rows:
        picks.setdefault(strategy, []).append(symbol)
    return picks


def load_industry(db_path: str) -> dict[str, str]:
    """如果库里有行业表就用，没有就返回空（组合层会跳过行业约束）。"""
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT symbol, industry FROM stock_industry").fetchall()
    except sqlite3.Error:
        return {}
    return {row[0]: row[1] for row in rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用留痕结果构建带权重的组合")
    parser.add_argument("--date", default=None, help="信号日；默认取最新一天")
    parser.add_argument("--max-weight", type=float, default=0.10, help="单票上限")
    parser.add_argument("--max-industry", type=float, default=0.25, help="单行业上限")
    parser.add_argument("--total", type=float, default=1.0, help="目标总仓位")
    parser.add_argument("--weighting", choices=("inv_vol", "equal"), default="inv_vol")
    args = parser.parse_args(argv)

    settings = get_settings()
    run_date = args.date or latest_run_date(settings.db_path)
    if not run_date:
        logger.error("pick_history 里没有记录，先跑一次 `python main.py --dry-run`")
        return 1

    picks = load_picks(settings.db_path, run_date)
    if not picks:
        logger.error(f"{run_date} 没有留痕")
        return 1

    engine = DataEngine(settings)
    snap = engine.get_snapshot()
    names = StockDirectory(settings.db_path).get_names(engine.get_local_symbols())
    industry = load_industry(settings.db_path)

    positions = build(
        picks,
        snap,
        max_weight=args.max_weight,
        max_industry=args.max_industry,
        industry=industry,
        weighting=args.weighting,
        total=args.total,
    )
    info = summarize(positions)

    print(
        f"\n信号日 {run_date} · 候选 {sum(len(v) for v in picks.values())} 只"
        f"（{len(picks)} 个策略）· 去重后 {info['positions']} 只"
    )
    print(
        f"总仓位 {info['gross']:.1%} · 单票最大 {info['max_weight']:.1%} · "
        f"多策略共识 {info['consensus']} 只\n"
    )
    print(f"{'代码':<8}{'名称':<10}{'权重':>7}{'年化波动':>9}  策略")
    print("-" * 72)
    for p in positions:
        name = (names.get(p.symbol) or "")[:8]
        print(f"{p.symbol:<8}{name:<10}{p.weight:>6.1%}{p.vol:>9.1%}  {', '.join(p.strategies)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
