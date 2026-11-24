"""策略注册表：稳定的策略标识 → 策略类。

为什么单独一个模块
------------------
``main.py`` 之前直接 import 了 7 个策略类并硬编码一个列表，导致：

* 想"只跑某个策略"要先改代码；
* `--list-strategies` 之类的能力没法实现；
* 新增策略要改两处（import + 列表）。

现在只有这一处需要登记，``main.py`` 通过命令行按标识选择。

标识与 ``BaseStrategy.webhook_key`` **保持一致**，这样一个标识同时决定
"策略选择"和"飞书机器人路由"，不会对不上。
"""

from __future__ import annotations

from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy
from sequoia_x.strategy.reversal_bounce import ReversalBounceStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.trend_pullback import TrendPullbackStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy

#: 标识 → 策略类；字典顺序即默认执行顺序（先趋势/突破，再回踩/反转，最后事件）
STRATEGIES: dict[str, type[BaseStrategy]] = {
    MaVolumeStrategy.webhook_key: MaVolumeStrategy,
    TurtleTradeStrategy.webhook_key: TurtleTradeStrategy,
    HighTightFlagStrategy.webhook_key: HighTightFlagStrategy,
    LimitUpShakeoutStrategy.webhook_key: LimitUpShakeoutStrategy,
    UptrendLimitDownStrategy.webhook_key: UptrendLimitDownStrategy,
    RpsBreakoutStrategy.webhook_key: RpsBreakoutStrategy,
    TrendPullbackStrategy.webhook_key: TrendPullbackStrategy,
    ReversalBounceStrategy.webhook_key: ReversalBounceStrategy,
    PrivatePlacementStrategy.webhook_key: PrivatePlacementStrategy,
}

#: 需要 K 线数据的策略（`PrivatePlacement` 走 akshare，不依赖本地库）
NEEDS_MARKET_DATA: frozenset[str] = frozenset(STRATEGIES) - {"private_placement"}


def all_strategy_ids() -> list[str]:
    """返回全部策略标识（按默认执行顺序）。"""
    return list(STRATEGIES)


def resolve_strategies(spec: str | None = None) -> list[type[BaseStrategy]]:
    """把命令行里的策略描述解析成策略类列表。

    Args:
        spec: ``None`` / ``"all"`` 表示全部；也支持逗号分隔的标识列表，
            例如 ``"ma_volume,turtle"``。

    Returns:
        策略类列表（按默认执行顺序，而不是命令行里的顺序）。

    Raises:
        ValueError: 出现未知标识时抛出，错误信息里会列出可用标识。
    """
    if spec is None or spec.strip().lower() in ("", "all", "*"):
        return [STRATEGIES[key] for key in STRATEGIES]

    wanted = {item.strip().lower() for item in spec.split(",") if item.strip()}
    unknown = wanted - set(STRATEGIES)
    if unknown:
        available = ", ".join(STRATEGIES)
        raise ValueError(f"未知策略标识 {sorted(unknown)}；可用：{available}")

    return [STRATEGIES[key] for key in STRATEGIES if key in wanted]
