"""海龟突破策略：55 日新高 + 趋势 + 量能确认，按相对强度排序。

改造要点（对应原实现的不合理点）
--------------------------------
1. **突破窗口 20 日 → 55 日**：A 股 20 日高点噪声太大（一个月内反复创新高），
   55 个交易日（约三个月）的新高才有"突破"含义。
2. **要求"刚突破"而不是"任意突破"**：收盘价不超过前高的 5%，避免追已经远离
   突破位 20% 的票。
3. **趋势与量能双确认**：ma20>ma60 且量比 ≥1.3。原实现只有"成交额过亿"这一条
   绝对阈值，对大盘股形同虚设、对小盘股又过严。
4. **去掉流通市值排序**：按市值降序与"突破"没有任何因果关系，却要为此每天多打
   50~200 次 baostock 请求（实测 ~9 秒）。改为按"相对强度 + 量比 + 突破幅度"
   综合打分，既有依据又不联网。
5. 通过通用过滤剔除 ST / 停牌 / 次新 / 低流动性 / 今日涨停（买不到）。
"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.filters import pct_change, tradable_mask, volume_ratio

logger = get_logger(__name__)


class TurtleTradeStrategy(BaseStrategy):
    """90 日新高突破（趋势 + 量能确认，相对强度排序）。

    Attributes:
        webhook_key: 路由到 'turtle' 专属飞书机器人。
    """

    webhook_key: str = "turtle"

    _MIN_BARS: int = 91
    _BREAKOUT_WINDOW: int = 90  # 突破窗口（交易日，≈4.5 个月；实测 90 > 55 > 40）
    _MAX_BREAKOUT_MARGIN: float = 1.03  # 收盘价不超过前高的 3%（刚突破）
    _VOL_SURGE: float = 1.2

    def run(self) -> list[str]:
        """返回满足突破条件的股票代码列表（按综合强度降序）。"""
        snap = self.engine.get_snapshot()
        condition, score = self._signals(snap)
        selected = snap.ranked(
            condition,
            score,
            self.settings.max_picks or None,
        )

        logger.info(f"TurtleTradeStrategy 选出 {len(selected)} 只股票")
        return selected

    def _signals(self, snap):
        """返回 (condition, score)：按行计算，不含"只看最后一行"的语义。

        ``run()`` 在信号上取每只股票的最后一行并排序截断；回测时用同一份逻辑
        在全部历史行上判定，保证与线上完全一致。
        """

        close = snap.col("close")
        prior_high = snap.rolling_shifted("high", 1, self._BREAKOUT_WINDOW, "max")

        breakout = close > prior_high
        fresh = close <= prior_high * self._MAX_BREAKOUT_MARGIN

        ma20 = snap.rolling("close", 20)
        ma60 = snap.rolling("close", 60)
        trend = (ma20 > ma60) & (close > ma60)

        vol = volume_ratio(snap)
        volume_ok = vol >= self._VOL_SURGE

        # 综合打分：相对强度（市场分位）40% + 量比 30% + 突破幅度 30%
        relative_strength = pct_change(snap, 120).rank(pct=True)
        margin = (close / prior_high - 1.0).clip(lower=0.0)
        score = relative_strength * 0.4 + vol.rank(pct=True) * 0.3 + margin.rank(pct=True) * 0.3

        return (
            tradable_mask(snap, self.settings) & breakout & fresh & trend & volume_ok,
            score,
        )
