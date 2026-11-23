"""RPS 相对强度突破策略：120 日涨幅排名前 10% 且接近 120 日高点。

与其余策略不同，本策略需要每只股票的**完整历史**（120 个交易日之前的价格），
因此不能使用 ``MarketSnapshot``（默认只有 130 个交易日的窗口，对长期停牌股会少算，
导致横截面排名出现偏差）。这里改为让 SQLite 用索引做"每股取最后 121 根 K 线"的
聚合，只把 3 个数值（现价、120 日前收盘、120 日最高价）带回 pandas：

    10.5 s / 440 MB  →  1.5 s / 0.4 MB（实测，选股结果完全一致）

剩下的事情（横截面百分位排名、突破判定）在 5,224 行的小表上用 pandas 完成。
"""

import sqlite3

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)

#: 聚合 SQL：每只股票只返回 4 个聚合值，而不是整表 355 万行。
#: ``LIMIT :period OFFSET :period`` 取"倒数第 period+1 行"的收盘价（即 period 个交易日前）。
#:
#: 注意：最新交易日的过滤必须写成"两个标量 MAX 子查询相等"，**不能**写成
#: ``EXISTS (SELECT 1 FROM stock_daily z, latest WHERE ...)`` —— 后者会让 SQLite
#: 对外层 5,224 只股票各做一次全表扫描（实测卡死数分钟）。当前写法 1.5 s。
_RPS_AGGREGATE_SQL = """
SELECT q.* FROM (
    SELECT s.symbol,
           (SELECT t.close FROM stock_daily t
             WHERE t.symbol = s.symbol ORDER BY t.date DESC LIMIT 1)                AS close_now,
           (SELECT t.close FROM stock_daily t
             WHERE t.symbol = s.symbol ORDER BY t.date DESC LIMIT 1 OFFSET :period) AS close_period,
           (SELECT MAX(t.high) FROM (
                SELECT high FROM stock_daily u
                 WHERE u.symbol = s.symbol ORDER BY u.date DESC LIMIT :period) t)   AS roll_high,
           (SELECT COUNT(*) FROM (
                SELECT 1 FROM stock_daily v
                 WHERE v.symbol = s.symbol LIMIT :period))                      AS bars_in_window
    FROM (SELECT DISTINCT symbol FROM stock_daily) s
) q
WHERE q.close_period IS NOT NULL
  AND q.bars_in_window >= :min_bars
  AND (SELECT MAX(date) FROM stock_daily)
      = (SELECT MAX(z.date) FROM stock_daily z WHERE z.symbol = q.symbol)
"""


class RpsBreakoutStrategy(BaseStrategy):
    """RPS 极强动量突破策略。

    选股条件（先 SQL 聚合，再在 5,224 行的小表上向量化）：
    1. 120 日涨幅排名前 10%（横截面百分位 ``rank(pct=True) * 100 >= 90``）
    2. 现价不低于 120 日最高价的 90%

    注意"突破"是宽松口径：只要距 120 日高点 10% 以内即可，并不要求创出新高。

    Attributes:
        webhook_key: 路由到 'rps' 专属飞书机器人。
        rps_period: 回看窗口（交易日）。
        rps_threshold: RPS 阈值（0~100）。
    """

    webhook_key: str = "rps"

    rps_period: int = 120
    rps_threshold: int = 90
    _BREAKOUT_RATIO: float = 0.90

    @property
    def _min_bars(self) -> int:
        """窗口内至少需要的 K 线数（等价 pandas ``rolling`` 的 ``min_periods``）。"""
        return max(1, self.rps_period // 2)

    # ── 数据获取 ──

    def _load_aggregate(self) -> pd.DataFrame:
        """用一条 SQL 取回每只股票的现价、period 日前收盘、period 日最高价。

        Returns:
            列 ``symbol / close_now / close_period / roll_high / bars_in_window``
            的 DataFrame，仅包含"在最新交易日有行情"的股票。

        Raises:
            sqlite3.Error: 数据库读取失败时向上抛出，由 ``run()`` 兜底。
        """
        with sqlite3.connect(self.engine.db_path) as conn:
            return pd.read_sql(
                _RPS_AGGREGATE_SQL,
                conn,
                params={
                    "period": int(self.rps_period),
                    "min_bars": int(self._min_bars),
                },
            )

    # ── 选股逻辑（纯 pandas，便于单测） ──

    def _select(self, agg: pd.DataFrame) -> list[str]:
        """在聚合结果上做横截面排名与突破判定。

        Args:
            agg: ``_load_aggregate()`` 的返回值。

        Returns:
            选中的股票代码列表；无结果时返回空列表。
        """
        if agg.empty:
            return []

        # 涨幅算不出来的股票（历史不足 121 根）不参与排名
        valid = agg[agg["close_period"].notna()].copy()
        if valid.empty:
            return []

        valid["pct_change"] = (valid["close_now"] - valid["close_period"]) / valid["close_period"]
        valid["rps"] = valid["pct_change"].rank(pct=True) * 100

        strong = valid[valid["rps"] >= self.rps_threshold]
        # 窗口内样本不足时滚动最高价无意义（等价旧实现的 min_periods）
        strong = strong[strong["bars_in_window"] >= self._min_bars]
        selected = strong[strong["close_now"] >= strong["roll_high"] * self._BREAKOUT_RATIO]

        return selected["symbol"].tolist()

    # ── 入口 ──

    def run(self) -> list[str]:
        """返回满足 RPS 突破条件的股票代码列表。

        Returns:
            满足条件的股票代码列表，按代码升序；出错时返回空列表。
        """
        try:
            agg = self._load_aggregate()
        except Exception as exc:  # 数据库异常不应中断其他策略
            logger.error(f"RpsBreakoutStrategy 读取数据库失败：{exc}")
            return []

        selected = self._select(agg)

        # 自选股模式：排名仍在**全市场**上算（30 只里排第一没有意义），只过滤输出
        universe = getattr(self.engine, "universe", None)
        if universe:
            allowed = set(universe)
            selected = [symbol for symbol in selected if symbol in allowed]

        logger.info(f"RpsBreakoutStrategy 选出 {len(selected)} 只股票")
        return selected
