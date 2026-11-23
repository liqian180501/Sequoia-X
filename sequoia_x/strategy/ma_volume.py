"""均线+成交量选股策略：5日均线上穿20日均线且成交量放大。"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class MaVolumeStrategy(BaseStrategy):
    """均线+成交量选股策略。

    选股条件（全市场向量化，一次算完 5,224 只）：
    1. 5日收盘均线上穿20日收盘均线（金叉）
    2. 当日成交量 > 20日均量的 1.5 倍（放量确认）

    实现上只对"每只股票的最后一行"判定，见 ``MarketSnapshot.latest``。

    Attributes:
        webhook_key: 路由到 'ma_volume' 专属飞书机器人。
    """

    webhook_key: str = "ma_volume"

    _MIN_BARS: int = 21  # 20 日均线 + 昨日一行（金叉需要昨日均线）
    _VOL_SURGE_RATIO: float = 1.5

    def run(self) -> list[str]:
        """返回满足均线金叉+放量条件的股票代码列表。

        Returns:
            满足条件的股票代码列表，按代码升序。
        """
        snap = self.engine.get_snapshot()

        ma5 = snap.rolling("close", 5)
        ma20 = snap.rolling("close", 20)
        # 昨日均线：等价于 ma5 / ma20 各自 shift(1)
        ma5_prev = snap.rolling_shifted("close", 1, 5)
        ma20_prev = snap.rolling_shifted("close", 1, 20)
        vol_ma20 = snap.rolling("volume", 20)

        golden_cross = (ma5_prev < ma20_prev) & (ma5 > ma20)
        volume_surge = snap.col("volume") > vol_ma20 * self._VOL_SURGE_RATIO

        selected = snap.latest(golden_cross & volume_surge & snap.enough_bars(self._MIN_BARS))

        logger.info(f"MaVolumeStrategy 选出 {len(selected)} 只股票")
        return selected
