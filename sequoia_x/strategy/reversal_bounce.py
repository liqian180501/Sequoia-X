"""超跌反弹策略：短期急跌 + 远离均线 + 当日止跌。

为什么新增这个策略
------------------
现有策略全部是"趋势/突破"型（海龟新高、旗形突破、RPS 新高、回踩、金叉），
在市场风格切换到**超跌反弹**时会集体空仓。A 股短期（1~2 周）存在显著的反转效应，
补一个均值回归型策略可以让组合在不同市场状态下都有输出。

但"超跌"本身不是买点——**接飞刀**是这类策略最常见的死法。因此要求当日出现止跌
信号（阳线或长下影），并排除封死跌停（买不到且次日惯性下跌）。

与 ``UptrendLimitDownStrategy`` 的区别：那个要求趋势完好（上升趋势中的错杀），
这个不要求趋势，只要超跌 + 止跌。
"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.filters import (
    at_limit_down,
    lower_shadow_ratio,
    pct_change,
    tradable_mask,
    volume_ratio,
)

logger = get_logger(__name__)


class ReversalBounceStrategy(BaseStrategy):
    """短期超跌 + 偏离均线 + 当日止跌。

    条件：
    1. 超跌：5 日跌幅 ≤ -15%
    2. 偏离：收盘价低于 ma20 的 85%（远离均线）
    3. 止跌：收出下影线（下影占振幅 ≥40%）且收盘高于最低价 2%
    4. 有承接：量比 ≥ 0.8（不是无量阴跌）
    5. 非封死跌停（买不到）

    Attributes:
        webhook_key: 路由到 'reversal_bounce' 专属飞书机器人。
    """

    webhook_key: str = "reversal_bounce"

    _MIN_BARS: int = 61
    _DROP_5: float = -0.15
    _DEVIATION: float = 0.85
    _SHADOW: float = 0.4
    _MIN_VOL: float = 0.8

    def run(self) -> list[str]:
        """返回满足超跌反弹条件的股票代码列表（按超跌程度降序）。"""
        snap = self.engine.get_snapshot()

        close = snap.col("close")
        ma20 = snap.rolling("close", 20)

        drop5 = pct_change(snap, 5)
        oversold = drop5 <= self._DROP_5
        far_below = close <= ma20 * self._DEVIATION

        shadow = lower_shadow_ratio(snap)
        # 止跌：收出下影线（有承接）且收盘明显脱离当日最低
        stop_falling = (shadow >= self._SHADOW) & (close > snap.col("low") * 1.02)

        alive = volume_ratio(snap) >= self._MIN_VOL

        # 打分：超跌幅度 60% + 下影线 40%
        score = (-drop5).rank(pct=True) * 0.6 + shadow.rank(pct=True) * 0.4

        selected = snap.ranked(
            tradable_mask(snap, self.settings)
            & ~at_limit_down(snap)
            & oversold
            & far_below
            & stop_falling
            & alive,
            score,
            self.settings.max_picks or None,
        )

        logger.info(f"ReversalBounceStrategy 选出 {len(selected)} 只股票")
        return selected
