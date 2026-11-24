"""涨停洗盘策略：涨停后**缩量**回踩、收盘不破位。

改造要点（对应原实现的不合理点）
--------------------------------
1. **放量 → 缩量**：原实现要求"今日成交量 > 昨日 2 倍"，这与"洗盘"的语义正好相反——
   放量下跌更像出货。洗盘的本质是**缩量**回落（浮筹被清洗、抛压衰减）。
2. **允许下探昨收**：原实现 `low >= 昨日收盘` 要求当天最低价不破昨收，而真实的
   洗盘几乎都会**下探**一下再收回。改为"盘中可以破，但收盘不破 -3%"。
3. **排除昨日一字板**：一字板涨停买不到，并非有效信号。
4. 增加趋势约束（收盘站上 ma10），避免在下跌趋势里接"假洗盘"。
5. 通过通用过滤剔除 ST / 停牌 / 次新 / 低流动性 / 今日涨停。
"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.filters import tradable_mask, volume_ratio
from sequoia_x.strategy.limits import limit_up_multiplier

logger = get_logger(__name__)


class LimitUpShakeoutStrategy(BaseStrategy):
    """昨日涨停（非一字板）→ 今日缩量回踩、收盘守住 -3%。

    Attributes:
        webhook_key: 路由到 'shakeout' 专属飞书机器人。
    """

    webhook_key: str = "shakeout"

    _MIN_BARS: int = 21
    _SHRINK: float = 0.8  # 今日量 / 昨日量
    _MAX_CLOSE_DROP: float = 0.97  # 收盘不低于昨收的 97%

    def run(self) -> list[str]:
        """返回满足涨停洗盘条件的股票代码列表（按强度降序）。"""
        snap = self.engine.get_snapshot()
        condition, score = self._signals(snap)
        selected = snap.ranked(
            condition,
            score,
            self.settings.max_picks or None,
        )

        logger.info(f"LimitUpShakeoutStrategy 选出 {len(selected)} 只股票")
        return selected

    def _signals(self, snap):
        """返回 (condition, score)：按行计算，不含"只看最后一行"的语义。

        ``run()`` 在信号上取每只股票的最后一行并排序截断；回测时用同一份逻辑
        在全部历史行上判定，保证与线上完全一致。
        """

        prev1_close = snap.shift("close", 1)
        prev2_close = snap.shift("close", 2)
        prev1_volume = snap.shift("volume", 1)
        prev1_high = snap.shift("high", 1)
        prev1_low = snap.shift("low", 1)

        # ① 昨日涨停（按板块），且不是一字板
        limit_up_yesterday = prev1_close >= prev2_close * limit_up_multiplier(snap.col("symbol"))
        not_one_word = prev1_high > prev1_low * 1.01

        # ② 今日缩量回踩：可以下探昨收，但收盘不破 -3%
        shrink = snap.col("volume") < prev1_volume * self._SHRINK
        dipped = snap.col("low") <= prev1_close
        holds = snap.col("close") >= prev1_close * self._MAX_CLOSE_DROP

        # ③ 趋势未坏：收盘仍在 10 日线上
        above_ma10 = snap.col("close") > snap.rolling("close", 10)

        # 打分：昨日涨停强度 + 今日缩量程度（越缩越好）
        limit_strength = snap.rank_pct(prev1_close / prev2_close - 1.0)
        score = limit_strength * 0.6 + snap.rank_pct(1.0 - volume_ratio(snap).clip(0, 3) / 3) * 0.4

        return (
            tradable_mask(snap, self.settings)
            & limit_up_yesterday
            & not_one_word
            & shrink
            & dipped
            & holds
            & above_ma10,
            score,
        )
