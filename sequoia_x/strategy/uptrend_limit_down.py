"""上升趋势中的"错杀承接"策略（原"上升趋势跌停"）。

改造要点（对应原实现的不合理点）
--------------------------------
原实现是"昨天上涨趋势 + 今天放量跌停 → 买入"，这在 A 股是典型的**接飞刀**：

* 跌停当日往往**封死**，根本买不到；
* 即使买到，跌停次日惯性下跌概率很高；
* 只判断"跌幅 ≥ 9.5%/19.5%"和"放量"，完全不看**有没有资金承接**。

改造后只保留"错杀"的合理内核，并用三个条件把"飞刀"过滤掉：

1. **不买封死的跌停**：收盘价必须高于跌停价（盘中打开、有成交）；
2. **必须有承接**：收出下影线（收盘价高于最低价 3% 以上），且收盘在当日振幅
   的上半区（``close >= (high+low)/2``）；
3. **放量但趋势未破**：量比 ≥1.5，且 ma20>ma60、收盘仍在 ma60 之上。

即：把"跌停买入"改成"急跌 + 长下影 + 趋势未破"的**当日反包**信号。
"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.filters import (
    lower_shadow_ratio,
    tradable_mask,
    volume_ratio,
)
from sequoia_x.strategy.limits import limit_down_multiplier

logger = get_logger(__name__)


class UptrendLimitDownStrategy(BaseStrategy):
    """趋势中的急跌承接（未封死跌停 + 长下影 + 放量）。

    Attributes:
        webhook_key: 路由到 'limit_down' 专属飞书机器人。
    """

    webhook_key: str = "limit_down"

    _MIN_BARS: int = 61
    _DROP: float = 0.97  # 今日跌幅 ≥3%
    _VOL_SURGE: float = 1.5
    _REBOUND: float = 1.03  # 收盘高于最低价 3%

    def run(self) -> list[str]:
        """返回满足错杀承接条件的股票代码列表（按承接强度降序）。"""
        snap = self.engine.get_snapshot()
        condition, score = self._signals(snap)
        selected = snap.ranked(
            condition,
            score,
            self.settings.max_picks or None,
        )

        logger.info(f"UptrendLimitDownStrategy 选出 {len(selected)} 只股票")
        return selected

    def _signals(self, snap):
        """返回 (condition, score)：按行计算，不含"只看最后一行"的语义。

        ``run()`` 在信号上取每只股票的最后一行并排序截断；回测时用同一份逻辑
        在全部历史行上判定，保证与线上完全一致。
        """

        close = snap.col("close")
        low = snap.col("low")
        high = snap.col("high")
        prev_close = snap.shift("close", 1)

        ma20 = snap.rolling("close", 20)
        ma60 = snap.rolling("close", 60)

        # ① 趋势未破
        trend = (ma20 > ma60) & (close > ma60)

        # ② 今日急跌，但没有封死跌停（封死的买不到，且次日惯性大）
        sharp_drop = close <= prev_close * self._DROP
        not_sealed = close > prev_close * limit_down_multiplier(snap.col("symbol"))

        # ③ 有承接：下影线 + 收盘在振幅上半区
        rebound = close > low * self._REBOUND
        upper_half = close >= (high + low) / 2.0

        # ④ 放量（恐慌盘出清）
        heavy = volume_ratio(snap) >= self._VOL_SURGE

        # 打分：下影线占比 + 量比
        score = (
            lower_shadow_ratio(snap).rank(pct=True) * 0.5 + volume_ratio(snap).rank(pct=True) * 0.5
        )

        return (
            tradable_mask(snap, self.settings)
            & trend
            & sharp_drop
            & not_sealed
            & rebound
            & upper_half
            & heavy,
            score,
        )
