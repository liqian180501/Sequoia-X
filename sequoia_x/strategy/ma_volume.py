"""均线+成交量选股策略：多头排列中的放量启动。

改造要点（对应原实现的不合理点）
--------------------------------
1. **不再只认"今天刚好金叉"**：原实现要求 ma5 上穿 ma20 恰好发生在今天，属于极端
   事件型信号，滞后且稀少。现在改为"多头排列（ma5>ma20>ma60）+ 近 5 日内出现过
   金叉"，既保留"趋势刚转强"的语义，又能容忍 1~2 天的确认延迟。
2. **放量基准剔除当日**：原实现用含当日的 20 日均量做分母，今天的巨量会抬高基准，
   自己削弱自己。现在用"前 20 日均量（不含当日）"。
3. **不追高**：收盘价不得高于 ma20 的 20%，避免在严重偏离均线时接力。
4. 通过通用过滤剔除 ST / 停牌 / 次新 / 低流动性 / 涨停（无法买入）。
5. 按"量比 × 20 日涨幅"打分排序，只输出前 ``max_picks`` 只。
"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.filters import pct_change, tradable_mask, volume_ratio

logger = get_logger(__name__)


class MaVolumeStrategy(BaseStrategy):
    """多头排列 + 近 3 日金叉 + 放量。

    Attributes:
        webhook_key: 路由到 'ma_volume' 专属飞书机器人。
    """

    webhook_key: str = "ma_volume"

    _MIN_BARS: int = 61  # ma60 + 当日
    _VOL_SURGE: float = 1.5  # 量比阈值
    _CROSS_LOOKBACK: int = 3  # 近 N 日内出现过金叉
    _MAX_EXTENSION: float = 1.20  # 收盘价相对 ma20 的最大偏离

    def run(self) -> list[str]:
        """返回满足条件的股票代码列表（按信号强度降序）。"""
        snap = self.engine.get_snapshot()
        condition, score = self._signals(snap)
        selected = snap.ranked(
            condition,
            score,
            self.settings.max_picks or None,
        )

        logger.info(f"MaVolumeStrategy 选出 {len(selected)} 只股票")
        return selected

    def _signals(self, snap):
        """返回 (condition, score)：按行计算，不含"只看最后一行"的语义。

        ``run()`` 在信号上取每只股票的最后一行并排序截断；回测时用同一份逻辑
        在全部历史行上判定，保证与线上完全一致。
        """

        ma5 = snap.rolling("close", 5)
        ma20 = snap.rolling("close", 20)
        ma60 = snap.rolling("close", 60)
        close = snap.col("close")

        # ① 多头排列：中期趋势向上
        trend = (ma5 > ma20) & (ma20 > ma60)

        # ② 近 N 日内出现过 ma5 上穿 ma20（而不是只认今天）
        cross = (snap.rolling_shifted("close", 1, 5) < snap.rolling_shifted("close", 1, 20)) & (
            ma5 > ma20
        )
        recent_cross = snap.rolling_series(cross.astype("float64"), self._CROSS_LOOKBACK, "max") > 0

        # ③ 放量（基准不含当日）+ 不追高
        vol = volume_ratio(snap)
        surge = vol >= self._VOL_SURGE
        not_chasing = close <= ma20 * self._MAX_EXTENSION

        # ④ 信号强度：量比 × 20 日涨幅
        score = vol * (1.0 + pct_change(snap, 20).clip(lower=-0.5))

        return (
            tradable_mask(snap, self.settings) & trend & recent_cross & surge & not_chasing,
            score,
        )
