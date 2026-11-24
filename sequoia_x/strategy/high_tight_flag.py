"""高窄旗形策略：暴涨后缩量整理，**放量突破旗面上沿**时才入选。

改造要点（对应原实现的不合理点）
--------------------------------
1. **用收盘价而不是 high/low 极值**：原实现用区间最高/最低价比值，一根插针就能
   把"旗面振幅"撑大或缩小，噪声极大。
2. **整理与突破分离**：原实现只要"40 日大涨 + 10 日窄幅 + 缩量"就买入，等于在
   整理**过程中**抢跑；旗形策略的正确买点是**突破旗面上沿并且放量**。
3. **缩量要看持续性**：原实现只看当日量 < 前 20 日均量的 60%，一天缩量说明不了
   问题。现在要求"最近 5 日均量 ≤ 之前 20 日均量的 75%"。
4. **旗杆与旗面的窗口不再重叠**：旗面用**前 10 日**（不含当日），当日专门用于
   判断突破。
5. 通过通用过滤剔除 ST / 停牌 / 次新 / 低流动性 / 今日涨停。
"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.filters import tradable_mask, volume_ratio

logger = get_logger(__name__)


class HighTightFlagStrategy(BaseStrategy):
    """旗形整理后的放量突破。

    条件（全部基于收盘价）：
    1. 旗杆：旗面之前的 50 个交易日（t-60 ~ t-11）内最高收盘 / 最低收盘 ≥ 1.6
    2. 旗面：前 10 日收盘振幅 ≤ 18%，且最低收盘不低于旗杆高点的 80%
    3. 持续缩量：最近 5 日均量 ≤ 之前 20 日均量 × 0.85
    4. 突破：近 3 日内突破前 10 日最高收盘（×1.002），
       今日仍站在旗面上沿（≥99.5%），且量 > 前 5 日均量 × 1.2

    Attributes:
        webhook_key: 路由到 'flag' 专属飞书机器人。
    """

    webhook_key: str = "flag"

    _MIN_BARS: int = 62
    _POLE_RATIO: float = 1.6  # 旗杆涨幅
    _FLAG_RANGE: float = 0.18  # 旗面振幅
    _FLAG_HOLD: float = 0.80  # 旗面最低价相对旗杆高点的下限
    _SHRINK: float = 0.85  # 5 日均量 / 前 20 日均量
    _BREAKOUT_MARGIN: float = 1.002
    _BREAKOUT_VOL: float = 1.2  # 突破日量 / 前 5 日均量

    def run(self) -> list[str]:
        """返回旗形突破的股票代码列表（按强度降序）。"""
        snap = self.engine.get_snapshot()
        condition, score = self._signals(snap)
        selected = snap.ranked(
            condition,
            score,
            self.settings.max_picks or None,
        )

        logger.info(f"HighTightFlagStrategy 选出 {len(selected)} 只股票")
        return selected

    def _signals(self, snap):
        """返回 (condition, score)：按行计算，不含"只看最后一行"的语义。

        ``run()`` 在信号上取每只股票的最后一行并排序截断；回测时用同一份逻辑
        在全部历史行上判定，保证与线上完全一致。
        """
        close = snap.col("close")

        # ① 旗杆：旗面之前那 50 个交易日（t-60 ~ t-11）的收盘涨幅
        #    注意不能把当日算进旗杆，否则"正在突破"的票其 60 日高点就是今天，
        #    会永远不满足下面"旗面低点不低于旗杆高点"的条件。
        pole_high = snap.rolling_shifted("close", 10, 50, "max")
        pole_low = snap.rolling_shifted("close", 10, 50, "min")
        pole_ratio = pole_high / pole_low

        # ② 旗面：前 10 日（不含当日）
        face_high = snap.rolling_shifted("close", 1, 10, "max")
        face_low = snap.rolling_shifted("close", 1, 10, "min")
        face_range = (face_high - face_low) / face_low

        consolidation = face_range <= self._FLAG_RANGE
        hold = face_low >= pole_high * self._FLAG_HOLD

        # ③ 持续缩量：近 5 日均量 vs 之前 20 日均量
        vol_5 = snap.rolling("volume", 5)
        vol_20_prev = snap.rolling_shifted("volume", 5, 20)
        stable_shrink = vol_5 <= vol_20_prev * self._SHRINK

        # ④ 突破旗面上沿 + 放量
        #    不要求"恰好今天突破"——旗形突破当天的量能最容易确认，但一年只会有几次；
        #    改为"近 3 日内出现过突破，且今日收盘仍站在旗面上沿之上"，
        #    这样既能覆盖突破当日，也能覆盖突破后的缩量回踩（同样是买点）。
        breakout = close > face_high * self._BREAKOUT_MARGIN
        breakout_recent = snap.rolling_series(breakout.astype("float64"), 3, "max") > 0
        still_above = close >= face_high * 0.995
        breakout_vol = (
            snap.col("volume") > snap.rolling_shifted("volume", 1, 5) * self._BREAKOUT_VOL
        )

        # 打分：旗杆强度 + 收敛程度 + 突破日量能
        score = (
            pole_ratio.rank(pct=True) * 0.4
            + (1.0 / (face_range + 0.01)).rank(pct=True) * 0.3
            + volume_ratio(snap).rank(pct=True) * 0.3
        )

        return (
            tradable_mask(snap, self.settings)
            & (pole_ratio >= self._POLE_RATIO)
            & consolidation
            & hold
            & stable_shrink
            & breakout_recent
            & still_above
            & breakout_vol,
            score,
        )
