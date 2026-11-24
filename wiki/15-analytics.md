# 15 · 分析路径（DuckDB / Parquet）

> 目录：[sequoia_x/data/analytics.py](../sequoia_x/data/analytics.py)
> 入口：[`scripts/build_analytics.py`](../scripts/build_analytics.py)

---

## 1. 为什么不是"用 DuckDB 替换 SQLite"

两种负载长得完全不一样：

| 负载 | 例子 | 合适引擎 |
|---|---|---|
| **点查 + 写入** | 增量同步 upsert、取某只股票的 K 线 | SQLite（行存、事务稳、单文件） |
| **全表扫描 + 聚合** | 因子 IC、回测、每日统计、跨年分组 | DuckDB（列存、向量化、压缩） |

所以是**分工**，不是替换：SQLite 继续做写入与点查，DuckDB 做分析。

---

## 2. 实测差距

同一份"每日涨跌统计"（2024 年起，按日分组 + 中位数收益 + 涨跌家数，698 个交易日）：

```
SQLite + pandas :   8.40s
DuckDB          :   0.39s      →  21.6×
```

差在两个地方：

1. **列存**：分析只读少数几列，DuckDB 只扫这几列；SQLite 要读整行；
2. **向量化 + 并行**：聚合不在 Python 里做。SQLite 那边 8.4 秒里有 7 秒是
   "把 300 万行搬进 pandas 再 groupby"，而 DuckDB 直接在存储层算完。

> 结论不是"SQLite 慢"，而是**用错工具**：SQLite 做点查是几十微秒，DuckDB 做点查反而不划算。

---

## 3. 两张表

| 表 | 来源 | 行数 | 特点 |
|---|---|---:|---|
| `bars` | 从 SQLite `stock_daily` 拷贝 | 4,018,452 | **与线上完全一致**（后复权） |
| `bars_raw` | 同花顺 Parquet dump | 10,414,384 | 10 年、**未复权**、含 357 只北交所 |

为什么要两张：`bars` 用于"复现线上口径"，`bars_raw` 用于"需要长历史/原始价格"的分析
（例如真实成交价、跨 2015 股灾的样本）。**未复权与后复权不能混用**，所以分表而不是合并。

---

## 4. 用法

```bash
# 从 SQLite 重建（约 15 s）
python scripts/build_analytics.py --rebuild --stats

# 载入同花顺 dump（约 8 s，1,041 万行）
python scripts/build_analytics.py --from-dump

# 任意 SQL
python scripts/build_analytics.py --query "SELECT COUNT(DISTINCT symbol) FROM bars_raw"

# 性能对比
python scripts/build_analytics.py --benchmark
```

```python
from sequoia_x.data.analytics import AnalyticsStore

store = AnalyticsStore("data/analytics.duckdb")
store.build_from_sqlite("data/sequoia_v2.db")
df = store.query("""
    SELECT date, MEDIAN(close / prev_close - 1) AS median_ret
    FROM (
        SELECT date, close,
               LAG(close) OVER (PARTITION BY symbol ORDER BY date) AS prev_close
        FROM bars WHERE date >= '2024-01-02'
    )
    WHERE prev_close IS NOT NULL
    GROUP BY date ORDER BY date
""")
```

---

## 5. 实现细节

**优先用 DuckDB 的 sqlite 扩展**（`ATTACH ... (TYPE SQLITE)` 零拷贝），
扩展不可用（离线、未安装）时**自动降级**到 pandas 中转。两条路径结果一致，后者慢几秒。
降级是必要的：`INSTALL sqlite` 需要联网，而本项目经常在断网环境里跑。

Parquet 的日期是毫秒时间戳，导入时用
`strftime(make_timestamp(date_ms * 1000), '%Y-%m-%d')` 转成与 SQLite 一致的字符串，
这样两张表可以**直接按 date join**。

---

## 6. 局限

1. 分析库是**离线快照**，不是实时同步：SQLite 写入后要重跑 `--rebuild`；
2. 没有增量：每次都全量重建（4M 行 ~15 s，可接受；上亿行就需要改增量）；
3. `bars_raw` 未复权，不能直接算收益率跨度很长的指标（除权跳空）；
4. 目前只有 `bars` / `bars_raw` 两张表，财务、行业、指数都还没有；
5. 网页控制台还读 SQLite —— 迁移到 DuckDB 是后续 P2-9 的事。
