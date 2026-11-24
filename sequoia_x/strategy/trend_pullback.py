"""趋势回踩策略：强势股缩量回踩 20 日线。

为什么新增这个策略
------------------
原有策略清一色是"追突破"（海龟新高、旗形突破、均线金叉放量）。A 股实践中，
**突破买入的胜率对时点极其敏感**（容易被假突破洗掉），而"强势股缩量回踩不破"
是胜率/盈亏比通常更稳的一类买点：

* 中期趋势已经确立（60 日涨幅 ≥20%，ma20 > ma60）；
* 当前只是**回调**而非反转：缩量、且收盘守在 ma20 附近；
* 风险点明确：跌破 ma20 -2% 或 ma60 就止损。

它与 ``TurtleTradeStrategy``（追新高）互为补充：一个买强势延续，一个买强势回调。
"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.filters import pct_change, tradable_mask, volume_ratio

logger = get_logger(__name__)


class TrendPullbackStrategy(BaseStrategy):
    """强势趋势中缩量回踩 20 日线。

    条件：
    1. 趋势：ma20 > ma60 且收盘 > ma60
    2. 强势：60 日涨幅 ≥ 15%
    3. 回踩：最低价触及 ma20 上方 2% 以内，且收盘不低于 ma20 的 97%
    4. 不放量：量比 ≤ 0.9（回调没有明显放量，说明不是出货）

    Attributes:
        webhook_key: 路由到 'trend_pullback' 专属飞书机器人。
    """

    webhook_key: str = "trend_pullback"

    _MIN_BARS: int = 61
    _MIN_GAIN_60: float = 0.15
    _TOUCH: float = 1.02
    _HOLD: float = 0.97
    _SHRINK: float = 0.9

    def run(self) -> list[str]:
        """返回满足回踩条件的股票代码列表（按强势程度降序）。"""
        snap = self.engine.get_snapshot()

        close = snap.col("close")
        low = snap.col("low")
        ma20 = snap.rolling("close", 20)
        ma60 = snap.rolling("close", 60)

        trend = (ma20 > ma60) & (close > ma60)
        gain60 = pct_change(snap, 60)
        strong = gain60 >= self._MIN_GAIN_60

        # 盘中回踩到 20 日线附近，但收盘站住
        touched = (low <= ma20 * self._TOUCH) & (close >= ma20 * self._HOLD)

        vol = volume_ratio(snap)
        shrink = vol <= self._SHRINK

        # 打分：中期涨幅 60% + 缩量程度 40%
        score = gain60.rank(pct=True) * 0.6 + (1.0 - vol.clip(0, 2) / 2).rank(pct=True) * 0.4

        selected = snap.ranked(
            tradable_mask(snap, self.settings) & trend & strong & touched & shrink,
            score,
            self.settings.max_picks or None,
        )

        logger.info(f"TrendPullbackStrategy 选出 {len(selected)} 只股票")
        return selected
