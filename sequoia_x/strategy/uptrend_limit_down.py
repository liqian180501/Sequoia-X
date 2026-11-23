"""上升趋势跌停策略：趋势中放量跌停，捕捉错杀机会。"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.limits import limit_down_multiplier

logger = get_logger(__name__)


class UptrendLimitDownStrategy(BaseStrategy):
    """上升趋势跌停策略。

    选股条件（全市场向量化，一次算完 5,224 只）：
    1. 处于上升趋势：昨日20日均线 > 昨日60日均线
    2. 放量跌停：今日 close <= 昨日 close × 跌停乘数（按板块，见 ``limits.py``）
                且今日 volume > 20日均量的 2.0 倍

    趋势判定用**昨日**均线，避免今日跌停把均线拉下来导致自我否定。

    Attributes:
        webhook_key: 路由到 'limit_down' 专属飞书机器人。
    """

    webhook_key: str = "limit_down"

    _MIN_BARS: int = 61  # 60 日均线 + 昨日一行
    _LIMIT_DOWN_RATIO: float = 0.905
    _VOL_SURGE_RATIO: float = 2.0

    def run(self) -> list[str]:
        """返回满足上升趋势跌停条件的股票代码列表。

        Returns:
            满足条件的股票代码列表，按代码升序。
        """
        snap = self.engine.get_snapshot()

        ma20_prev = snap.rolling_shifted("close", 1, 20)
        ma60_prev = snap.rolling_shifted("close", 1, 60)
        vol_ma20 = snap.rolling("volume", 20)
        prev_close = snap.shift("close", 1)

        uptrend = ma20_prev > ma60_prev
        if self.settings.board_aware_limits:
            limit_down = limit_down_multiplier(snap.col("symbol"))
        else:
            limit_down = self._LIMIT_DOWN_RATIO
        volume_surge = snap.col("volume") > vol_ma20 * self._VOL_SURGE_RATIO

        is_limit_down = snap.col("close") <= prev_close * limit_down

        selected = snap.latest(
            uptrend & is_limit_down & volume_surge & snap.enough_bars(self._MIN_BARS)
        )

        logger.info(f"UptrendLimitDownStrategy 选出 {len(selected)} 只股票")
        return selected
