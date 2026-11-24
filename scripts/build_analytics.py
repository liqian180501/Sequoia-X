"""构建 / 查询 DuckDB 分析库，并和 SQLite 做性能对比。

用法
----
```bash
python scripts/build_analytics.py --rebuild            # 从 SQLite 拷贝（4M 行）
python scripts/build_analytics.py --from-dump          # 载入同花顺 dump（10M 行）
python scripts/build_analytics.py --stats
python scripts/build_analytics.py --query "SELECT count(*) FROM bars"
python scripts/build_analytics.py --benchmark          # SQLite vs DuckDB
```
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from sequoia_x.core.config import get_settings  # noqa: E402
from sequoia_x.core.logger import get_logger  # noqa: E402
from sequoia_x.data.analytics import AnalyticsStore  # noqa: E402

logger = get_logger(__name__)

#: 基准查询：一个典型的分析型负载（全表扫描 + 分组聚合 + 百分位）
BENCH_SQL = """
SELECT date,
       COUNT(*)                                        AS n,
       MEDIAN(close / prev_close - 1)                  AS median_ret,
       SUM(CASE WHEN close > prev_close THEN 1 ELSE 0 END) AS up_count
FROM (
    SELECT date, close,
           LAG(close) OVER (PARTITION BY symbol ORDER BY date) AS prev_close
    FROM bars WHERE date >= '2024-01-02'
)
WHERE prev_close IS NOT NULL
GROUP BY date
ORDER BY date
"""


def bench_sqlite(db_path: str) -> tuple[float, int]:
    """在 SQLite 上跑同一份聚合（用 pandas 补 SQLite 没有的 MEDIAN）。"""
    started = time.perf_counter()
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30) as conn:
        df = pd.read_sql(
            "SELECT symbol, date, close FROM stock_daily WHERE date >= '2024-01-02'", conn
        )
    df = df.sort_values(["symbol", "date"])
    df["prev_close"] = df.groupby("symbol")["close"].shift(1)
    df = df.dropna(subset=["prev_close"])
    g = df.groupby("date")
    out = pd.DataFrame(
        {
            "n": g.size(),
            "median_ret": g.apply(lambda d: (d["close"] / d["prev_close"] - 1).median()),
        }
    )
    return time.perf_counter() - started, len(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DuckDB 分析库")
    parser.add_argument("--rebuild", action="store_true", help="从 SQLite 重建 bars 表")
    parser.add_argument("--from-dump", action="store_true", help="载入 Parquet dump")
    parser.add_argument("--dump", default="data/dumps/daily_k_full.parquet")
    parser.add_argument("--stats", action="store_true", help="打印库概况")
    parser.add_argument("--query", default=None, help="执行任意 SQL")
    parser.add_argument("--benchmark", action="store_true", help="SQLite vs DuckDB 对比")
    parser.add_argument("--db", default="data/analytics.duckdb")
    args = parser.parse_args(argv)

    settings = get_settings()
    store = AnalyticsStore(args.db)
    try:
        if args.rebuild:
            store.build_from_sqlite(settings.db_path)
        if args.from_dump:
            path = Path(args.dump)
            if not path.exists():
                logger.error(f"找不到 {path}，先跑 scripts/fetch_dump.py --kind daily-k")
                return 1
            store.build_from_parquet(str(path))

        if args.stats or not any((args.rebuild, args.from_dump, args.query, args.benchmark)):
            info = store.stats()
            print(f"分析库：{info['path']}")
            for name, rows in info["tables"].items():
                print(f"  {name:<12}{rows:>12,} 行")
            if not info["tables"]:
                print("  （空库，先跑 --rebuild）")

        if args.query:
            df = store.query(args.query)
            print(df.to_string(index=False, max_rows=100))

        if args.benchmark:
            started = time.perf_counter()
            df = store.query(BENCH_SQL)
            duck = time.perf_counter() - started
            sqlite_time, n = bench_sqlite(settings.db_path)
            print("\n同一份——每日涨跌统计（2024 年起，按日分组 + 中位数）：")
            print(f"  SQLite + pandas : {sqlite_time:6.2f}s（{n} 个交易日）")
            print(f"  DuckDB          : {duck:6.2f}s（{len(df)} 个交易日）")
            if duck > 0:
                print(f"  加速            : {sqlite_time / duck:.1f}×")
            print(df.head(3).to_string(index=False))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
