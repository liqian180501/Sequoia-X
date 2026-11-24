"""分析库（DuckDB）：把重查询从 SQLite 挪出来。

为什么分工
----------
两种负载长得完全不一样：

| 负载 | 例子 | 合适引擎 |
|---|---|---|
| **点查 + 写入** | 增量同步 upsert、取某只股票的 K 线 | SQLite（行存、事务稳、单文件） |
| **全表扫描 + 聚合** | 因子 IC、回测、每日统计、跨年分组 | DuckDB（列存、向量化、压缩） |

所以不是"替换 SQLite"，而是**分工**：SQLite 继续做交易/写入，DuckDB 做分析。
两者可以 `ATTACH` 后 join。

数据来源有两个
--------------
1. 从 SQLite 拷贝（``bars`` 表）——与线上完全一致；
2. 直接读同花顺 Parquet dump（``bars_raw`` 表）——1,041 万行、10 年、未复权，
   用于需要长历史的分析（SQLite 目前只有 4M 行 / 3 年）。

用法
----
```python
store = AnalyticsStore("data/analytics.duckdb")
store.build_from_sqlite("data/sequoia_v2.db")
store.query("SELECT COUNT(*) FROM bars").fetchone()
```
"""

from __future__ import annotations

import time
from pathlib import Path

import duckdb
import pandas as pd

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

#: 从 SQLite 拷贝的列（与 stock_daily 对齐）
BAR_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]


class AnalyticsStore:
    """DuckDB 分析库。

    Args:
        path: DuckDB 文件路径（不存在会自动创建）。
    """

    def __init__(self, path: str = "data/analytics.duckdb") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(self.path))

    # ── 构建 ──

    def build_from_sqlite(self, sqlite_path: str, table: str = "bars") -> int:
        """把 SQLite 的 ``stock_daily`` 全量拷进 DuckDB。

        优先用 DuckDB 的 sqlite 扩展（快、零拷贝）；扩展不可用（离线/未安装）时
        退回 pandas 中转。两条路径结果一致，只是后者慢几秒。

        Args:
            sqlite_path: SQLite 文件路径。
            table: 目标表名。

        Returns:
            写入行数。
        """
        started = time.perf_counter()
        rows = self._try_attach(sqlite_path, table)
        if rows is None:
            rows = self._copy_via_pandas(sqlite_path, table)
        logger.info(
            f"已构建 {table}：{rows:,} 行，耗时 {time.perf_counter() - started:.1f}s"
        )
        return rows

    def _try_attach(self, sqlite_path: str, table: str) -> int | None:
        """用 sqlite 扩展直接 ATTACH（失败返回 None，由调用方降级）。"""
        try:
            self.conn.execute("INSTALL sqlite; LOAD sqlite;")
            self.conn.execute(f"ATTACH '{sqlite_path}' AS src (TYPE SQLITE)")
        except Exception as exc:  # noqa: BLE001 扩展不可用就走 pandas
            logger.info(f"sqlite 扩展不可用（{type(exc).__name__}），改用 pandas 中转")
            return None

        try:
            cols = ", ".join(BAR_COLUMNS)
            self.conn.execute(
                f"CREATE OR REPLACE TABLE {table} AS SELECT {cols} FROM src.stock_daily"
            )
            rows = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            return int(rows)
        finally:
            self.conn.execute("DETACH src")

    def _copy_via_pandas(self, sqlite_path: str, table: str) -> int:
        """pandas 中转（扩展不可用时的兜底）。"""
        import sqlite3

        with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True, timeout=30) as conn:
            df = pd.read_sql(f"SELECT {', '.join(BAR_COLUMNS)} FROM stock_daily", conn)
        self.conn.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM df")
        return len(df)

    def build_from_parquet(self, parquet_path: str, table: str = "bars_raw") -> int:
        """直接读同花顺 dump 的 Parquet（未复权，10 年）。

        Args:
            parquet_path: Parquet 文件路径。
            table: 目标表名。

        Returns:
            写入行数。
        """
        started = time.perf_counter()
        self.conn.execute(
            f"""
            CREATE OR REPLACE TABLE {table} AS
            SELECT thscode                       AS symbol,
                   strftime(make_timestamp(date_ms * 1000), '%Y-%m-%d') AS date,
                   open_price   AS open,
                   high_price   AS high,
                   low_price    AS low,
                   close_price  AS close,
                   volume,
                   turnover
            FROM read_parquet('{parquet_path}')
            """
        )
        rows = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        logger.info(
            f"已构建 {table}：{rows:,} 行，耗时 {time.perf_counter() - started:.1f}s"
        )
        return int(rows)

    # ── 查询 ──

    def query(self, sql: str, params: list | None = None) -> pd.DataFrame:
        """执行 SQL 并返回 DataFrame。"""
        return self.conn.execute(sql, params or []).df()

    def stats(self) -> dict:
        """当前库的概况。"""
        tables = [
            row[0]
            for row in self.conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        ]
        out: dict = {"path": str(self.path), "tables": {}}
        for name in tables:
            rows = self.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            out["tables"][name] = int(rows)
        return out

    def close(self) -> None:
        self.conn.close()
