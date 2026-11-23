"""涨停洗盘策略：昨日涨停后今日放量收阴但不破昨收。"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.limits import limit_up_multiplier

logger = get_logger(__name__)


class LimitUpShakeoutStrategy(BaseStrategy):
    """涨停洗盘策略。

    选股条件（全市场向量化，一次算完 5,224 只）：
    1. 昨日涨停：昨日 close >= 前日 close × 涨停乘数（按板块，见 ``limits.py``）
    2. 今日收阴：今日 close < 今日 open
    3. 今日放量：今日 volume > 昨日 volume × 2.0
    4. 支撑不破：今日 low >= 昨日 close

    Attributes:
        webhook_key: 路由到 'shakeout' 专属飞书机器人。
    """

    webhook_key: str = "shakeout"

    _MIN_BARS: int = 3  # 前日、昨日、今日
    _LIMIT_UP_RATIO: float = 1.095  # 旧口径：不区分板块时的固定乘数
    _VOL_SURGE_RATIO: float = 2.0

    def run(self) -> list[str]:
        """返回满足涨停洗盘条件的股票代码列表。

        Returns:
            满足条件的股票代码列表，按代码升序。
        """
        snap = self.engine.get_snapshot()

        prev1_close = snap.shift("close", 1)  # 昨日收盘
        prev2_close = snap.shift("close", 2)  # 前日收盘
        prev1_volume = snap.shift("volume", 1)

        if self.settings.board_aware_limits:
            limit_up = limit_up_multiplier(snap.col("symbol"))
        else:
            limit_up = self._LIMIT_UP_RATIO

        limit_up_yesterday = prev1_close >= prev2_close * limit_up
        bearish_today = snap.col("close") < snap.col("open")
        volume_surge = snap.col("volume") > prev1_volume * self._VOL_SURGE_RATIO
        support_hold = snap.col("low") >= prev1_close

        selected = snap.latest(
            limit_up_yesterday
            & bearish_today
            & volume_surge
            & support_hold
            & snap.enough_bars(self._MIN_BARS)
        )

        logger.info(f"LimitUpShakeoutStrategy 选出 {len(selected)} 只股票")
        return selected
