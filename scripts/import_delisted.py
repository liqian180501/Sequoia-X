"""把**退市股**的历史行情补进库，修掉回测的生存偏差。

为什么需要
----------
库里只有现在还在上市的股票（baostock 回填时按 ``status=1`` 过滤）。退市股不在样本里，
等于"回测时只看得见活下来的公司"——这是收益被系统性高估的最主要原因。

数据来源
--------
* **退市清单**：akshare ``stock_info_sh_delist``（沪）+ ``stock_info_sz_delist``（深）；
* **历史行情**：akshare ``stock_zh_a_hist(adjust="hfq")``，**后复权**，与库里 baostock 的
  后复权口径语义一致（两者的基准价不同，但同一只股票内部的收益率/均线关系等价）。

为什么不用同花顺 dump
--------------------
``daily-k`` dump 只含当前上市标的（实测 5,581 只里最后一根 K 线落后 30 天以上的只有 2 只），
拿不到退市股，所以这里退回 akshare。

用法
----
```bash
python scripts/import_delisted.py --since 2016-01-01            # 全部窗口内退市股
python scripts/import_delisted.py --limit 5 --dry-run           # 先看几只
```
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import akshare as ak
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sequoia_x.core.config import get_settings  # noqa: E402
from sequoia_x.core.logger import get_logger  # noqa: E402

logger = get_logger(__name__)

CREATE_DELISTED_SQL = """
CREATE TABLE IF NOT EXISTS delisted (
    symbol      TEXT PRIMARY KEY,
    name        TEXT,
    delist_date TEXT,
    imported_at TEXT
);
"""

UPSERT_BAR_SQL = """
INSERT INTO stock_daily (symbol, date, open, high, low, close, volume, turnover)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(symbol, date) DO UPDATE SET
    open     = excluded.open,
    high     = excluded.high,
    low      = excluded.low,
    close    = excluded.close,
    volume   = excluded.volume,
    turnover = excluded.turnover
"""

#: akshare 中文列 → 库列
COLUMN_MAP = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "turnover",
}


def load_delisted(since: str) -> pd.DataFrame:
    """取沪深两市退市清单，只保留 ``since`` 之后退市的。

    Args:
        since: ``YYYY-MM-DD``，早于该日期退市的股票不可能出现在回测窗口里。

    Returns:
        含 ``code / name / delist`` 三列的 DataFrame，按退市日升序。
    """
    sh = ak.stock_info_sh_delist().rename(
        columns={"公司代码": "code", "公司简称": "name", "暂停上市日期": "delist"}
    )
    sz = ak.stock_info_sz_delist().rename(
        columns={"证券代码": "code", "证券简称": "name", "终止上市日期": "delist"}
    )
    merged = pd.concat([sh[["code", "name", "delist"]], sz[["code", "name", "delist"]]])
    merged["delist"] = pd.to_datetime(merged["delist"], errors="coerce")
    merged["code"] = merged["code"].astype(str).str.zfill(6)
    merged = merged.dropna(subset=["delist"])
    merged = merged[merged["delist"] >= since]
    return merged.sort_values("delist").reset_index(drop=True)


def normalize(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """把 akshare 的行情表整成库里的八列格式。"""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.rename(columns=COLUMN_MAP)
    missing = [
        c for c in ("date", "open", "close", "high", "low", "volume", "turnover") if c not in out
    ]
    if missing:
        return pd.DataFrame()
    out = out[list(COLUMN_MAP.values())].copy()
    out.insert(0, "symbol", symbol)
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out = out.dropna(subset=["date", "close"])
    for col in ("open", "high", "low", "close", "volume", "turnover"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out[(out["close"] > 0) & (out["volume"] > 0)]
    return out.dropna(subset=["open", "high", "low", "close"])


def fetch_bars(code: str, start: str, end: str, attempts: int = 3) -> pd.DataFrame:
    """抓一只退市股的后复权日线，带有限退避重试（东财接口偶发 ConnectionError）。"""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            raw = ak.stock_zh_a_hist(
                symbol=code, period="daily", start_date=start, end_date=end, adjust="hfq"
            )
            return normalize(raw, code)
        except Exception as exc:  # noqa: BLE001 统一重试
            last = exc
            if attempt < attempts:
                time.sleep(2 * attempt)
    raise last  # type: ignore[misc]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="补入退市股历史（修生存偏差）")
    parser.add_argument("--since", default="2016-01-01", help="只补该日期之后退市的股票")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几只（0=全部，调试用）")
    parser.add_argument("--dry-run", action="store_true", help="只抓取不入库")
    args = parser.parse_args(argv)

    settings = get_settings()
    targets = load_delisted(args.since)
    if args.limit:
        targets = targets.head(args.limit)
    logger.info(f"待补退市股：{len(targets)} 只（{args.since} 之后退市）")

    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(CREATE_DELISTED_SQL)
        conn.commit()

    total_rows = 0
    done = 0
    failed: list[str] = []
    started = time.perf_counter()

    for index, row in enumerate(targets.itertuples(index=False), start=1):
        code, name, delist = row.code, row.name, row.delist
        try:
            bars = fetch_bars(
                code,
                args.since.replace("-", ""),
                (delist + pd.Timedelta(days=30)).strftime("%Y%m%d"),
            )
        except Exception as exc:  # 单只失败不影响整体
            logger.warning(f"{code} {name} 抓取失败：{type(exc).__name__}: {str(exc)[:60]}")
            failed.append(code)
            continue

        if bars.empty:
            logger.warning(f"{code} {name} 无行情数据，跳过")
            failed.append(code)
            continue

        if not args.dry_run:
            records = list(
                bars[
                    ["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]
                ].itertuples(index=False, name=None)
            )
            with sqlite3.connect(settings.db_path) as conn:
                conn.executemany(UPSERT_BAR_SQL, records)
                conn.execute(
                    "INSERT OR REPLACE INTO delisted (symbol, name, delist_date, imported_at) "
                    "VALUES (?, ?, ?, ?)",
                    (code, name, str(delist.date()), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                conn.commit()

        total_rows += len(bars)
        done += 1
        if index % 20 == 0 or index == len(targets):
            elapsed = time.perf_counter() - started
            logger.info(
                f"进度 {index}/{len(targets)}：成功 {done} 只 / {total_rows:,} 行，"
                f"失败 {len(failed)} 只，{elapsed:.0f}s"
            )

    logger.info(
        f"完成：{done} 只退市股、{total_rows:,} 行"
        + (f"，失败 {len(failed)} 只（{', '.join(failed[:10])}）" if failed else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
