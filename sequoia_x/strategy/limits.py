"""A 股涨跌停幅度：按板块给出判定"涨停/跌停"用的乘数。

为什么需要
----------
旧实现把阈值写死成 ``1.095`` / ``0.905``（即"较昨收涨跌 9.5% 以上"），只对主板成立。
创业板（30）与科创板（68）是 **20%**，北交所是 **30%**，于是：

* 创业板"涨 10% 的普通阳线"会被 ``LimitUpShakeout`` 误判成"昨日涨停"；
* 创业板"跌 10%"会被 ``UptrendLimitDown`` 误判成"跌停"。

本模块按代码前缀返回**乘数**（已含 0.5% 的取整容差）：

======  ==============  =============  ==============
板块    代码前缀        涨停乘数       跌停乘数
======  ==============  =============  ==============
主板    60 / 00         1.095          0.905
创业板  30              1.195          0.805
科创板  68              1.195          0.805
北交所  43/83/87/92     1.295          0.705
======  ==============  =============  ==============

主板乘数 1.095 / 0.905 与旧实现**完全一致**，所以这是"补上其它板块"，而不是改变主板行为。

关于 0.5% 容差：交易所的涨跌停价是按前收盘价 ×(1±幅度) 后**四舍五入到分**，
因此实际涨幅/跌幅会与理论值有细微偏差（可能是 9.94% 而不是 10%）。
``_TOLERANCE = 0.005`` 让判定不会被这种取整误差卡掉。

ST 股（主板 5%）无需特殊处理：5% 的涨跌幅达不到 9.5% 的门槛，
这两个策略本来就不会在 ST 股上触发（属于"安全地不选"）。
"""

from __future__ import annotations

import pandas as pd

#: 判定涨停/跌停时对"理论涨跌幅"的容差（吸收四舍五入到分的误差）
_TOLERANCE = 0.005

#: 单只股票的涨跌幅限制比例
_RATIO_BY_PREFIX: dict[str, float] = {
    "30": 0.20,  # 创业板
    "68": 0.20,  # 科创板
    "43": 0.30,  # 北交所
    "83": 0.30,
    "87": 0.30,
    "92": 0.30,
}

_DEFAULT_RATIO = 0.10  # 主板（60/00）


def limit_ratio(symbol: str) -> float:
    """返回单只股票的涨跌幅限制比例（0.10 / 0.20 / 0.30）。

    Args:
        symbol: 6 位股票代码。

    Returns:
        涨跌幅限制比例，未知前缀按主板 0.10 处理。
    """
    return _RATIO_BY_PREFIX.get(symbol[:2], _DEFAULT_RATIO)


def limit_up_multiplier(symbols: pd.Series) -> pd.Series:
    """向量化返回"涨停判定乘数"，用于 ``close_t >= close_t-1 * 乘数``。

    Args:
        symbols: 每行的股票代码列。

    Returns:
        与输入等长的 float Series（如主板 1.095、创业板 1.195）。
    """
    return _multiplier(symbols, sign=1)


def limit_down_multiplier(symbols: pd.Series) -> pd.Series:
    """向量化返回"跌停判定乘数"，用于 ``close_t <= close_t-1 * 乘数``。

    Args:
        symbols: 每行的股票代码列。

    Returns:
        与输入等长的 float Series（如主板 0.905、创业板 0.805）。
    """
    return _multiplier(symbols, sign=-1)


def _multiplier(symbols: pd.Series, sign: int) -> pd.Series:
    """按前缀映射比例，加上/减去容差后返回乘数。"""
    ratio = symbols.str[:2].map(_RATIO_BY_PREFIX).fillna(_DEFAULT_RATIO).astype("float64")
    return 1.0 + sign * (ratio - _TOLERANCE)
