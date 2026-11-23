"""海龟交易策略：20日新高突破 + 成交额过亿 + 动量阳线过滤。"""

from sequoia_x.core.logger import get_logger
from sequoia_x.data.cache import MarketCapCache
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class TurtleTradeStrategy(BaseStrategy):
    """海龟交易策略（A股防诱多改良版）。

    选股条件（全市场向量化，一次算完 5,224 只）：
    1. 突破新高：今日 close > 前20个交易日 high 的最大值
    2. 流动性：今日 turnover > 100,000,000
    3. 防诱多过滤：今日必须是实体阳线（今日 close > 今日 open），
       且必须真涨（今日 close > 昨日 close）

    最后按流通市值从大到小排序（需要额外查一次 baostock）。

    Attributes:
        webhook_key: 路由到 'turtle' 专属飞书机器人。
    """

    webhook_key: str = "turtle"

    _MIN_BARS: int = 21  # 20 日窗口 + 当日
    _MIN_TURNOVER: float = 100_000_000

    def _get_market_caps(self, symbols: list[str]) -> dict[str, float]:
        """查询候选股票的流通市值（不复权收盘价 × 流通股本），带本地缓存。

        流通股本 = 成交量 / (换手率% / 100)
        流通市值 = 流通股本 × 不复权收盘价

        市值按 ``(symbol, date)`` 缓存在本地 ``market_cap`` 表：同一天重复运行
        （调参、补推送）不再打网络；跨天则只对缓存未命中的候选查询 baostock。

        Args:
            symbols: 候选股票代码列表。

        Returns:
            ``{代码: 流通市值}``；查不到的股票不会出现（调用方按 0 处理）。
        """
        if not symbols:
            return {}

        from datetime import date

        today_str = date.today().strftime("%Y-%m-%d")
        cache = MarketCapCache(self.engine.db_path)

        caps = cache.get(symbols, today_str)
        missing = [s for s in symbols if s not in caps]
        if not missing:
            logger.info(f"流通市值命中缓存：{len(caps)} 只")
            return caps

        fetched = self._fetch_market_caps(missing, today_str)
        if fetched:
            cache.put(today_str, fetched)
            caps.update(fetched)
        return caps

    def _fetch_market_caps(self, symbols: list[str], today_str: str) -> dict[str, float]:
        """通过 baostock 逐只查询流通市值（无缓存，联网操作）。"""
        import baostock as bs

        market_caps: dict[str, float] = {}

        bs.login()
        try:
            for symbol in symbols:
                bs_code = self.engine._to_baostock_code(symbol)
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "close,volume,turn",
                    start_date=today_str,
                    end_date=today_str,
                    frequency="d",
                    adjustflag="3",  # 不复权，真实价格
                )
                while rs.next():
                    row = rs.get_row_data()
                    try:
                        close = float(row[0])
                        volume = float(row[1])
                        turn = float(row[2])
                        if turn > 0:
                            circulating_shares = volume / (turn / 100)
                            market_caps[symbol] = circulating_shares * close
                    except (ValueError, ZeroDivisionError):
                        continue
        finally:
            bs.logout()

        return market_caps

    def run(self) -> list[str]:
        """返回满足海龟突破条件的股票代码列表（按流通市值降序）。

        Returns:
            满足条件的股票代码列表。
        """
        snap = self.engine.get_snapshot()
        close = snap.col("close")

        breakout = close > snap.rolling_shifted("high", 1, 20, "max")
        liquid = snap.col("turnover") > self._MIN_TURNOVER
        # 拒绝高开低走的大阴线：实体必须是阳线，且相对昨收必须真涨
        is_yang = close > snap.col("open")
        is_up = close > snap.shift("close", 1)

        candidates = snap.latest(
            breakout & liquid & is_yang & is_up & snap.enough_bars(self._MIN_BARS)
        )

        # 按流通市值从大到小排序
        if candidates:
            market_caps = self._get_market_caps(candidates)
            candidates.sort(key=lambda s: market_caps.get(s, 0), reverse=True)

        logger.info(f"TurtleTradeStrategy 选出 {len(candidates)} 只股票")
        return candidates
