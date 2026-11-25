"""资金流向的三个数据源（可插拔）。

| 源 | 口径 | 何时可用 |
|---|---|---|
| ``price_volume`` | **代理**：成交额 × 涨跌方向 = 当日净流向 | 一直可用（本地库） |
| ``dragon_tiger`` | 同花顺龙虎榜：机构/游资席位买入卖出额 | 公开 API，实测可用 |
| ``akshare`` | 东财个股资金流（超大单/大单分档） | 东财网络恢复后 |

**代理指标是粗的**：它只有"涨了算流入、跌了算流出"，分不出主动买卖、也看不出大单。
它的价值是零依赖、可立刻跑通、以及和真实资金流做交叉验证——不能用它替代真实口径。

同花顺的 ``capital-flow/snapshot``（真正的超大单/大单拆解）返回 ``code=2004``：
该数据为客户端专用，公开 Key 拿不到（见 wiki 16）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

HITHINK_BASE = "https://fuyao.aicubes.cn"
DRAGON_TIGER = "/api/a-share/special-data/dragon-tiger-list"


def price_volume_proxy(db_path: str, days: int = 5) -> pd.DataFrame:
    """用价量构造资金流代理：``net_main = 成交额 × 当日涨跌幅``（money-flow 口径）。

    为什么不用 ±1 当方向：那样 ``|net|`` 恒等于成交额，"占成交额比例"永远是 100%，
    等于没有信息。乘以涨跌幅后，比例 = |涨跌幅|，才有跨股票可比性。
    注意这仍然是**代理**：它分不出主动买卖、也看不出大单，只是"资金推动强度"的近似。

    Args:
        db_path: SQLite 路径。
        days: 取最近多少个交易日。

    Returns:
        含 ``symbol / date / net_main / turnover`` 的 DataFrame。
    """
    import sqlite3

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30) as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT ?", (days + 1,)
        ).fetchall()
        if not rows:
            return pd.DataFrame()
        since = min(row[0] for row in rows)
        df = pd.read_sql(
            "SELECT symbol, date, close, turnover FROM stock_daily WHERE date >= ?",
            conn,
            params=[since],
        )
    if df.empty:
        return pd.DataFrame()

    df = df.sort_values(["symbol", "date"])
    df["prev_close"] = df.groupby("symbol")["close"].shift(1)
    df = df.dropna(subset=["prev_close"])
    pct = df["close"] / df["prev_close"] - 1.0
    df["net_main"] = df["turnover"] * pct
    return df[["symbol", "date", "net_main", "turnover"]].reset_index(drop=True)


def _api_key() -> str | None:
    """按统一来源取同花顺 Key（只读不打印）。"""
    key = os.environ.get("HITHINK_FINANCE_API_KEY", "").strip()
    if key:
        return key
    appdata = os.environ.get("APPDATA")
    candidates = []
    if appdata:
        candidates.append(Path(appdata) / "hithink-finance" / "credentials.env")
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(("HITHINK_FINANCE_API_KEY", "FUYAO_TOKEN")):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def dragon_tiger(board_type: str = "all") -> pd.DataFrame:
    """取同花顺龙虎榜（公开接口），整理成 ``symbol / date / net_main / turnover``。

    龙虎榜给的是"席位的买入额 / 卖出额"（字段 ``buy_value`` / ``sell_value``），
    净额优先用接口直接给的 ``net_value``；没有就用两者相减。

    Args:
        board_type: ``all`` / ``org``（机构榜）/ ``hot_money``（游资榜）。

    Returns:
        DataFrame；取不到时返回空表（不抛异常）。
    """
    import requests

    key = _api_key()
    if not key:
        logger.warning("没有同花顺 API Key，跳过龙虎榜")
        return pd.DataFrame()
    try:
        resp = requests.get(
            HITHINK_BASE + DRAGON_TIGER,
            params={"board_type": board_type},
            headers={"X-api-key": key},
            timeout=40,
        )
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 网络问题不算致命
        logger.warning(f"龙虎榜获取失败：{type(exc).__name__}")
        return pd.DataFrame()

    if payload.get("code") != 0:
        logger.warning(f"龙虎榜返回 code={payload.get('code')}")
        return pd.DataFrame()

    data = payload.get("data") or {}
    trade_date = data.get("trade_date") or ""
    rows = []
    for item in data.get("stock_items") or []:
        buy = float(item.get("buy_value") or 0)
        sell = float(item.get("sell_value") or 0)
        net = item.get("net_value")
        net = float(net) if net is not None else buy - sell
        symbol = str(item.get("ticker") or item.get("thscode") or "")
        rows.append(
            {
                "symbol": symbol.split(".")[0],
                "date": trade_date,
                "net_main": net,
                "net_large": buy,
                "turnover": buy + sell,
            }
        )
    if not rows:
        logger.warning("龙虎榜没有 stock_items（可能是游资榜）")
        return pd.DataFrame()
    return pd.DataFrame(rows)


def akshare_individual_flow(symbol: str) -> pd.DataFrame:
    """（插槽）东财个股资金流：超大单/大单分档的真实口径。

    东财网络恢复后启用；当前调用会抛 ``ConnectionError``，由调用方捕获。

    Args:
        symbol: 6 位代码。

    Returns:
        含 ``symbol / date / net_main / net_large / net_super / turnover`` 的 DataFrame。
    """
    import akshare as ak

    raw = ak.stock_individual_fund_flow(
        stock=symbol, market="sh" if symbol.startswith("6") else "sz"
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    rename = {
        "日期": "date",
        "收盘价": "close",
        "主力净流入-净额": "net_main",
        "大单净流入-净额": "net_large",
        "超大单净流入-净额": "net_super",
    }
    out = raw.rename(columns=rename)
    cols = [c for c in ("date", "net_main", "net_large", "net_super") if c in out.columns]
    out = out[cols].copy()
    out.insert(0, "symbol", symbol)
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out["turnover"] = None
    return out.dropna(subset=["date"])
