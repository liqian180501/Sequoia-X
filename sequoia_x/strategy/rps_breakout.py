"""RPS 相对强度突破策略：多周期相对强度 + 真正逼近一年新高。

改造要点（对应原实现的不合理点）
--------------------------------
1. **"突破"名不副实**：原实现是 `close >= 120 日高点 × 0.90`，即"距 120 日高点
   10% 以内"——一路下跌但刚好反弹到高点下方 10% 的票也会入选。现在改成
   **距 250 日（约一年）新高 5% 以内**，这才是欧奈尔意义上的"接近新高"。
2. **单一周期太脆弱**：只用 120 日收益排名。改为 IBD 式多周期加权
   （3 个月权重 60%、12 个月 40%），兼顾"近期动量"和"长期趋势"。
3. **样本门槛自相矛盾**：原实现 `min_periods=60` 与 `shift(120)` 冲突，实际门槛
   是 121 根。现在统一按 250 日窗口的一半（125 根）作为最低样本数。
4. **Top10% 不可执行**：全市场前 10% 有 200+ 只，推送出来没法用。改为
   RS ≥ 80 分位 + 逼近新高，再按综合分取前 ``max_picks`` 只。
5. 仍不使用 ``MarketSnapshot``：它需要**完整历史**（250 个交易日）才能算排名，
   而快照窗口默认只有 130 天。这里让 SQLite 只返回每只股票的 5 个聚合值
   （约 5 千行），内存占用可以忽略。
"""

import sqlite3

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)

#: 聚合 SQL：每只股票只返回 5 个数值，而不是整表 355 万行。
#:
#: 注意：最新交易日的过滤必须写成"两个标量 MAX 子查询相等"，不能写成
#: ``EXISTS (SELECT 1 FROM stock_daily z, latest WHERE ...)`` —— 后者会让 SQLite
#: 对外层 5,224 只股票各做一次全表扫描（实测卡死数分钟）。
_RPS_AGGREGATE_SQL = """
SELECT q.* FROM (
    SELECT s.symbol,
           (SELECT t.close FROM stock_daily t
             WHERE t.symbol = s.symbol ORDER BY t.date DESC LIMIT 1)              AS close_now,
           (SELECT t.close FROM stock_daily t
             WHERE t.symbol = s.symbol ORDER BY t.date DESC LIMIT 1 OFFSET :short)
                                                                                   AS close_short,
           (SELECT t.close FROM stock_daily t
             WHERE t.symbol = s.symbol ORDER BY t.date DESC LIMIT 1 OFFSET :long)
                                                                                   AS close_long,
           (SELECT MAX(t.high) FROM (
                SELECT high FROM stock_daily u
                 WHERE u.symbol = s.symbol ORDER BY u.date DESC LIMIT :long) t)    AS high_long,
           (SELECT COUNT(*) FROM (
                SELECT 1 FROM stock_daily v
                 WHERE v.symbol = s.symbol LIMIT :long))                           AS bars_long
    FROM (SELECT DISTINCT symbol FROM stock_daily) s
) q
WHERE q.close_now IS NOT NULL
  AND q.close_short IS NOT NULL
  AND q.close_long IS NOT NULL
  AND q.bars_long >= :min_bars
  AND (SELECT MAX(date) FROM stock_daily)
      = (SELECT MAX(z.date) FROM stock_daily z WHERE z.symbol = q.symbol)
"""


class RpsBreakoutStrategy(BaseStrategy):
    """多周期 RPS + 逼近一年新高。

    选股条件：
    1. 综合 RPS ≥ ``rps_threshold``（默认 80 分位）
    2. 现价 ≥ 250 日最高价 × 0.95（距一年新高 5% 以内）
    3. 现价高于 250 日前收盘（一年正收益）

    Attributes:
        webhook_key: 路由到 'rps' 专属飞书机器人。
        rps_period: 短周期（交易日），默认 63（约 3 个月）。
        long_period: 长周期（交易日），默认 250（约 12 个月）。
        rps_threshold: 综合 RPS 阈值（0~100）。
    """

    webhook_key: str = "rps"

    rps_period: int = 63
    long_period: int = 250
    rps_threshold: int = 80
    _SHORT_WEIGHT: float = 0.6
    _NEAR_HIGH: float = 0.95

    @property
    def _min_bars(self) -> int:
        """窗口内至少需要的 K 线数（等价 pandas ``rolling`` 的 ``min_periods``）。"""
        return max(1, self.long_period // 2)

    # ── 数据获取 ──

    def _load_aggregate(self) -> pd.DataFrame:
        """用一条 SQL 取回每只股票的关键价位（只含在最新交易日有行情的股票）。

        Returns:
            列 ``symbol / close_now / close_short / close_long / high_long / bars_long``。

        Raises:
            sqlite3.Error: 数据库读取失败时向上抛出，由 ``run()`` 兜底。
        """
        with sqlite3.connect(self.engine.db_path) as conn:
            return pd.read_sql(
                _RPS_AGGREGATE_SQL,
                conn,
                params={
                    "short": int(self.rps_period),
                    "long": int(self.long_period),
                    "min_bars": int(self._min_bars),
                },
            )

    # ── 选股逻辑（纯 pandas，便于单测） ──

    def _select(self, agg: pd.DataFrame) -> list[str]:
        """在聚合结果上做多周期排名、新高判定与截断。

        Args:
            agg: ``_load_aggregate()`` 的返回值。

        Returns:
            选中的股票代码列表（按综合分降序）；无结果时返回空列表。
        """
        if agg.empty:
            return []

        valid = agg.dropna(subset=["close_now", "close_short", "close_long"]).copy()
        if valid.empty:
            return []

        # ① 多周期相对强度（横截面百分位）
        rps_short = (valid["close_now"] / valid["close_short"]).rank(pct=True) * 100
        rps_long = (valid["close_now"] / valid["close_long"]).rank(pct=True) * 100
        valid["rps"] = rps_short * self._SHORT_WEIGHT + rps_long * (1 - self._SHORT_WEIGHT)

        # ② 逼近一年新高 + 一年正收益
        near_high = valid["close_now"] >= valid["high_long"] * self._NEAR_HIGH
        positive = valid["close_now"] > valid["close_long"]

        picked = valid[(valid["rps"] >= self.rps_threshold) & near_high & positive]
        if picked.empty:
            return []

        # ③ 综合分：RPS + 距新高的接近程度
        distance = (picked["close_now"] / picked["high_long"]).clip(upper=1.0)
        score = picked["rps"] * 0.7 + distance.rank(pct=True) * 100 * 0.3
        picked = picked.assign(score=score).sort_values(
            ["score", "symbol"], ascending=[False, True]
        )

        limit = self.settings.max_picks or 0
        if limit:
            picked = picked.head(limit)
        return picked["symbol"].tolist()

    # ── 入口 ──

    def run(self) -> list[str]:
        """返回满足条件的股票代码列表（按综合分降序）。"""
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
