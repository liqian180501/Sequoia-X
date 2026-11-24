"""数据健康检查：跑批之前/之后确认"数据是不是真的能用"。

检查项
------
1. **库存在且有数据**：行数、股票数、日期范围；
2. **新鲜度**：行情最新日期距"应有的交易日"落后几天（超阈值即不健康）；
3. **覆盖度**：最新交易日有多少只股票有 K 线（对比总股票数，太少说明同步失败）；
4. **重复行**：``(symbol, date)`` 是否唯一；
5. **留痕连续性**：``pick_history`` 最近是否有记录（可选）。

为什么重要
----------
没有健康检查时，"baostock 挂了"和"今天没有符合策略的股票"在日志里长得一模一样。
把这两件事区分开，是自动化跑批能长期可信的前提。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

#: 最新交易日至少要有这个比例的股票有数据，否则判为"同步不完整"
MIN_COVERAGE = 0.90


def expected_trade_date(today: date | None = None) -> date:
    """粗估"最近一个应有的交易日"：跳过周末（不处理法定节假日）。

    节假日会让检查偏严——所以阈值默认允许落后 1 天，而不是 0 天。

    Args:
        today: 参考日期，默认今天。

    Returns:
        最近一个工作日。
    """
    cursor = today or date.today()
    while cursor.weekday() >= 5:  # 5=周六, 6=周日
        cursor -= timedelta(days=1)
    return cursor


def check(
    db_path: str,
    max_lag_days: int = 1,
    min_coverage: float = MIN_COVERAGE,
    today: date | None = None,
) -> dict:
    """跑一遍健康检查。

    Args:
        db_path: SQLite 路径。
        max_lag_days: 允许落后的天数。
        min_coverage: 最新交易日的最低覆盖率（0~1）。
        today: 参考日期（测试用）。

    Returns:
        ``{"ok": bool, "issues": [...], "stats": {...}}``。
    """
    issues: list[str] = []
    stats: dict = {}

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        return {"ok": False, "issues": [f"无法打开数据库：{exc}"], "stats": {}}

    try:
        total = conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
        stats["rows"] = total
        if not total:
            return {"ok": False, "issues": ["stock_daily 是空的"], "stats": stats}

        last_date, symbols = conn.execute(
            "SELECT MAX(date), COUNT(DISTINCT symbol) FROM stock_daily"
        ).fetchone()
        stats["last_date"] = last_date
        stats["symbols"] = symbols

        # ① 新鲜度
        expected = expected_trade_date(today)
        lag = (expected - datetime.strptime(last_date, "%Y-%m-%d").date()).days
        stats["lag_days"] = lag
        if lag > max_lag_days:
            issues.append(f"行情落后 {lag} 天（最新 {last_date}，应为 {expected} 附近）")

        # ② 最新交易日的覆盖度
        on_last = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM stock_daily WHERE date = ?", (last_date,)
        ).fetchone()[0]
        coverage = on_last / symbols if symbols else 0.0
        stats["coverage"] = round(coverage, 4)
        if coverage < min_coverage:
            issues.append(
                f"最新交易日 {last_date} 只有 {on_last}/{symbols} 只（{coverage:.0%}）"
                f"，可能同步不完整"
            )

        # ③ 重复行
        dups = conn.execute(
            "SELECT COUNT(*) FROM (SELECT symbol, date, COUNT(*) n FROM stock_daily "
            "GROUP BY symbol, date HAVING n > 1)"
        ).fetchone()[0]
        stats["duplicates"] = dups
        if dups:
            issues.append(f"发现 {dups} 组重复的 (symbol, date)")

        # ④ 留痕（有表才检查）
        has_history = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='pick_history'"
        ).fetchone()[0]
        if has_history:
            row = conn.execute("SELECT MAX(run_date) FROM pick_history").fetchone()
            stats["last_pick_date"] = row[0] if row else None
    except sqlite3.Error as exc:
        issues.append(f"查询失败：{exc}")
    finally:
        conn.close()

    return {"ok": not issues, "issues": issues, "stats": stats}
