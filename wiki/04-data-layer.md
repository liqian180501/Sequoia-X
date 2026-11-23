# 04 · 数据层 DataEngine

> 文件：[sequoia_x/data/engine.py](../sequoia_x/data/engine.py)（333 行）
> 一句话职责：**把 baostock 的行情数据落到本地 SQLite，并提供给策略读取。**

---

## 1. 类结构一览

```python
class DataEngine:
    def __init__(self, settings)             # engine.py:59   建库建表（幂等）
    def _init_db(self)                       # engine.py:64   执行 CREATE TABLE / INDEX
    def _get_last_date(self, symbol)         # engine.py:72   单只股票的最新日期
    def get_ohlcv(self, symbol) -> DataFrame # engine.py:80   ★ 策略层的唯一读入口
    @staticmethod _to_baostock_code(symbol)  # engine.py:90   '600000' → 'sh.600000'
    def sync_today_bulk(self) -> int         # engine.py:97   ★ 日常增量（8 进程）
    def backfill(self, symbols)              # engine.py:158  ★ 历史回填（单进程+重试）
    def get_all_symbols(self) -> list[str]   # engine.py:301 从 baostock 拿全市场清单
    def get_local_symbols(self) -> list[str] # engine.py:328 从 SQLite 拿已有清单
```

模块级还有：

```python
_CREATE_TABLE_SQL, _CREATE_INDEX_SQL        # engine.py:14, 29
def _bs_fetch_batch(tasks: list) -> list    # engine.py:34   多进程 worker（必须模块级！）
```

---

## 2. 表结构

```sql
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,      -- 纯数字代码，如 '600000'（不带 sh/sz 前缀）
    date     TEXT    NOT NULL,      -- 'YYYY-MM-DD'
    open     REAL,                  -- 后复权
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,                  -- 成交量（股）
    turnover REAL,                  -- 成交额（元）；注意 baostock 里这个字段叫 amount
    UNIQUE (symbol, date)           -- ★ 幂等的根基
);

CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
```

### 2.1 设计要点

| 设计 | 原因 |
|---|---|
| **单表宽表**，不拆 `stock` / `daily` | 选股只需 OHLCV，不需要 join；少一次查询就快一倍 |
| `symbol` 存**纯数字** | baostock 用 `sh.600000`，雪球用 `SH600000`，只在边界转换（`_to_baostock_code` / `_to_xueqiu_code`），库内保持中性 |
| `date` 用 **TEXT** | SQLite 没有 DATE 类型；`'YYYY-MM-DD'` 的字典序 == 时间序，`MAX(date)` 直接可用 |
| **后复权** | 历史价格不随除权变化 → 可以只追加新行，不需要重算历史（增量的前提） |
| `UNIQUE(symbol, date)` | 重复插入直接抛 `IntegrityError`，天然防重；回填重跑也安全 |
| `id AUTOINCREMENT` | 实际上**没人用**（所有查询都按 symbol+date），而且 AUTOINCREMENT 会多维护一张 `sqlite_sequence` 表，属于轻微冗余 |
| `idx_symbol_date` | 与 UNIQUE 隐式创建的索引**完全重复**，是多余索引（多占磁盘、拖慢写入），见 [09](09-code-review-findings.md) |

### 2.2 实测规模

| 指标 | 实测值 |
|---|---|
| 文件大小 | 481 MB |
| 总行数 | 3,549,519 |
| 股票数 | 5,224 |
| 日期范围 | 2024-01-02 ~ 2026-11-18 |
| 单股 K 线数 | 平均 679，最少 7（次新股），最多 696 |

一行样例：

```
id=1, symbol='600000', date='2024-01-02',
open=79.22708118, high=79.4660769, low=78.8685876, close=78.8685876,
volume=22066700.0, turnover=146066303.72
```

> 价格是**后复权价**，所以 600000 显示 78.87 而不是真实的 ~7 元。所有策略都基于复权价算比率（涨幅、均线），这是对的；**但如果你想显示"真实股价"，必须用 `adjustflag="3"` 重新查一次**——`turtle_trade.py` 的市值计算就是这么做的。

---

## 3. baostock 对接

baostock 是一个"登录 → 查询 → 登出"的会话式 SDK（内部是裸 socket，没有连接池）。

```python
bs.login()                                   # 全局登录，成功返回 error_code == "0"
rs = bs.query_history_k_data_plus(
    "sh.600000",
    "date,open,high,low,close,volume,amount",   # 要哪些字段
    start_date="2024-01-01",
    end_date="2026-11-19",
    frequency="d",                              # d=日线
    adjustflag="1",                             # 1=后复权 2=前复权 3=不复权
)
while rs.next():                                # 迭代器，自动翻页
    row = rs.get_row_data()                     # 返回 list[str]（全是字符串！）
bs.logout()
```

| 关键点 | 说明 |
|---|---|
| `error_code != "0"` | baostock 用**字符串**表示错误码，比较时别写 `!= 0` |
| 服务端地址 | `public-api.baostock.com:10030`（裸 socket，非 HTTP），见 [contants.py:11-12](../.venv/Lib/site-packages/baostock/common/contants.py#L11-L12) |
| `rs.fields` | 列名列表，只在成功时有意义；`backfill` 用它建 DataFrame（engine.py:263） |
| `rs.next()` **自动翻页** | 每页 10000 条（`BAOSTOCK_PER_PAGE_COUNT`，见 [contants.py:15](../.venv/Lib/site-packages/baostock/common/contants.py#L15)）。单股 696 根 K 线一页装得下，所以不用担心翻页逻辑 |
| 数据**全是字符串** | 空串表示该日无数据，所以必须 `pd.to_numeric(errors="coerce")` |
| `amount` | 成交额（元）。本项目把它 rename 成 `turnover` |
| `adjustflag` | **1=后复权、2=前复权、3=不复权**（baostock 的枚举反直觉：数字越小复权越多） |

### 3.1 代码格式转换

```python
@staticmethod
def _to_baostock_code(symbol: str) -> str:          # engine.py:90
    prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
    return f"{prefix}.{symbol}"
```

| 真实市场 | 代码前缀 | 这段代码的结果 | 正确吗 |
|---|---|---|---|
| 上交所主板/科创板 | 60 / 68 / 9(B股) | `sh.600000` ✅ | ✅ |
| 深交所主板/创业板 | 00 / 30 / 2(B股) | `sz.000001` ✅ | ✅ |
| 北交所 | 43 / 83 / 87 / 92 | `sz.430047` ❌ | ⚠️ baostock **不支持**北交所（见下） |

**实测结论（2026-11-19，baostock 0.9.1）**：

```
sz.430047   err=0          msg='success'                   rows=0     ← 静默返回空
bj.430047   err=10004011   msg='股票代码未识别sh或sz'        rows=0     ← 明确拒绝
sh.600000   err=0          msg='success'                   rows=5
```

也就是说 baostock **只认 `sh.` / `sz.` 两个前缀，没有北交所数据**。所以：

- 北交所股票永远无法进入 `stock_daily`——这是**能力缺口**，不是 bug
- 但如果 4/8 开头的代码混进了 `get_all_symbols` 的返回值，`sz.4xxxxx` 查询会 `err=0` + 0 行 → **静默失败**，日志里只表现为"跳过"
- 而 `FeishuNotifier._to_xueqiu_code` 却正确处理了 4/8 → `BJ`，说明系统**意图**支持北交所，只是数据源不支持

详见 [09 · P1-3](09-code-review-findings.md#p1-3)。

---

## 4. 三条数据写入路径

```
                       ┌──────────────────────────────────────┐
                       │            SQLite stock_daily         │
                       └──────▲─────────────▲──────────────▲───┘
                              │             │              │
        ┌─────────────────────┘             │              └──────────────────┐
        │                                   │                                 │
┌───────┴─────────────┐   ┌─────────────────┴──────────┐   ┌──────────────────┴─────┐
│ sync_today_bulk()    │   │ backfill(symbols)          │   │ get_ohlcv / get_last   │
│ 日常增量·8进程         │   │ 历史回填·单进程+重试       │   │ 只读                    │
│ main.py 默认路径      │   │ python main.py --backfill  │   │ 策略层调用               │
└───────┬─────────────┘   └─────────────────┬──────────┘   └────────────────────────┘
        │                                    │
        └──────────────┬─────────────────────┘
                       ▼
              baostock (HTTP/socket)
```

### 4.1 路径 A：`sync_today_bulk()` — 日常增量（多进程）

**语义**：对每只已入库的股票，从 `MAX(date)+1` 补到**今天**（不是"只补今天"）。

```python
# ① 找出每只股票的进度（一次 GROUP BY 查询）
rows = conn.execute("SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol").fetchall()

# ② 只为落后的股票生成任务
for symbol, last_date in rows:
    if last_date and last_date >= today_str:      # 已是最新 → 跳过
        continue
    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
    tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

# ③ 切成 N 份（默认 4），每个 worker 带同一个 deadline 并行拉
deadline = time.monotonic() + budget
payload = [(chunk, deadline, _StopFlag()) for chunk in chunks]
with Pool(n_workers) as pool:
    results = pool.map_async(_bs_fetch_batch, payload).get(timeout=budget + 20)

# ④ 合并 → 清洗 → 幂等 upsert（不再先删后插）
conn.executemany(_UPSERT_SQL, records)   # ON CONFLICT(symbol,date) DO UPDATE
```

**多进程细节**

| 点 | 说明 |
|---|---|
| `_bs_fetch_batch` 为什么是模块级函数 | 提交给 `Pool` 的函数必须能被 **pickle**。写成 `DataEngine` 的方法会把 `self`（含 SQLite 路径等）一起序列化，且 `sqlite3` 连接不可跨进程序列化；模块级纯函数最安全 |
| 每个子进程自己 `login` / `logout` | baostock 的登录态是**进程级全局变量**，父进程登录对子进程无效（spawn 模式下更是全新解释器） |
| 并发数默认 4 | 8 个并发登录容易触发 baostock 限流（`BSERR_LOGIN_COUNT_LIMIT = 10001005`），实测 8 并发时出现过 1 个进程登录失败、其余全部超时；4 并发稳定 |
| `chunks = [tasks[i::N] for i in range(N)]` | 交错分片（第 i 个开始每隔 N 个取一个），比连续切块更均衡 |
| 为什么传 `_StopFlag` 而不是 `multiprocessing.Event` | `Event` 无法作为 `Pool` 任务参数传递（会抛 `Condition objects should only be shared between processes through inheritance`）；这里只需要"本进程提前收工"的语义，普通对象随任务 pickle 即可 |
| 时间预算 | worker 每处理一只前检查 `deadline`，到点返回部分结果；父进程另有 `budget + 20s` 硬超时兜底 |

**数据安全（v2 已修）**：旧实现的第 ⑤ 步是 `DELETE FROM stock_daily WHERE date = ?` —— 删的是**那一天的全部股票**，然后只插入本次真正取到的行。如果某只股票拉取失败，它那一天的历史数据就被**永久删除**。现在改成按 `(symbol, date)` 幂等 upsert，取数失败的股票保留原数据，重跑也不会重复。详见 [09 · P1-1](09-code-review-findings.md#p1-1)。

**失败语义**：预算用尽或网络异常时，同步只覆盖一部分股票，日志会明确写
`同步结束（31.8s）：成功 0 | 失败 0 | 未处理 5224 ...` 以及"本次同步未覆盖全部股票"。
重跑 `main.py` 会从未更新的股票继续（`MAX(date)` 决定起点），或用 `--sync-rounds N` 一次跑多轮。

### 4.2 路径 B：`backfill(symbols)` — 历史回填（单进程 + 重试）

```python
# python main.py --backfill
all_symbols = engine.get_all_symbols()   # 从 baostock 拿 5,224 只
engine.backfill(all_symbols)
```

流程与容错：

```
bs.login()
for i, symbol in enumerate(symbols):
    if 已有数据且 >= 今天:      skipped++; continue        ← 断点续传的关键
    每 200 只: logout + sleep(1) + login                   ← 防长连接超时
    查询循环最多 3 次:
        成功 → break
        失败 → sleep(2/4/8 递增) + 重连 + 重试
    3 次都失败 → failed++; continue
    清洗 → INSERT（IntegrityError 直接 pass）
finally:
    bs.logout()                                            ← 无论如何都会登出
```

| 容错机制 | 代码位置 | 参数 |
|---|---|---|
| 单只失败重试 | engine.py:221-253 | `max_retries = 3`，退避 `2^attempt` 秒（2/4/8） |
| 定期重连 | engine.py:203-210 | 每 `reconnect_interval = 200` 只重连一次 |
| 幂等续传 | engine.py:192-200 | 已是最新的直接跳过 |
| 唯一约束 | engine.py:283-284 | `except sqlite3.IntegrityError: pass` |
| 进度日志 | engine.py:195-199/288-292 | 每 500 只打一行 |

最终日志形如：

```
回填完成 — 成功: 5100 | 跳过: 120 | 失败: 4
```

**注意 `failed` 的语义**：失败只是计数，不重试第二遍、不抛出。所以看到"失败 4"要手动再跑一次（会续传），或者写脚本循环跑直到 `failed == 0`。

### 4.3 路径 C：读取

```python
def get_ohlcv(self, symbol: str) -> pd.DataFrame:      # engine.py:80
    with sqlite3.connect(self.db_path) as conn:
        df = pd.read_sql("SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                         conn, params=(symbol,))
    return df
```

- **每次调用新建一个 SQLite 连接**。如果策略层逐只调用（v1 的做法），单个策略就有 5,224 次连接/关闭。
- `SELECT *` 会把没人用的 `id` 列也读出来。影响很小（多了几 MB 内存带宽），但严格说应该只选 7 列。
- `ORDER BY date` 是**必须**的：策略里 `iloc[-1]` 依赖行序。

> ⚠️ **v2 起策略层不再走这条路径**。`get_ohlcv()` 保留给"单只股票诊断/调试"用（见 [10 HOWTO 配方 7](10-how-to-recipes.md)），
> 策略改用下面的 `get_snapshot()`。

### 4.4 路径 D：`get_snapshot()` — 全市场快照（v2 推荐，策略层唯一入口）

```python
snap = engine.get_snapshot()      # 首次调用加载，之后复用（进程内缓存）
```

内部只做两件事：

```python
# ① 找出窗口边界：最近 N 个交易日
dates = conn.execute("SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT ?", (days,))
# ② 一条 SQL 取回窗口内全市场数据（只取 8 列，不 SELECT *）
df = pd.read_sql("SELECT symbol,date,open,high,low,close,volume,turnover "
                 "FROM stock_daily WHERE date >= ?", conn, params=(cutoff,))
# ③ 按 (symbol, date) 排序后交给 MarketSnapshot
df = df.sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)
```

| 项 | 值（实测，130 个交易日） |
|---|---|
| 窗口 | 2026-05-13 ~ 2026-11-18 |
| 行数 | 674,960 行 / 5,224 只 |
| 耗时拆解 | DISTINCT date 1.48 s + 读取 2.63 s + 排序 0.23 s + 建索引 0.13 s ≈ **4.5 s** |
| 内存 | ≈ 105 MB（对比 RPS 全表 440 MB） |

`MarketSnapshot`（`sequoia_x/data/snapshot.py`）在此之上提供：

| 方法 | 作用 |
|---|---|
| `snap.rolling(col, w, how)` | 按股票分组的滚动指标（含当日），结果缓存 |
| `snap.rolling_shifted(col, n, w, how)` | 先移位再滚动（等价"不含当日"，如"前 20 日最高价"） |
| `snap.shift(col, n)` | 按股票分组取 N 天前的值 |
| `snap.enough_bars(n)` | 该股票在窗口内是否有 ≥ n 根 K 线 |
| `snap.latest(cond)` | 只取每只股票**最后一行**满足条件的代码 |

**实现要点**：不用 `groupby(...).transform(...)`（每次分组回调进 Python，实测慢 43 倍），
而是整列做一次 `rolling`/`shift`，再用 `cumcount` 把每组头部的跨组污染行置 NaN。
详见 [03 pandas 手册 · 全局 rolling + cumcount 掩码](03-pandas-field-guide.md#pandas-rolling-trick)。

**缓存失效**：`sync_today_bulk()` / `backfill()` 开头会调用 `invalidate_snapshot()`，
所以"先同步再跑策略"的顺序永远是安全的。窗口大小由 `SNAPSHOT_DAYS`（默认 130）控制。

---

## 5. 股票清单：两个来源

| 方法 | 数据源 | 用途 | 耗时 |
|---|---|---|---|
| `get_all_symbols()` | baostock `query_stock_basic(code_name="", code="")` | **回填**时决定要拉哪些票 | 几秒（一次分页查询） |
| `get_local_symbols()` | `SELECT DISTINCT symbol FROM stock_daily` | 需要"代码列表"而非数据时使用 | 毫秒级 |
| `get_snapshot()` | 窗口内全市场行情 | **策略层的主入口**（v2） | 4.5 s，只算一次 |

```python
rs = bs.query_stock_basic(code_name="", code="")
while rs.next():
    row = rs.get_row_data()
    status = row[4]         # ← 实际是 type
    stock_type = row[5]     # ← 实际是 status
    if status == "1" and stock_type == "1":
        symbols.append(code.split(".")[1])
```

**实测 baostock 返回的字段顺序是**：

```
['code', 'code_name', 'ipoDate', 'outDate', 'type', 'status']
    0        1          2          3         4       5
```

所以 `row[4]` 是 **type**（1=股票 2=指数 3=其它 4=可转债 5=ETF），`row[5]` 是 **status**（1=上市 0=退市）。代码把两个变量的**名字写反了**，但因为两个条件都要求 `== "1"`（只要"股票"且"上市"），过滤结果**恰好正确**。这是一个"结果对、可读性错"的典型，见 [09](09-code-review-findings.md#p2-1)。

`code.split(".")[1]` 把 `'sh.600000'` 拆成 `'600000'`，与库内格式一致。

---

## 6. 耗时与并发（v2 实测）

| 阶段 | 量级 | 瓶颈 |
|---|---|---|
| `get_all_symbols` | 一次分页查询 | 网络（**注意**：`query_stock_basic` 全市场查询在某些网络下会超时，有 `query_all_stock` 兜底） |
| `sync_today_bulk` | 8 进程 × 约 650 只/进程 | baostock 网络往返（中位 94 ms/次） |
| 行情快照 | 1 次查询 × 67 万行 | **SQLite 全表扫描 + pandas 物化**（4.5 s） |
| 5 个 K 线策略 | 各 0.02~0.20 s（共享快照） | CPU 很轻 |
| RPS 策略 | 1 条 SQL 聚合（5,224 行 / 0.4 MB） | 索引范围扫描（1.5 s） |
| 查股票名 | 首次建目录约 30~42 s，之后本地表 3 ms | 网络 → **本地表** |
| 查流通市值 | 首次约 170 ms/只，同日重跑命中缓存 | 网络 → **本地表** |
| 飞书推送 | 最多 7 次 HTTP | 网络 |

改造过程分两轮：

1. **策略层**：从"7 × 5,224 次单只查询"改成"1 次快照 + 5 次向量化"，125 s → 15 s；
2. **RPS + 富化**：RPS 改 SQL 聚合 10.5 s → 1.5 s（内存 440 MB → 0.4 MB）；
   股票名 37.7 s → 3 ms、同日市值查询 → 5 ms。

**策略 + 推送阶段（不含数据同步）：约 187 s → 6.9 s（热跑）。**
现在整次运行的瓶颈几乎全在同步的 baostock 网络往返上。

`socket.setdefaulttimeout(10.0)`（[main.py:16](../main.py#L16)）给所有 socket 设了 10 秒超时，避免 baostock 半连接挂死整个进程。

---

## 7. 坑与注意（调试前必读）

| # | 现象 | 原因 | 处理 |
|---|---|---|---|
| 1 | 日志显示"无新数据（可能非交易日）" | 周末/节假日 baostock 返回空 | 正常，忽略 |
| 2 | 某天的历史数据突然变少 | `sync_today_bulk` 的 DELETE-by-date（见 4.1） | 重新 `python main.py` 补回；**不要在生产库上随便跑** |
| 3 | 回填卡在某个股票很久 | baostock 长连接超时，正在走 2/4/8 秒退避 | 等；或看日志 `[symbol] 第N次失败` |
| 4 | `baostock 登录失败` | 网络/并发登录数超限 | 减少 `n_workers` |
| 5 | 策略一条都选不出来 | 数据太旧，或 `--backfill` 没跑过 | 先检查 `SELECT MAX(date)` |
| 6 | 北交所股票从没数据 | baostock 不支持北交所，`sz.4xxxxx` 静默返回 0 行 | 见 [09 · P1-3](09-code-review-findings.md#p1-3) |
| 7 | 查询某代码后进程**卡死不动** | 没有 socket 超时 + baostock 对某些代码不响应（实测可挂 10 分钟以上） | **不要删** [main.py:16](../main.py#L16) 的 `socket.setdefaulttimeout(10.0)` |
| 8 | `sqlite3.OperationalError: database is locked` | 多个进程同时写 | 目前只有主进程写，理论上不会；如果自己加了并发写，开 WAL |
| 9 | 内存飙到 1GB+ | 旧版 RPS 整表加载（440 MB）；**已修复**，现为 0.4 MB | 见 [09 · P1-5](09-code-review-findings.md#p1-5) |
| 10 | `--backfill` 提示"共 0 只" | `query_stock_basic` 全市场查询超时（`err=10002007`） | 已加 `query_all_stock` + 本地库双重兜底；若仍为 0，检查网络/换离线清单 |

### 7.1 手工排查 SQL

```sql
-- 数据进度
SELECT MIN(date), MAX(date), COUNT(*), COUNT(DISTINCT symbol) FROM stock_daily;

-- 哪些股票数据落后（回填失败的那几只）
SELECT symbol, MAX(date) AS last, COUNT(*) AS bars
FROM stock_daily GROUP BY symbol HAVING last < date('now','-1 day')
ORDER BY last LIMIT 50;

-- 某天入库了多少只（判断 DELETE-by-date 是否误删）
SELECT date, COUNT(*) FROM stock_daily GROUP BY date ORDER BY date DESC LIMIT 10;

-- 单只股票最近 5 天
SELECT date, open, close, volume, turnover FROM stock_daily
WHERE symbol='600000' ORDER BY date DESC LIMIT 5;
```

---

## 8. 如果要换数据源

`DataEngine` 对策略层暴露的契约只有 3 个：

```python
engine.get_local_symbols() -> list[str]
engine.get_ohlcv(symbol)   -> pd.DataFrame   # 必须含 open/high/low/close/volume/turnover/date
engine.db_path             -> str            # RPS 策略直接读这个（破坏封装，见 09）
```

只要新实现保证 `get_ohlcv` 返回同样的列名 + 按 date 升序，7 个策略一行都不用改。这就是"数据层封装"的价值。

下一步：看懂策略怎么用这些数据 → [05 策略系统](05-strategy-system.md)
