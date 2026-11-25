"""反转复合因子策略（**研究用，默认未注册**）：横截面选"被冷落"的股票。

> ⚠️ **实测结论：扣成本后跑输，不要默认启用。**
> 2015–2022 h5 超额 −0.17%、2023–2026 −0.12%（扣 0.2% 往返成本）。
> 原因见下文"为什么它不赚钱"。保留代码是因为它是一个干净的**因子基准**，
> 用来对照别的想法，而不是因为它是可交易策略。

为什么是反转
------------
[13 因子层](../wiki/13-factors.md) 在两个独立窗口上算了 11 个因子的 IC：

| 因子 | 2015–2022 IC | 2024–2026 IC |
|---|---:|---:|
| `vol_20`（波动率） | −5.57% | −5.69% |
| `ma_alignment`（均线多头程度） | −4.81% | −5.43% |
| `mom_20`（20 日动量） | −4.63% | −6.00% |
| `turnover_z_60`（换手异常） | −3.92% | −5.39% |

**11 个因子里 10 个显著为负**，而且两段样本方向一致。也就是说：过去十年 A 股在
5 日尺度上是**反转市**——越强势、越放量、越活跃的票，未来一周越跑输。

原来那 8 个策略几乎全部建立在"动量/突破"方向上，等于长期逆着因子走。
这个策略换到因子的正方向：**低换手、低波动、弱动量、均线走弱**。

为什么用排序而不是阈值
--------------------
形态策略需要一堆阈值（突破幅度 3%、量比 1.2…），每个阈值都是一个过拟合入口。
这里只做一件事：把四个因子的**横截面分位**加权，取最高的一批。没有绝对阈值，
换一个市场环境也不会因为"参数卡死"而完全失效。

权重来自 ICIR（越稳定权重越高），并且只在**样本内**（2015–2022）确定。

为什么它不赚钱（实测）
----------------------
分层收益（2015–2022，5 日持有）：

| 分位 | 未来 5 日收益 |
|---|---:|
| Q1（最热） | −0.169% |
| Q2 | +0.163% |
| Q3 | +0.290% |
| Q4 | +0.284% |
| Q5（最冷） | +0.249% |

因子方向是对的（最热的最差），但**最冷一档只比全市场均值高约 +0.09%**，
而往返成本是 0.2%——**毛超额不够付手续费**。

教训：**因子 IC ≠ 策略收益**。IC 衡量的是整个横截面的秩相关（由中间段主导），
而策略实际交易的是最极端的一小撮（top 30 / 4000 ≈ 0.7%），那里是尾部效应；
再加成本，+0.09% 的毛边直接归零。要变现这种量级的因子，只能靠
"更长持有（摊薄成本）"或"更低交易成本"，而不是更强的因子。
"""

from __future__ import annotations

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.data.snapshot import MarketSnapshot
from sequoia_x.factor.library import FACTORS
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.filters import tradable_mask

logger = get_logger(__name__)


class ReversalCompositeStrategy(BaseStrategy):
    """反转复合因子：选"冷门、低波、弱动量"的股票（按因子分位排序取前 N）。"""

    webhook_key = "reversal_composite"

    #: 因子权重：来自 2015–2022 样本内的 ICIR（只看样本内，避免用未来信息调权重）
    _WEIGHTS: dict[str, float] = {
        "turnover_z_60": 0.30,  # ICIR −0.358
        "vol_20": 0.30,  # ICIR −0.328
        "mom_20": 0.20,  # ICIR −0.322
        "ma_alignment": 0.20,  # ICIR −0.306
    }

    #: 参与排序所需的最少 K 线（最长窗口是 60 日）
    _MIN_BARS: int = 61

    def _signals(self, snap: MarketSnapshot) -> tuple[pd.Series, pd.Series]:
        """返回 (condition, score)：按行计算，不含"只看最后一行"的语义。

        每个因子取**反向**的横截面分位（越低越好 → 分位越高），再加权求和。
        四个因子都必须有值才参与，避免"缺一个因子但排名很高"的伪信号。
        """
        parts = []
        available = pd.Series(True, index=snap.df.index)
        for name, weight in self._WEIGHTS.items():
            raw = FACTORS[name](snap)
            # 反向：因子越小，分位越高
            ranked = snap.rank_pct(-raw)
            parts.append(ranked * weight)
            available &= ranked.notna()

        score = parts[0].fillna(0.0)
        for part in parts[1:]:
            score = score + part.fillna(0.0)

        condition = (
            tradable_mask(snap, self.settings)
            & available
            & snap.enough_bars(self._MIN_BARS)
        )
        return condition, score

    def run(self) -> list[str]:
        """返回按反转复合分降序的股票代码列表。"""
        snap = self.engine.get_snapshot()
        condition, score = self._signals(snap)
        selected = snap.ranked(
            condition,
            score,
            self.settings.max_picks or None,
        )

        logger.info(f"{type(self).__name__} 选出 {len(selected)} 只股票")
        return selected
