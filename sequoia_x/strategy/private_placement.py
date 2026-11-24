"""定增"破发/接近发行价"监控。

改造要点（对应原实现的不合理点）
--------------------------------
原实现把"最近 7 天有定增公告"直接当成买入信号，这在方向上是**反的**：
定向增发通常折价发行、摊薄每股收益，公告后短期偏负面，解禁期更是压力。

真正有意义的观察点是**发行价与现价的关系**：

* 现价**跌破发行价**（破发）→ 承销商/发行人维稳动机强、后续保发行诉求高；
* 现价**接近发行价**（溢价 10% 以内）→ 安全边际相对好；
* 现价已远高于发行价（比如 +50%）→ 套利空间已兑现，不关注。

akshare 的 ``stock_qbzf_em`` 恰好同时给出 ``发行价格`` 与 ``最新价``（都是**不复权**
价格，可直接比较），所以本策略不再需要额外的行情接口。

另外：
1. 窗口 7 天 → 30 天（定增从发行到上市有间隔，7 天太短）；
2. 剔除 ST / 退市股；
3. 按"现价/发行价"升序排序（越破发越靠前），去重后取前 ``max_picks`` 只。

注意：这是**事件提醒**，不是形态选股；它不依赖本地行情库，akshare 失败时返回空列表。
"""

from datetime import date, timedelta

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class PrivatePlacementStrategy(BaseStrategy):
    """定增破发 / 接近发行价监控。

    Attributes:
        webhook_key: 路由到 'private_placement' 飞书机器人。
    """

    webhook_key: str = "private_placement"

    _LOOKBACK_DAYS: int = 30
    _MAX_PREMIUM: float = 1.10  # 现价 / 发行价 ≤ 1.10 才关注
    _REQUIRED_COLUMNS = (
        "股票代码",
        "股票简称",
        "发行方式",
        "发行价格",
        "最新价",
        "发行日期",
    )

    def run(self) -> list[str]:
        """返回近期定增且接近/跌破发行价的股票代码列表。"""
        try:
            import akshare as ak

            df = ak.stock_qbzf_em()
        except Exception as exc:
            logger.error(f"PrivatePlacementStrategy 获取定增数据失败：{exc}")
            return []

        return self._select(df)

    def _select(self, df: pd.DataFrame | None) -> list[str]:
        """从定增列表里筛出"接近/跌破发行价"的标的（纯 pandas，便于单测）。

        Args:
            df: ``akshare.stock_qbzf_em()`` 的返回值。

        Returns:
            股票代码列表（按破发程度降序），无结果时返回空列表。
        """
        if df is None or df.empty:
            logger.info("PrivatePlacementStrategy 无定增数据")
            return []

        missing = [c for c in self._REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            logger.error(f"PrivatePlacementStrategy 定增数据缺少列 {missing}，跳过")
            return []

        data = df[df["发行方式"] == "定向增发"].copy()
        if data.empty:
            return []

        # 剔除 ST / 退市
        data = data[~data["股票简称"].astype(str).str.contains("ST|退", na=False)]

        data["发行日期"] = pd.to_datetime(data["发行日期"], errors="coerce")
        for col in ("发行价格", "最新价"):
            data[col] = pd.to_numeric(data[col], errors="coerce")
        data = data.dropna(subset=["发行日期", "发行价格", "最新价"])
        data = data[(data["发行价格"] > 0) & (data["最新价"] > 0)]

        cutoff = date.today() - timedelta(days=self._LOOKBACK_DAYS)
        data = data[data["发行日期"].dt.date >= cutoff]
        if data.empty:
            logger.info("PrivatePlacementStrategy 近期无新定增")
            return []

        # 现价 / 发行价：<1 表示破发
        data["ratio"] = data["最新价"] / data["发行价格"]
        data = data[data["ratio"] <= self._MAX_PREMIUM].sort_values(
            ["ratio", "发行日期"], ascending=[True, False]
        )
        if data.empty:
            return []

        symbols: list[str] = []
        seen: set[str] = set()
        limit = self.settings.max_picks or 0
        for raw in data["股票代码"].astype(str):
            match = pd.Series([raw]).str.extract(r"(\d{6})")[0].iloc[0]
            if not isinstance(match, str) or match in seen:
                continue
            seen.add(match)
            symbols.append(match)
            if limit and len(symbols) >= limit:
                break

        logger.info(f"PrivatePlacementStrategy 选出 {len(symbols)} 只股票")
        return symbols
