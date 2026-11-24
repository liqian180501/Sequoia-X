"""因子评估：IC（信息系数）与分层收益。

指标
----
* **IC**：每个交易日，因子值与未来收益的**横截面 Spearman 相关系数**；
* **ICIR** = ``mean(IC) / std(IC)``——比 IC 均值更重要，衡量因子的**稳定性**；
* **t 值** = ``ICIR × √n``，粗判显著性（|t| > 2 大致可认为不为 0）；
* **分层收益**：按因子值把当日股票分成 5 组，看各组未来收益是否单调；
  单调性比 IC 更直观——"最好的 20% 比最差的 20% 多赚多少"。

收益口径与回测一致：**T+1 开盘买入，T+h 收盘卖出**。这里不扣成本（IC 衡量的是
排序能力，不是净收益），成本在回测里再算。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

#: 每个横截面至少要有这么多只股票，IC 才有意义
MIN_NAMES = 50


def add_forward_returns(df: pd.DataFrame, horizon: int = 5, price_col: str = "close") -> pd.Series:
    """算 ``close[t+h] / open[t+1] - 1``，与回测口径一致。

    Args:
        df: 按 (symbol, date) 升序的行情表，需含 ``open`` / ``close``。
        horizon: 持有交易日数。
        price_col: 卖出用的价格列。

    Returns:
        与 ``df`` 等长的 Series。
    """
    grouped = df.groupby("symbol", sort=False)
    next_open = grouped["open"].shift(-1)
    exit_price = grouped[price_col].shift(-horizon)
    return exit_price / next_open - 1.0


def cross_sectional_ic(
    factor: pd.Series,
    forward: pd.Series,
    dates: pd.Series,
    min_names: int = MIN_NAMES,
) -> pd.Series:
    """逐日横截面 Spearman IC。

    实现上先按日把两组值转成排名，再用"秩相关的 Pearson 公式"一次性向量化算完
    （避免对上千个交易日逐个 ``groupby.apply``）。

    Args:
        factor: 因子值。
        forward: 未来收益。
        dates: 日期列（与因子等长）。
        min_names: 当日有效样本下限，低于此值的日期跳过。

    Returns:
        索引为日期、值为 IC 的 Series。
    """
    frame = pd.DataFrame(
        {
            "date": pd.Series(dates).to_numpy(),
            "x": pd.Series(factor).to_numpy(),
            "y": pd.Series(forward).to_numpy(),
        }
    ).dropna()
    if frame.empty:
        return pd.Series(dtype="float64")

    frame["rx"] = frame.groupby("date")["x"].rank()
    frame["ry"] = frame.groupby("date")["y"].rank()
    frame["xy"] = frame["rx"] * frame["ry"]
    frame["x2"] = frame["rx"] ** 2
    frame["y2"] = frame["ry"] ** 2

    stats = frame.groupby("date").agg(
        n=("rx", "size"),
        sx=("rx", "sum"),
        sy=("ry", "sum"),
        sxy=("xy", "sum"),
        sxx=("x2", "sum"),
        syy=("y2", "sum"),
    )
    stats = stats[stats["n"] >= min_names]
    if stats.empty:
        return pd.Series(dtype="float64")

    n = stats["n"]
    cov = stats["sxy"] - stats["sx"] * stats["sy"] / n
    var_x = stats["sxx"] - stats["sx"] ** 2 / n
    var_y = stats["syy"] - stats["sy"] ** 2 / n
    denom = np.sqrt(var_x * var_y)
    # 分母为 0（当天所有值相同）时 IC 无定义
    ic = (cov / denom.replace(0.0, np.nan)).dropna()
    return ic


def summarize_ic(ic: pd.Series) -> dict:
    """把逐日 IC 汇总成可读的指标。

    Args:
        ic: ``cross_sectional_ic`` 的返回值。

    Returns:
        含 ``n / ic_mean / ic_std / icir / t_stat / win_rate`` 的字典（单位 %，除 n 与 t）。
    """
    if ic.empty:
        return {"n": 0}
    n = len(ic)
    mean = float(ic.mean())
    std = float(ic.std(ddof=1)) if n > 1 else float("nan")
    icir = mean / std if std and std == std else float("nan")
    return {
        "n": n,
        "ic_mean": round(mean * 100, 2),
        "ic_std": round(std * 100, 2) if std == std else None,
        "icir": round(icir, 3) if icir == icir else None,
        "t_stat": round(icir * np.sqrt(n), 2) if icir == icir else None,
        "win_rate": round(float((ic > 0).mean()) * 100, 1),
    }


def layered_returns(
    factor: pd.Series,
    forward: pd.Series,
    dates: pd.Series,
    quantiles: int = 5,
) -> pd.DataFrame:
    """按因子值把每日股票分成 N 组，返回各组的平均未来收益（%）。

    Args:
        factor: 因子值。
        forward: 未来收益。
        dates: 日期列。
        quantiles: 分组数。

    Returns:
        ``DataFrame``：索引为组号（1=因子最小），列含 ``ret`` 与 ``count``；
        若最后加一行 ``spread``，表示"最高组 − 最低组"。
    """
    frame = pd.DataFrame(
        {
            "date": pd.Series(dates).to_numpy(),
            "x": pd.Series(factor).to_numpy(),
            "y": pd.Series(forward).to_numpy(),
        }
    ).dropna()
    if frame.empty:
        return pd.DataFrame(columns=["ret", "count"])

    frame["group"] = frame.groupby("date")["x"].transform(
        lambda s: pd.qcut(s.rank(method="first"), quantiles, labels=False, duplicates="drop") + 1
    )
    grouped = frame.groupby("group")["y"]
    out = pd.DataFrame({"ret": (grouped.mean() * 100).round(3), "count": grouped.size()})
    if 1 in out.index and quantiles in out.index:
        spread = out.loc[quantiles, "ret"] - out.loc[1, "ret"]
        out.loc["spread"] = {"ret": round(float(spread), 3), "count": None}
    return out
