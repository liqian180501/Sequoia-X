"""全市场行情快照：一次载入 + 按股票分组安全的向量化指标。

设计动机（性能）
----------------
改造前，5 个基于 K 线的策略各自用 ``get_ohlcv(symbol)`` 逐只查库，
同一份行情被重复读取 5 遍（5,224 次查询 × 5 = 2.6 万次），实测占策略层耗时的约 75%。
本模块把"读"和"算"各做一次：

1. 读取：一条 SQL 取回最近 N 个交易日的全市场数据（实测 130 个交易日 ≈ 2.6 秒）；
2. 计算：指标按列做一次向量化运算，结果缓存在实例上，多个策略共享同一份。

设计约束（正确性）
------------------
* 数据必须按 ``(symbol, date)`` 升序，且索引是 RangeIndex；
* 任何滚动/移位都必须"按 symbol 分组"，否则会串到上一只股票的数据；
* 这里**不用** ``groupby(...).transform(...)``（每次分组都要回调进 Python，实测慢约 40 倍），
  而是先对整列做一次 C 层 ``rolling``/``shift``，再用 ``cumcount`` 把每组的头部污染行
  置为 NaN。因为数据已按 symbol 排好序，跨组污染只可能发生在每组前 ``window-1`` 行。

用法::

    snap = engine.get_snapshot()
    ma5 = snap.rolling("close", 5)
    high20 = snap.rolling_shifted("high", 1, 20, "max")
    selected = snap.latest((close > high20) & snap.enough_bars(21))
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

RollingHow = Literal["mean", "max", "min", "sum", "std"]

#: 快照必须包含的列
REQUIRED_COLUMNS: tuple[str, ...] = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
)

_SUPPORTED_HOW: frozenset[str] = frozenset({"mean", "max", "min", "sum", "std"})


class MarketSnapshot:
    """全市场最近 N 个交易日的行情快照，附带指标缓存。

    Attributes:
        df: 按 ``(symbol, date)`` 升序排列、索引为 RangeIndex 的行情数据。
        days: 载入的交易日数量。
    """

    def __init__(self, df: pd.DataFrame, days: int) -> None:
        """
        Args:
            df: 行情数据，必须包含 ``REQUIRED_COLUMNS`` 中的列，且按 (symbol, date) 升序。
            days: 该快照覆盖的交易日数量，仅用于日志与缓存键。

        Raises:
            ValueError: 缺少必需列时抛出。
        """
        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"行情快照缺少必需列: {missing}")

        self.df = df
        self.days = days
        self._cache: dict[tuple, pd.Series] = {}

        if df.empty:
            self._cumcount = np.zeros(0, dtype=np.int64)
            self._bars = np.zeros(0, dtype=np.int64)
            self._symbols: list[str] = []
        else:
            grouped = df.groupby("symbol", sort=False)
            self._cumcount = grouped.cumcount().to_numpy()
            sizes = grouped.size()
            self._bars = df["symbol"].map(sizes).to_numpy(dtype=np.int64)
            self._symbols = sizes.index.tolist()

        #: 每只股票在快照中的最后一行（即"最新一根 K 线"）
        self._is_last = self._cumcount == self._bars - 1

    # ── 基本信息 ──

    @property
    def symbol_count(self) -> int:
        """快照中的股票数量。"""
        return len(self._symbols)

    @property
    def row_count(self) -> int:
        """快照的总行数（K 线根数）。"""
        return len(self.df)

    @property
    def symbols(self) -> list[str]:
        """快照中的股票代码列表（按代码升序）。"""
        return list(self._symbols)

    def bars(self) -> pd.Series:
        """每行所属股票在快照中的 K 线总数。"""
        return pd.Series(self._bars, index=self.df.index)

    def enough_bars(self, min_bars: int) -> pd.Series:
        """标记哪些行的股票在快照内至少有 ``min_bars`` 根 K 线。

        等价于旧实现里的 ``if len(df) < _MIN_BARS: continue``。

        Args:
            min_bars: 最少 K 线根数。

        Returns:
            bool Series，与 ``df`` 等长。
        """
        key = ("enough", min_bars)
        if key not in self._cache:
            self._cache[key] = pd.Series(self._bars >= min_bars, index=self.df.index)
        return self._cache[key]

    # ── 取值 ──

    def col(self, name: str) -> pd.Series:
        """返回一列原始数据（不做任何分组处理）。

        Args:
            name: 列名。

        Returns:
            该列 Series。
        """
        if name not in self.df.columns:
            raise KeyError(f"行情快照没有列 {name!r}")
        return self.df[name]

    # ── 分组安全的运算符 ──

    def shift(self, name: str, periods: int = 1) -> pd.Series:
        """按股票分组下移 ``periods`` 行（取 N 天前的值）。

        Args:
            name: 列名。
            periods: 下移行数，必须为正数。

        Returns:
            Series；每只股票的前 ``periods`` 行为 NaN。
        """
        if periods <= 0:
            raise ValueError("periods 必须为正数")

        key = ("shift", name, periods)
        if key not in self._cache:
            out = self.col(name).shift(periods)
            out[self._cumcount < periods] = np.nan
            self._cache[key] = out
        return self._cache[key]

    def rolling(self, name: str, window: int, how: RollingHow = "mean") -> pd.Series:
        """按股票分组做 ``window`` 行滚动运算（**包含当日**）。

        Args:
            name: 列名。
            window: 窗口长度。
            how: 聚合方式，可选 ``mean`` / ``max`` / ``min`` / ``sum`` / ``std``。

        Returns:
            Series；每只股票的前 ``window - 1`` 行为 NaN。
        """
        self._check_window(window, how)

        key = ("roll", name, window, how)
        if key not in self._cache:
            out = getattr(self.col(name).rolling(window), how)()
            out[self._cumcount < window - 1] = np.nan
            self._cache[key] = out
        return self._cache[key]

    def rolling_shifted(
        self,
        name: str,
        periods: int,
        window: int,
        how: RollingHow = "mean",
    ) -> pd.Series:
        """先按股票分组下移 ``periods`` 行，再做 ``window`` 行滚动运算（**不含当日**）。

        等价于旧实现里的 ``series.shift(periods).rolling(window)...``，
        例如 ``rolling_shifted("high", 1, 20, "max")`` 表示"前 20 个交易日的最高价"。

        Args:
            name: 列名。
            periods: 下移行数，必须为正数。
            window: 窗口长度。
            how: 聚合方式。

        Returns:
            Series；每只股票的前 ``periods + window - 1`` 行为 NaN。
        """
        if periods <= 0:
            raise ValueError("periods 必须为正数")
        self._check_window(window, how)

        key = ("rollshift", name, periods, window, how)
        if key not in self._cache:
            out = getattr(self.shift(name, periods).rolling(window), how)()
            out[self._cumcount < periods + window - 1] = np.nan
            self._cache[key] = out
        return self._cache[key]

    # ── 结果提取 ──

    def latest(self, condition: pd.Series) -> list[str]:
        """在每只股票的**最后一行**上取满足条件的股票代码。

        Args:
            condition: 与 ``df`` 等长的布尔 Series。

        Returns:
            满足条件的股票代码列表，按代码升序；无结果时返回空列表。
        """
        if len(condition) != len(self.df):
            raise ValueError(f"condition 长度 {len(condition)} 与快照行数 {len(self.df)} 不一致")
        cond = np.asarray(pd.Series(condition).fillna(False), dtype=bool)
        mask = cond & self._is_last
        return self.df.loc[mask, "symbol"].tolist()

    # ── 内部工具 ──

    @staticmethod
    def _check_window(window: int, how: str) -> None:
        if window < 1:
            raise ValueError("window 必须为正整数")
        if how not in _SUPPORTED_HOW:
            raise ValueError(f"不支持的聚合方式 {how!r}，可选 {sorted(_SUPPORTED_HOW)}")

    def describe(self) -> str:
        """返回一行人类可读的摘要，用于日志。"""
        if self.df.empty:
            return f"空快照（days={self.days}）"
        first = self.df["date"].min()
        last = self.df["date"].max()
        return (
            f"{self.symbol_count} 只 / {self.row_count:,} 行 / "
            f"{self.days} 个交易日窗口（数据日期 {first} ~ {last}）"
        )
