"""策略回测：在真实历史行情上逐日复现选股，统计持有 N 个交易日的真实收益。

口径（务必先看清，否则数字会骗人）
----------------------------------
* **信号日 T**：用 T 日收盘后的数据算信号（与线上一致）；
* **成交**：T+1 日**开盘价**买入（不假设能以 T 日收盘价成交）；
* **卖出**：T+h 日收盘价卖出，`fwd_h = close[T+h] / open[T+1] - 1`；
* **基准**：同一交易日**全市场等权**的 `fwd_h` 中位数，用来看"选股有没有超额"，
  而不是只看绝对收益（牛市里闭眼买都赚）；
* **组合收益**：每天把当日选中的股票等权，用 h=1 的非重叠收益拼净值曲线，
  据此算年化与最大回撤；
* 停牌/退市：某日没有 K 线的股票自然不会被选中，不需要额外处理；
* ST：用**当前**名称判断（存在轻微前视偏差，见 README 的已知局限）。

用法
----
```bash
python scripts/backtest.py                              # 全部策略，2024 至今
python scripts/backtest.py --strategy ma_volume,turtle   # 只看两个策略
python scripts/backtest.py --horizons 1,3,5,10 --top 30
python scripts/backtest.py --train-end 2026-01-01        # 样本内/样本外分开看
```
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sequoia_x.core.config import Settings, get_settings  # noqa: E402
from sequoia_x.data.snapshot import MarketSnapshot  # noqa: E402
from sequoia_x.strategy.registry import STRATEGIES  # noqa: E402

#: 无法回测的策略（需要 akshare 的历史公告，本地没有）
SKIP = {"private_placement"}


# ── 数据准备 ──


def load_history(db_path: str) -> pd.DataFrame:
    """载入全部日线，按 (symbol, date) 升序。"""
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql(
            "SELECT symbol,date,open,high,low,close,volume,turnover "
            "FROM stock_daily ORDER BY symbol,date",
            conn,
        )
    return df.reset_index(drop=True)


def attach_st_flag(df: pd.DataFrame, db_path: str) -> pd.DataFrame:
    """用本地股票目录标记 ST（注意：这是**当前**名称，存在轻微前视偏差）。"""
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT symbol FROM stock_info WHERE name LIKE '%ST%' OR name LIKE '%退%'"
            ).fetchall()
    except sqlite3.Error:
        rows = []
    st = {row[0] for row in rows}
    df["is_st"] = df["symbol"].isin(st) if st else False
    df["is_stale"] = False  # 回测里"某日没有 K 线"即视为停牌，由日期分组自然排除
    return df


def build_snapshot(df: pd.DataFrame) -> MarketSnapshot:
    """把整段历史包成 MarketSnapshot（_signals 是按行的，不依赖"最后一行"）。"""
    days = df["date"].nunique()
    return MarketSnapshot(df, days=days)


def add_forward_returns(df: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    """按股票分组算 fwd_h = close[t+h] / open[t+1] - 1。"""
    grouped = df.groupby("symbol", sort=False)
    next_open = grouped["open"].shift(-1)
    for h in horizons:
        df[f"fwd_{h}"] = grouped["close"].shift(-h) / next_open - 1.0
    return df


def market_forward_returns(df: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    """每个交易日的全市场等权基准收益（中位数，抗极端值）。"""
    frame = df[["date"]].copy()
    for h in horizons:
        frame[f"mkt_{h}"] = df[f"fwd_{h}"]
    return frame.groupby("date").median()


# ── 信号 ──


def rps_signals(
    snap: MarketSnapshot,
    threshold: float = 80,
    short_weight: float = 0.6,
    near_high: float = 0.95,
) -> tuple[pd.Series, pd.Series]:
    """RPS 策略的 pandas 镜像（线上走 SQL 聚合，这里逐行重算，用于回测）。

    与线上语义一致：63/250 日涨幅的横截面分位加权 → 距 250 日新高 5% 内 → 一年正收益。
    """
    close = snap.col("close")
    short = snap.shift("close", 63)
    long = snap.shift("close", 250)
    high_long = snap.rolling("high", 250, "max")

    pct_short = close / short - 1.0
    pct_long = close / long - 1.0
    by_date = snap.col("date")

    rps_short = pct_short.groupby(by_date).rank(pct=True) * 100
    rps_long = pct_long.groupby(by_date).rank(pct=True) * 100
    rps = rps_short * short_weight + rps_long * (1 - short_weight)

    valid = short.notna() & long.notna() & snap.enough_bars(125)
    cond = valid & (rps >= threshold) & (close >= high_long * near_high) & (close > long)

    distance = (close / high_long).clip(upper=1.0).groupby(by_date).rank(pct=True)
    score = rps * 0.7 + distance * 100 * 0.3
    return cond, score


def strategy_signals(key: str, snap: MarketSnapshot, settings: Settings):
    """取某个策略的 (condition, score)。"""
    if key == "rps":
        return rps_signals(snap)
    strategy = STRATEGIES[key](engine=None, settings=settings)  # type: ignore[arg-type]
    return strategy._signals(snap)


def picks_by_date(df: pd.DataFrame, cond: pd.Series, score: pd.Series, top: int) -> pd.DataFrame:
    """每个交易日按分数取前 top 只，返回 (date, symbol)。"""
    mask = np.asarray(pd.Series(cond).fillna(False), dtype=bool)
    frame = pd.DataFrame(
        {
            "symbol": df["symbol"].to_numpy()[mask],
            "date": df["date"].to_numpy()[mask],
            "score": pd.Series(score).to_numpy()[mask],
        }
    )
    frame = frame.sort_values(["date", "score", "symbol"], ascending=[True, False, True])
    if top:
        frame = frame.groupby("date", sort=True).head(top)
    return frame[["date", "symbol"]]


# ── 评估 ──


def evaluate(
    picks: pd.DataFrame,
    df: pd.DataFrame,
    market: pd.DataFrame,
    horizons: list[int],
    cost: float = 0.0,
) -> dict:
    """把选股结果与真实收益对齐，算出各项指标。

    Args:
        cost: 单次往返交易成本（小数），从每笔收益里扣除（佣金+印花税+滑点）。
    """
    merged = picks.merge(
        df[["symbol", "date"] + [f"fwd_{h}" for h in horizons]],
        on=["symbol", "date"],
        how="left",
    )
    result: dict = {
        "days": int(picks["date"].nunique()),
        "trades": int(len(picks)),
        "per_day": round(len(picks) / max(picks["date"].nunique(), 1), 1),
    }
    for h in horizons:
        col = f"fwd_{h}"
        net = merged[col] - cost
        valid = net.dropna()
        if valid.empty:
            continue
        bench = merged["date"].map(market[f"mkt_{h}"])
        excess = (net - bench).dropna()
        result[f"h{h}_mean"] = valid.mean() * 100
        result[f"h{h}_win"] = (valid > 0).mean() * 100
        result[f"h{h}_exc"] = excess.mean() * 100
        result[f"h{h}_n"] = len(valid)

    # 日频净值（h=1，非重叠）：每天等权持有到次日
    merged["net_1"] = merged["fwd_1"] - cost
    daily = merged.dropna(subset=["net_1"]).groupby("date")["net_1"].mean()
    if len(daily) > 1:
        equity = (1 + daily).cumprod()
        result["total"] = (equity.iloc[-1] - 1) * 100
        years = len(daily) / 244.0
        result["annual"] = (equity.iloc[-1] ** (1 / years) - 1) * 100 if years > 0 else float("nan")
        peak = equity.cummax()
        result["mdd"] = ((equity / peak) - 1).min() * 100
        std = daily.std()
        result["sharpe"] = daily.mean() / std * np.sqrt(244) if std and std > 0 else float("nan")
    return result


def print_table(rows: list[tuple[str, dict]], horizons: list[int]) -> None:
    """打印结果表。"""
    head = f"{'策略':<18}{'天数':>5}{'笔数':>7}{'日均':>6}"
    for h in horizons:
        head += f"{'h' + str(h):>8}{'胜率':>7}{'超额':>8}"
    head += f"{'年化':>9}{'回撤':>8}{'夏普':>7}"
    print(head)
    print("-" * len(head))
    for name, m in rows:
        line = f"{name:<18}{m.get('days', 0):>5}{m.get('trades', 0):>7}{m.get('per_day', 0):>6.1f}"
        for h in horizons:
            line += (
                f"{m.get(f'h{h}_mean', float('nan')):>8.2f}"
                f"{m.get(f'h{h}_win', float('nan')):>7.1f}"
                f"{m.get(f'h{h}_exc', float('nan')):>8.2f}"
            )
        line += (
            f"{m.get('annual', float('nan')):>9.1f}"
            f"{m.get('mdd', float('nan')):>8.1f}"
            f"{m.get('sharpe', float('nan')):>7.2f}"
        )
        print(line)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X 策略回测")
    parser.add_argument("--strategy", default="all", help="逗号分隔的策略标识，all=全部")
    parser.add_argument("--horizons", default="1,3,5,10", help="持有天数，逗号分隔")
    parser.add_argument("--top", type=int, default=30, help="每日最多取前 N 只（0=不限）")
    parser.add_argument(
        "--cost",
        type=float,
        default=0.002,
        help="单次往返交易成本（默认 0.2%%：佣金+印花税+滑点）",
    )
    parser.add_argument("--start", default=None, help="回测起始日 YYYY-MM-DD")
    parser.add_argument(
        "--out",
        default=None,
        help="把结果写成 JSON（供网页控制台读取），例如 data/backtest_summary.json",
    )
    parser.add_argument(
        "--train-end",
        default=None,
        help="样本内截止日；给了就把结果拆成「样本内 / 样本外」两块",
    )
    args = parser.parse_args(argv)

    horizons = [int(x) for x in args.horizons.split(",")]
    settings = get_settings()
    db_path = settings.db_path

    print(f"载入行情：{db_path}")
    df = load_history(db_path)
    df = attach_st_flag(df, db_path)
    df = add_forward_returns(df, horizons)
    market = market_forward_returns(df, horizons)
    snap = build_snapshot(df)
    print(
        f"{len(df):,} 行 / {df['symbol'].nunique():,} 只 / {df['date'].min()} ~ {df['date'].max()}"
    )

    keys = (
        list(STRATEGIES)
        if args.strategy == "all"
        else [k.strip() for k in args.strategy.split(",") if k.strip()]
    )
    keys = [k for k in keys if k not in SKIP]

    # 先算好每个策略的 picks，再按时间窗切分评估
    all_picks: dict[str, pd.DataFrame] = {}
    for key in keys:
        cond, score = strategy_signals(key, snap, settings)
        all_picks[key] = picks_by_date(df, cond, score, args.top)
        print(f"  {key:<18} 全部历史选股 {len(all_picks[key]):>7} 笔")

    start = args.start or str(df["date"].min())
    records: list[dict] = []

    def run_block(label: str, lo: str, hi: str | None) -> None:
        print(f"\n===== {label}（{lo} ~ {hi or '最新'}）=====")
        rows = []
        for key in keys:
            picks = all_picks[key]
            picks = picks[picks["date"] >= lo]
            if hi:
                picks = picks[picks["date"] < hi]
            metrics = evaluate(picks, df, market, horizons, cost=args.cost)
            rows.append((key, metrics))
            records.append(
                {
                    "strategy": key,
                    "block": label,
                    "trades": metrics.get("trades", 0),
                    "days": metrics.get("days", 0),
                    "h1_exc": metrics.get("h1_exc"),
                    "h5_exc": metrics.get("h5_exc"),
                    "annual": metrics.get("annual"),
                    "mdd": metrics.get("mdd"),
                    "sharpe": metrics.get("sharpe"),
                }
            )
        print_table(rows, horizons)

    # 基准：全市场等权
    bench = market.loc[market.index >= start]
    print(f"\n已扣除往返成本 {args.cost * 100:.2f}%")
    print("基准（全市场等权中位数）:")
    for h in horizons:
        print(f"  h={h}: 平均 {bench[f'mkt_{h}'].mean() * 100:.2f}%")

    if args.train_end:
        run_block("样本内", start, args.train_end)
        run_block("样本外", args.train_end, None)
    else:
        run_block("全样本", start, None)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        meta = (
            f"{df['date'].min()} ~ {df['date'].max()} · "
            f"{df['symbol'].nunique()} 只 · 扣成本 {args.cost * 100:.2f}% · "
            f"每日前 {args.top} 只"
        )
        out_path.write_text(
            json.dumps({"meta": meta, "rows": records}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"\n结果已写入 {out_path}")


if __name__ == "__main__":
    main()
