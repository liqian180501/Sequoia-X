"""高旗形整理策略：强动量后极度收敛缩量。"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class HighTightFlagStrategy(BaseStrategy):
    """高旗形整理策略。

    选股条件（全市场向量化，一次算完 5,224 只）：
    1. 强动量：过去40天区间最高价 / 区间最低价 > 1.6（涨幅超60%）
    2. 极度收敛：最近10天区间最高价 / 区间最低价 < 1.15（振幅低于15%）
    3. 高位抗跌：近10天最低点不低于40天最高点的 80%
    4. 缩量：今日 volume < 过去20日 volume 均值的 0.6 倍

    Attributes:
        webhook_key: 路由到 'flag' 专属飞书机器人。
    """

    webhook_key: str = "flag"

    _MIN_BARS: int = 40  # 至少需要 40 根 K 线
    _MOMENTUM_RATIO: float = 1.6
    _TIGHT_RATIO: float = 1.15
    _HIGH_LEVEL_RATIO: float = 0.8
    _SHRINK_RATIO: float = 0.6

    def run(self) -> list[str]:
        """返回满足高旗形整理条件的股票代码列表。

        Returns:
            满足条件的股票代码列表，按代码升序。
        """
        snap = self.engine.get_snapshot()

        high40 = snap.rolling("high", 40, "max")
        low40 = snap.rolling("low", 40, "min")
        high10 = snap.rolling("high", 10, "max")
        low10 = snap.rolling("low", 10, "min")
        # 前 20 日均量（不含当日），等价于旧实现的 volume.iloc[-21:-1].mean()
        vol_ma20_prev = snap.rolling_shifted("volume", 1, 20)

        momentum = high40 / low40 > self._MOMENTUM_RATIO
        consolidation = high10 / low10 < self._TIGHT_RATIO
        high_level = low10 >= high40 * self._HIGH_LEVEL_RATIO
        shrink = snap.col("volume") < vol_ma20_prev * self._SHRINK_RATIO
        # 防止除零：价格为 0 的数据视为无效
        valid = (low40 > 0) & (low10 > 0)

        selected = snap.latest(
            valid
            & momentum
            & consolidation
            & high_level
            & shrink
            & snap.enough_bars(self._MIN_BARS)
        )

        logger.info(f"HighTightFlagStrategy 选出 {len(selected)} 只股票")
        return selected
