# 09 · 代码评注与改进建议

> 这份文档是**代码审查记录**，不是官方文档。每条都标了「已确认」或「需验证」，并提供可直接采用的修法。
> 等级：**P1** 会产生错误结果 / 数据损失 / 线上中断；**P2** 可维护性与性能隐患；**P3** 风格与整洁。
>
> **2026-11 更新（性能改造 P0+P1 已落地）**：策略层改为"一次批量读 + 全市场向量化"，
> 共 6 个策略的选股结果逐只比对完全一致，策略层耗时 125 s → 15 s。
>
> **第三轮（CLI + 健壮性）**：新增 `--push-now/--dry-run/--strategy/--symbols` 等命令行入口；
> 同步加了时间预算与断线重连（修掉"网络故障挂一整晚"）；写入改为幂等 upsert；
> 涨跌停按板块判定。因此 [P1-1](#p1-1)、[P1-2](#p1-2)、[P1-4](#p1-4) 也已修复。
>
> **第四轮（策略逻辑重写）**：9 个策略全部重审并改写（新增 TrendPullback / ReversalBounce），
> 引入通用可交易性过滤（ST/停牌/次新/流动性/涨停）与打分 Top-N，详见
> [05 策略系统](05-strategy-system.md)。因此 [P2-8](#p2-8) 也已修复。
>
> 因此 [P2-4](#p2-4)、[P2-5](#p2-5)、[P3-5](#p3-5) 已修复，
> [P1-5](#p1-5) 仅剩 RPS 未处理。其余条目仍待处理。

---

## 速览

| 编号 | 等级 | 问题 | 状态 |
|---|---|---|---|
| [P1-1](#p1-1) | P1 | `sync_today_bulk` 按日期整段删除后重插，部分失败会**永久丢数据** | **已修复**（改为 `(symbol,date)` 幂等 upsert） |
| [P1-2](#p1-2) | P1 | 涨跌停阈值 1.095 / 0.905 硬编码，未区分板块（10%/20%/30%/5%） | **已修复**（`limits.py`，`--no-board-limits` 可回退） |
| [P1-3](#p1-3) | P1 | 北交所数据完全缺失，`sz.` 前缀静默返回空且与推送端规则不一致 | 待修 |
| [P1-4](#p1-4) | P1 | `resp.json()` 非 JSON 时抛 `ValueError`，绕过 `RequestException` | **已修复**（单独捕获 ValueError） |
| [P1-5](#p1-5) | P1 | RPS 每次把 355 万行整表载入内存 | **已修复**（SQL 聚合：1.5 s / 0.4 MB） |
| [P1-6](#p1-6) | P1 | `get_all_symbols()` 依赖的全市场接口会超时，`--backfill` 拿到空清单 | **已修复**（`query_all_stock` + 本地库兜底） |
| [P2-1](#p2-1) | P2 | `get_all_symbols` 里 type/status 两个变量名写反 | **已修复**（改为元组解包，名字自解释） |
| [P2-2](#p2-2) | P2 | `settings_customise_sources` 是死代码且污染 `os.environ` | 待修 |
| [P2-3](#p2-3) | P2 | 策略 webhook 依赖 `load_dotenv()`，删了会静默失效 | 待修 |
| [P2-4](#p2-4) | P2 | MaVolume `_MIN_BARS=20` 应为 21 | **已修复**（=21；Uptrend 同时改为 61） |
| [P2-5](#p2-5) | P2 | 7 个策略重复扫全市场 + 每股票新建连接 | **已修复**（8.2×，见 [05](05-strategy-system.md)） |
| [P2-6](#p2-6) | P2 | 测试真实访问 baostock 网络 | **已修复**（名称查询改为 mock） |
| [P2-7](#p2-7) | P2 | 所有阈值都是魔法数字，不可配置、无法回测 | 部分改善（已提升为具名类常量） |
| [P2-8](#p2-8) | P2 | 无 ST / 停牌 / 次新过滤 | **已修复**（`strategy/filters.py` 统一过滤） |
| [P2-9](#p2-9) | P2 | 回填失败只计数不重试 | 待修 |
| [P2-10](#p2-10) | P2 | 无交易日历，节假日空跑 1~2 分钟 | 待修 |
| [P2-11](#p2-11) | P2 | RPS 绕过数据层直接读库 | **已放宽**（改为有注释说明的 SQL 聚合，见 P1-5） |
| [P3-1](#p3-1) | P3 | 4 处未使用导入 / 死变量 | 部分修复（4 个策略文件已清理） |
| [P3-2](#p3-2) | P3 | ruff 配置位置过时（`select` 应在 `[tool.ruff.lint]`） | 待修 |
| [P3-3](#p3-3) | P3 | 重复索引 `idx_symbol_date` | 待修 |
| [P3-4](#p3-4) | P3 | docstring 与实现不一致（3 条 vs 4 条条件） | **已修复** |
| [P3-5](#p3-5) | P3 | `except Exception` 吞掉一切 | **已消除**（5 个 K 线策略不再有单只循环） |
| [P3-6](#p3-6) | P3 | 无 CI、无类型检查、无回测记录 | 待修 |

---

<a id="p1-1"></a>
## P1-1 · `sync_today_bulk` 会永久删掉某天的部分数据（已确认）

**位置**：[engine.py:149-153](../sequoia_x/data/engine.py#L149-L153)

```python
with sqlite3.connect(self.db_path) as conn:
    for d in df["date"].unique().tolist():
        conn.execute("DELETE FROM stock_daily WHERE date = ?", (d,))   # ← 删掉这一天的【所有股票】
    df.to_sql("stock_daily", conn, if_exists="append", ...)            # ← 只插回本次拿到的
```

**为什么危险**：`DELETE` 的条件只有 `date`，不带 `symbol`。假设某天 5,224 只里只有 5,100 只拉取成功（子进程 `continue` 掉了 124 只），执行后这 124 只**那一天的数据被永久删除**，而且不会有任何报错——`count` 日志显示的是 5,100 条，看起来"正常"。

再次运行也**补不回来**：这 124 只的 `MAX(date)` 已经是那天了，`sync_today_bulk` 会认为"已是最新"而跳过（engine.py:115-116）。

**触发条件**：
- baostock 偶发查询失败（重试机制在 `backfill` 里，`sync_today_bulk` **没有**）
- 8 个子进程里某个中途异常退出
- 盘中（15:00 前）运行后再在收盘后运行：第一次存的是未完成的日线，第二次会删掉整天再重插——这一次通常是全的，所以还好

**修法 A（最小改动）**：改成"按 symbol+date 幂等插入"，彻底不删。

```python
# 先把这批数据写到临时表，再 UPSERT 进主表
df.to_sql("_tmp_sync", conn, if_exists="replace", index=False)
conn.execute("""
    INSERT INTO stock_daily (symbol, date, open, high, low, close, volume, turnover)
    SELECT symbol, date, open, high, low, close, volume, turnover FROM _tmp_sync
    WHERE true
    ON CONFLICT(symbol, date) DO UPDATE SET
        open=excluded.open, high=excluded.high, low=excluded.low,
        close=excluded.close, volume=excluded.volume, turnover=excluded.turnover
""")
conn.execute("DROP TABLE _tmp_sync")
```

**修法 B（最省事）**：只删本次真正拿到的 `(symbol, date)` 组合，而不是整天。

```python
for (sym, d) in df[["symbol", "date"]].itertuples(index=False, name=None):
    conn.execute("DELETE FROM stock_daily WHERE symbol=? AND date=?", (sym, d))
```

（`itertuples` 比 `iterrows` 快很多，且这里只有几千行，可接受。）

> 无论哪种修法，**当天首次入库后立刻备份**是最低成本的兜底。

---

<a id="p1-2"></a>
## P1-2 · 涨跌停判定没有区分板块（已确认）

**位置**：`limit_up_shakeout.py:49`、`uptrend_limit_down.py:56`

```python
limit_up_yesterday = prev1["close"] >= prev2["close"] * 1.095      # limit_up_shakeout.py:49
limit_down         = today["close"] <= prev["close"] * 0.905       # uptrend_limit_down.py:56
```

A 股实际涨跌幅限制：

| 板块 | 代码前缀 | 限制 | 对 1.095 的影响 |
|---|---|---|---|
| 主板 | 60 / 00 | 10% | ✅ 基本正确 |
| 创业板 | 30 | **20%** | ❌ 20% 涨停（×1.20）也满足 `>= 1.095`，会把"涨 10% 的普通阳线"误判为涨停 |
| 科创板 | 68 | **20%** | ❌ 同上 |
| 北交所 | 43/83/87/92 | **30%** | ❌ 同上 |
| ST / *ST | 任意 | **5%** | ❌ 涨 9.5% 在 ST 上不可能，等于该股永不入选 |

**具体后果**：
- `LimitUpShakeout`：在创业板/科创板里，"昨天涨了 10% 的票"会被当成"昨天涨停"，**假信号大量涌入**。这可能是该策略实际表现不稳的主因。
- `UptrendLimitDown`：创业板跌 9.5% 不是跌停，但会被判成跌停；反之真正跌 20% 的当然也被算进去（碰巧对）。

**修法**：先按代码前缀算限制比例，再判定。

```python
def _limit_pct(code: str, name: str = "") -> float:
    """返回该股票的涨跌幅限制比例（0.10 / 0.20 / 0.30 / 0.05）。"""
    if "ST" in name.upper():
        return 0.05
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("43", "83", "87", "92")):
        return 0.30
    return 0.10
```

但注意：**涨跌停价是四舍五入到分的**，严格判定应比较"是否等于涨停价"而不是"涨幅是否 ≥ 9.5%"。最稳的做法是把 baostock 的 `tradestatus` / `isST` 字段也一起存下来（见 [10 HOWTO](10-how-to-recipes.md)）。

---

<a id="p1-3"></a>
## P1-3 · 北交所数据完全缺失，且失败是静默的（已实测确认）

**位置**：[engine.py:89-93](../sequoia_x/data/engine.py#L89-L93) vs [feishu.py:31-38](../sequoia_x/notify/feishu.py#L31-L38)

```python
# DataEngine（数据层）
prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
return f"{prefix}.{symbol}"          # '430047' → 'sz.430047'

# FeishuNotifier（推送层）
if code.startswith("6"):      return f"SH{code}"
elif code.startswith(("4", "8")): return f"BJ{code}"      # '430047' → 'BJ430047'
return f"SZ{code}"
```

**同一个 6 位代码，两处映射规则不一致。**

### 实测证据（baostock 0.9.1，2026-11-19）

```
sz.430047   err=0          msg='success'                 rows=0     ← 静默返回空！
bj.430047   err=10004011   msg='股票代码未识别sh或sz'      rows=0     ← 明确拒绝
sh.600000   err=0          msg='success'                 rows=5
```

复现脚本：

```python
import socket; socket.setdefaulttimeout(5.0)      # ★ 不加超时会卡死，见下方警告
import baostock as bs
bs.login()
for code in ["sz.430047", "bj.430047", "sh.600000"]:
    rs = bs.query_history_k_data_plus(code, "date,close",
        start_date="2024-06-03", end_date="2024-06-07", frequency="d", adjustflag="1")
    n = 0
    while rs.next(): rs.get_row_data(); n += 1
    print(code, rs.error_code, rs.error_msg, n)
bs.logout()
```

### 结论（比"代码写错了"更准确）

1. **baostock 本身不支持北交所**——它只认 `sh.` / `sz.` 两个前缀，`bj.` 直接返回 `10004011 股票代码未识别sh或sz`。
2. 所以 `_to_baostock_code` 把 4/8 映射成 `sz.` 并不能"修好"——它返回的是 **`err=0` + 0 行**，一个**看起来成功但没数据**的结果。
3. 这意味着：**北交所股票永远无法进入 `stock_daily`**。功能上是个能力缺口，不是数据污染。
4. 真正的风险在**静默**：`backfill` 里 `rows` 为空走 `skipped++`（engine.py:259-261），日志表现为"跳过"，没人会发现这几百只票从来没有数据。
5. `_to_xueqiu_code` 却正确处理 4/8 → `BJ`，说明系统**意图**支持北交所。两处不一致是"意图未落地"的信号。

这是一个附带的**重要发现**：探测 `bj.430047` 时如果**不设 socket 超时**，查询会**无限期挂起**（我实测卡了 12 分钟没返回）。这解释了为什么 [main.py:16](../main.py#L16) 要有那句 `socket.setdefaulttimeout(10.0)`——**那行不是防御性代码，是必需品**，别删。

### 修法

**如果接受"不支持北交所"**（现实选择）：在 `get_all_symbols` 里主动排除，并明确记日志。

```python
symbols = []
excluded_bj = 0
while rs.next():
    row = rs.get_row_data()
    code, _name, _ipo, _out, stock_type, status = row
    if status == "1" and stock_type == "1":
        num = code.split(".")[1]
        if num.startswith(("4", "8")):
            excluded_bj += 1
            continue
        symbols.append(num)
logger.info(f"获取股票列表完成，共 {len(symbols)} 只（已排除 {excluded_bj} 只北交所）")
```

**如果必须支持北交所**：baostock 这条路走不通，需要在 `DataEngine` 里为 4/8 开头的代码接第二个数据源（akshare 的 `stock_zh_a_hist` 或 tushare），并在 `_to_baostock_code` 返回空串时路由到该源。

---

<a id="p1-4"></a>
## P1-4 · 非 JSON 响应会让整个进程退出（已确认）

**位置**：[feishu.py:120-140](../sequoia_x/notify/feishu.py#L120-L140)

```python
try:
    resp = requests.post(...)
    resp_json = resp.json()            # ← 如果响应不是 JSON，这里抛 json.JSONDecodeError
    if resp.status_code != 200 or resp_json.get("code") != 0:
        logger.error(...)
except requests.RequestException as exc:    # ← 只接网络异常
    logger.error(...)
```

`json.JSONDecodeError` 继承自 `ValueError`，**不是** `requests.RequestException` 的子类。所以：

- 公司代理/网关拦了请求返回 HTML 502 页
- 飞书风控返回纯文本
- DNS 劫持返回广告页

以上任一情况 → `ValueError` 逃出 `send()` → 冒泡到 `main.py:95` 的兜底 → `sys.exit(1)` → **后面的策略全部不执行**。

而且 docstring 明确承诺"不抛出异常"，实际并不成立。

**修法**：

```python
try:
    resp = requests.post(..., timeout=10)
except requests.RequestException as exc:
    logger.error(f"飞书推送请求异常 [{webhook_key}]：{exc}")
    return

try:
    resp_json = resp.json()
except ValueError:
    logger.error(f"飞书返回非 JSON [{webhook_key}] HTTP={resp.status_code} body={resp.text[:200]}")
    return

if resp.status_code != 200 or resp_json.get("code") != 0:
    logger.error(f"飞书推送失败 [{webhook_key}] HTTP={resp.status_code} 响应={resp.text}")
else:
    logger.info(f"飞书推送成功 [{webhook_key}]，共 {len(symbols)} 只股票")
```

---

<a id="p1-5"></a>
## P1-5 · RPS 策略整表载入内存（✅ 已修复）

> **2026-11 改造**：改为一条 SQL 聚合，每只股票只返回 4 个数值（现价、120 日前收盘、
> 120 日最高价、窗口样本数），行数从 355 万降到 5,224，内存 **440 MB → 0.4 MB**，
> 耗时 **10.5 s → 1.5 s**，208 只选股结果逐只比对完全一致。
> 实现见 [05 · 第 8 节](05-strategy-system.md)。以下保留原始分析。

**位置**：[rps_breakout.py:18-19](../sequoia_x/strategy/rps_breakout.py#L18-L19)

```python
df = pd.read_sql("SELECT symbol, date, close, high FROM stock_daily", conn)
```

355 万行 × 4 列。pandas 3.0 下的粗略占用：

| 列 | dtype | 估算 |
|---|---|---|
| `symbol`（字符串 5 万+ 重复值） | `str`（3.0 默认） | ~50~150 MB |
| `date`（转 datetime64 后 8 字节） | `datetime64[ns]` | 28 MB |
| `close`, `high` | `float64` 8 字节 | 各 28 MB |
| 中间列 `close_shift` / `pct_change` / `roll_high` | 同上 | +84 MB |
| groupby / sort 的临时副本 | — | 可能翻倍 |

**峰值 400 MB ~ 1 GB**，在 1 GB 内存的 VPS 上极易被 OOM killer 杀掉（而且是在跑完 6 个策略之后才被杀，最气人）。

**修法（不改策略语义）**：让 SQLite 先做聚合，只把"每只股票的最新一行 + 120 日前收盘 + 120 日最高"取出来。

```python
# 只取需要的聚合结果：最新一天、该股 120 日前的收盘、该股 120 日最高
df = pd.read_sql("""
    WITH latest AS (
        SELECT symbol, MAX(date) AS d FROM stock_daily GROUP BY symbol
    )
    SELECT s.symbol, s.close,
           (SELECT close FROM stock_daily p
             WHERE p.symbol = s.symbol AND p.date <= s.date
             ORDER BY p.date DESC LIMIT 1 OFFSET 120) AS close_120,
           (SELECT MAX(high) FROM stock_daily h
             WHERE h.symbol = s.symbol AND h.date <= s.date) AS roll_high
    FROM stock_daily s JOIN latest ON s.symbol = latest.symbol AND s.date = latest.d
""", conn)
```

（这样只有 5,224 行，内存从 GB 级降到 MB 级。注意子查询性能，最好先建 `(symbol, date)` 索引——已有。）

**修法（更简单）**：分批读。`pd.read_sql` 支持 `chunksize`，但要跨批做排名比较麻烦，不如上 SQL。

**折中**：至少在加载前只取最近 250 个交易日，历史数据不参与 RPS。

---

<a id="p1-6"></a>
## P1-6 · `get_all_symbols()` 会超时，`--backfill` 拿到空清单（✅ 已修复）

**位置**：[engine.py](../sequoia_x/data/engine.py) 的 `get_all_symbols()`

**现象**（实测，2026-11-23）：

```
query_stock_basic(code_name="", code="")  →  err=10002007「网络接收异常」
                                            20.7 s 后返回 0 行
```

`--backfill` 的第一步就是 `get_all_symbols()`，拿到空列表后 `backfill()` 直接结束，
日志只会打印"获取股票列表完成，共 0 只"，**看起来像正常跑完**。也就是说：在这台机器上
**整个回填功能是不可用的**，而这个问题跟本次性能改造无关，一直存在。

顺带实测了其它全市场数据源：

| 接口 | 耗时 | 结果 |
|---|---|---|
| `bs.query_stock_basic("", "")` | 20.7 s | ❌ `err=10002007`，0 行 |
| `bs.query_all_stock(day)` | 30.2 s | ✅ 7,452 行（含指数/ETF，需过滤） |
| `bs.query_stock_basic("sh.600000")` | 0.3 s | ✅ 单只正常 |
| `akshare.stock_info_a_code_name()` | 26.9 s | ✅ 5,581 行 |
| `akshare.stock_zh_a_spot_em()` | 8.5 s | ❌ `ConnectionError` 断连 |

**修法（已实现）**：三级回退链，第一个拿到结果的即返回。

```python
for label, fetch in (("query_stock_basic", self._symbols_from_basic),
                     ("query_all_stock", self._symbols_from_all_stock)):
    symbols = fetch()          # 各自负责 login/logout，失败抛异常被捕获
    if symbols:
        return symbols
return self.get_local_symbols()   # 断网兜底：库里已有的代码，回填可续传
```

配套细节：

- `_symbols_from_all_stock()` 用 `MAX(date)` 锚定最近交易日，并往回试 10 个自然日，
  避免周末/节假日拿到空名单；
- 过滤走 [cache.py 的 `to_stock_symbol`](../sequoia_x/data/cache.py)：**必须**排除
  `sh.000001`（上证指数）这类代码，否则会把深市 `000001` 的名称/身份覆盖掉；
- 单测 `tests/test_engine_symbols.py` 覆盖了四种回退路径（不联网）。

**仍建议**：`--backfill` 结束后若 `failed > 0` 或 `success == 0`，应以非 0 退出码结束，
让 cron 能感知（对应 P2-9）。

---

<a id="p2-1"></a>
## P2-1 · `get_all_symbols` 里两个变量名写反了（✅ 已修复）

> **2026-11 改造**：`_symbols_from_basic()` 里改成元组解包
> `code, _name, _ipo, _out, stock_type, status = row[:6]`，名字与 baostock
> 字段顺序一一对应，不再有"变量名反了但结果碰巧对"的陷阱。以下保留原始分析。

**位置**：[engine.py:314-318](../sequoia_x/data/engine.py#L314-L318)

```python
code       = row[0]
status     = row[4]        # ← 实际是 type（1=股票 2=指数 3=其它 4=可转债 5=ETF）
stock_type = row[5]        # ← 实际是 status（1=上市 0=退市）
if status == "1" and stock_type == "1":
```

实测 baostock `query_stock_basic` 的 `rs.fields`：

```
['code', 'code_name', 'ipoDate', 'outDate', 'type', 'status']
    0        1          2          3         4       5
```

**结果是对的**（因为两个条件都要求 `== "1"`，等价于 `type==1 && status==1`），但**名字全错**。任何人想改成"只排除指数"或"包含 ETF"时都会踩坑。

**修法**：

```python
row = rs.get_row_data()
code, _name, _ipo, _out, stock_type, status = row          # 解包，自解释
if status == "1" and stock_type == "1":
    symbols.append(code.split(".")[1])
```

---

<a id="p2-2"></a>
## P2-2 · `settings_customise_sources` 是死代码，还污染了 `os.environ`（已确认）

**位置**：[config.py:19-43](../sequoia_x/core/config.py#L19-L43)

这个方法：

1. `from pydantic_settings import EnvSettingsSource` —— 导入后从未使用
2. `original_init = kwargs.get("init_settings")` —— 取到后从未使用
3. `os.environ.setdefault("_STRATEGY_WEBHOOKS_PARSED", "1")` —— **往环境变量里塞了个无意义的键**
4. `cls._parsed_strategy_webhooks = webhooks` —— 挂在类上，全项目无人读取
5. `return sources` —— 和父类实现完全一致，等于什么都没做

真正干活的是 `model_post_init`（config.py:45-57）。这个方法是**历史残留**。

**修法**：整段删除（第 19~43 行）。

---

<a id="p2-3"></a>
## P2-3 · 策略 webhook 静默依赖 `load_dotenv()`（已确认）

**位置**：`main.py:10-11` + `config.py:45-57`

`model_post_init` 遍历的是 **`os.environ`**，而 pydantic 的 `env_file=".env"` **不会**把 `.env` 的内容写进 `os.environ`。所以必须靠 `main.py` 顶部的 `load_dotenv()` 先把 `.env` 注入环境变量。

| 场景 | 结果 |
|---|---|
| 有 `load_dotenv()` | 7 个策略各推各的群 ✅ |
| 删掉 `load_dotenv()` | `strategy_webhooks` 为空 → **全部 fallback 到默认机器人**，无任何报错 ❌ |
| 用 `python -m sequoia_x.xxx` 或自己的脚本 | 同上 ❌ |

**修法（任选）**：

A. 在 `Settings.model_post_init` 里也读 `.env`（用 `dotenv_values`），不依赖外部调用：

```python
def model_post_init(self, __context: object) -> None:
    from dotenv import dotenv_values
    webhooks = dict(dotenv_values(".env"))          # 先读文件
    webhooks.update(os.environ)                     # 环境变量优先
    ...
```

B. 在 `config.py` 模块顶层就 `load_dotenv()`（让"读配置"这件事自带 .env 支持）：

```python
from dotenv import load_dotenv
load_dotenv()
```

C. 至少在 `get_settings()` 里断言"如果有 FEISHU_WEBHOOK_URL 但 strategy_webhooks 为空"时打一条 WARNING。这是最低成本的"防沉默失败"。

---

<a id="p2-4"></a>
## P2-4 · MaVolume 的 `_MIN_BARS` 少 1（✅ 已修复：现为 21）

> 2026-11 改造时一并修正：`MaVolumeStrategy._MIN_BARS = 21`，
> `UptrendLimitDownStrategy._MIN_BARS = 61`（原来写 60，靠 `pd.isna` 兜住）。
> 两者都不改变选股结果（原来那两类情况本来就选不出来）。以下保留原始分析。

**位置**：[ma_volume.py:37](../sequoia_x/strategy/ma_volume.py#L37)

```python
if len(df) < 20:        # 应为 21
    continue
```

`df.iloc[-2]`（昨日）的 `ma20` 在 `len(df) == 20` 时仍是 `NaN`（`rolling(20)` 在索引 18 处只有 19 个观测）。因为 `NaN < NaN` 为 `False`，所以只是"不入选"，不会误选、不会崩。

**修法**：改成 `if len(df) < 21:`，并把其他策略的 `_MIN_BARS` 统一核查一遍（见 [05 策略系统 · _MIN_BARS 一览](05-strategy-system.md)）。

---

<a id="p2-5"></a>
## P2-5 · 7 个策略重复扫全市场，且每只股票都新建 SQLite 连接（✅ 已修复）

> 2026-11 改造：新增 `MarketSnapshot`（`sequoia_x/data/snapshot.py`），
> 策略层改为"一次查 130 个交易日的全市场数据 + 全市场向量化"。
> 实测 5 个 K 线策略合计 125 s → 约 15 s（含 RPS 的 10.5 s），选股结果逐只比对**完全一致**。
> 详见 [05 策略系统 · 第 2 节](05-strategy-system.md)。
> `get_ohlcv()` 仍保留给单只股票诊断用。以下保留原始分析。

**位置**：7 个策略的 `run()` + [engine.py:80-87](../sequoia_x/data/engine.py#L80-L87)

```python
def get_ohlcv(self, symbol):
    with sqlite3.connect(self.db_path) as conn:     # ← 每次调用新建连接
        return pd.read_sql(...)
```

单次运行的开销：

```
7 个策略 × 5,224 只股票 = 36,568 次
   ├─ 36,568 次 sqlite3.connect/open/close
   ├─ 36,568 次查询（每次约 700 行）
   └─ 同一份 MA20 被重复计算 5 次（MaVolume / Uptrend / 其他）
```

**修法 A（改连接）**：`DataEngine` 持有一个长连接（`check_same_thread=False`），或加一个 `lru_cache` 缓存最近读过的股票。

**修法 B（改架构，收益最大）**：一次读全市场 → 在 pandas 里一次性算好所有指标 → 各策略只做布尔过滤。

```python
# DataEngine 新增
def load_all(self) -> pd.DataFrame:
    with sqlite3.connect(self.db_path) as conn:
        return pd.read_sql("SELECT symbol,date,open,high,low,close,volume,turnover "
                           "FROM stock_daily ORDER BY symbol,date", conn)
```

但这一条会撞上 P1-5 的内存问题，所以**必须先解决全表内存占用**（比如只取最近 250 个交易日）。两者应当一起改。

**修法 C（最小改动）**：给 `get_ohlcv` 加 `functools.lru_cache`，让每个策略内部不重复读同一只票（但跨策略仍重复）。

---

<a id="p2-6"></a>
## P2-6 · 测试真的会访问外网（已确认）

**位置**：[tests/test_feishu.py](../tests/test_feishu.py)

只 mock 了 `requests.post`，但 `FeishuNotifier._build_card()` 内部调 `_get_stock_names()` → `baostock.login()` + `query_stock_basic()`，**没有 mock**。

后果：
- CI 里偶发失败（网络抖动）
- 每个 hypothesis 用例几十次真实查询 × `max_examples=50` → 可能跑几分钟
- 离线环境下"通过"但实际测的是 fallback 路径

**修法**：

```python
@patch.object(FeishuNotifier, "_get_stock_names", return_value={})
def test_notification_contains_all_symbols(mock_names, symbols): ...
```

或用 `conftest.py` 里一个 autouse fixture 统一 patch。

---

<a id="p2-7"></a>
## P2-7 · 所有策略阈值都是魔法数字（已确认）

散落在各处的硬编码：

| 值 | 含义 | 位置 |
|---|---|---|
| `5`, `20` | 均线周期 | ma_volume.py:41-43 |
| `1.5` | 放量倍数 | ma_volume.py:53 |
| `20`, `100_000_000` | 突破窗口、成交额下限 | turtle_trade.py:81, 92 |
| `1.6`, `1.15`, `0.8`, `0.6` | 旗形四条件 | high_tight_flag.py:55-62 |
| `1.095`, `2.0` | 涨停幅度、放量倍数 | limit_up_shakeout.py:49, 53 |
| `0.905`, `2.0` | 跌停幅度、放量倍数 | uptrend_limit_down.py:56-57 |
| `120`, `90`, `0.90` | RPS 窗口/阈值/突破线 | rps_breakout.py:13-14, 52 |
| `7` | 定增回看天数 | private_placement.py:24 |

**问题**：想试"20 日改 30 日"必须改代码 + 重启；也没法做参数扫描回测。

**修法（渐进）**：

1. 把可调参数提到类属性（像 `_MIN_BARS` 那样），至少集中在一处；
2. 再进一步，让 `Settings` 支持 `STRATEGY_PARAMS__MA_VOLUME__VOL_SURGE=1.8` 这类嵌套配置（pydantic 支持嵌套模型 + `__` 分隔符）；
3. 最终目标：每个策略的参数从构造函数注入，`main.py` 从配置读。

---

<a id="p2-8"></a>
## P2-8 · 没有 ST / 停牌 / 次新股过滤（已确认）

全项目**没有一处**检查：
- 是否 ST / *ST（`query_stock_basic` 的 `code_name` 里含 "ST"，或专门的 `isST` 字段）
- 是否停牌（`volume == 0` 只过滤了当天，但没有"连续停牌"判断）
- 上市天数（`ipoDate` 可用，但没存进库）

**后果**：
- 选股结果里可能出现 ST 股（涨跌幅 5%，被 `1.095` 条件误判）
- 次新股（上市 7 天）会因为 `_MIN_BARS` 被跳过，但上市 30 天的会被选中，而 30 天的均线毫无统计意义
- 长期停牌后复牌的票，K 线中间有巨大缺口，所有均线/涨幅都是错的

**修法**：在 `stock_daily` 加 `is_st`、`tradestatus` 字段（baostock 的 `query_history_k_data_plus` 支持 `tradestatus,isST`），或在策略里统一加一个 `_is_tradable(df)` 前置过滤。

---

<a id="p2-9"></a>
## P2-9 · 回填失败只计数，不自动重试（已确认）

**位置**：[engine.py:297](../sequoia_x/data/engine.py#L297)

```python
logger.info(f"回填完成 — 成功: {success} | 跳过: {skipped} | 失败: {failed}")
```

`failed > 0` 时进程仍然退出码 0，cron 完全感知不到"有 4 只股票数据缺失"。

**修法**：

```python
if failed:
    logger.warning(f"{failed} 只股票回填失败，建议重跑")
    sys.exit(2)          # 让 cron/监控能识别
```

或加 `--retry-failed` 参数，把失败列表落盘再跑。

---

<a id="p2-10"></a>
## P2-10 · 没有交易日历判断（已确认）

**位置**：[engine.py:102](../sequoia_x/data/engine.py#L102)、[engine.py:171](../sequoia_x/data/engine.py#L171)

```python
today_str = date.today().strftime("%Y-%m-%d")     # 不管今天是不是交易日
```

周末/节假日运行时：为全部 5,224 只股票建任务 → 8 个进程各发几百个必然为空的查询 → 白等 1~2 分钟 → 日志写"无新数据（可能非交易日）"。

**修法**：baostock 有现成接口：

```python
def is_trade_date(d: str) -> bool:
    import baostock as bs
    rs = bs.query_trade_dates(start_date=d, end_date=d)
    while rs.next():
        if rs.get_row_data()[1] == "0":      # is_trading_day
            return False
    return True
```

在 `main()` 里先判断，非交易日直接 `return`（并可加 `--force` 跳过检查）。

---

<a id="p2-11"></a>
## P2-11 · RPS 绕过数据层直接读库（已确认）

**位置**：[rps_breakout.py:18-19](../sequoia_x/strategy/rps_breakout.py#L18-L19)

```python
with sqlite3.connect(self.engine.db_path) as conn:
    df = pd.read_sql("SELECT symbol, date, close, high FROM stock_daily", conn)
```

`DataEngine` 暴露了 `get_ohlcv()` 作为唯一读入口，但 RPS 直接摸 `engine.db_path` 自己连库。这是**封装破坏**（相当于在 Java 里绕过 Repository 直接 `DriverManager.getConnection`）。

**为什么可以接受**：一次性读全表确实没法用 `get_ohlcv(symbol)` 表达，逐只查 5,224 次更慢。
**怎么改才对**：在 `DataEngine` 上补一个 `load_all_ohlcv() -> DataFrame` 方法，RPS 调它。这样换数据源时只改一处。

---

<a id="p3-1"></a>
## P3-1 · 未使用的导入与死变量（已确认）

| 位置 | 代码 | 问题 |
|---|---|---|
| `ma_volume.py:3` | `import pandas as pd` | 全文未用 `pd.` |
| `high_tight_flag.py:3` | 同上 | 同上 |
| `limit_up_shakeout.py:3` | 同上 | 同上 |
| `main.py:13` | `from datetime import date` | 未使用（文件里也没用到 `date`） |
| `config.py:22` | `from pydantic_settings import EnvSettingsSource` | 未使用 |
| `config.py:37` | `original_init = kwargs.get("init_settings")` | F841 未使用变量 |

一条 `ruff check . --fix` 就能自动清掉大部分。

---

<a id="p3-2"></a>
## P3-2 · ruff 配置位置已过时（已确认）

**位置**：`pyproject.toml:22-25`

```toml
[tool.ruff]
line-length = 100
target-version = "py311"
select = ["E", "F", "I", "UP"]      # ← 0.2 起应为 [tool.ruff.lint] select
```

新版 ruff 会警告 `select` 位置不正确，并可能**忽略**这条配置（等于只跑默认规则）。

**修法**见 [08 第 5 节](08-testing-and-quality.md#5-代码规范ruff)。

另外 `target-version = "py311"` 与 `requires-python = ">=3.10"` 不一致，建议统一成 `py310`。

---

<a id="p3-3"></a>
## P3-3 · 重复索引（已确认）

```sql
UNIQUE (symbol, date)                                        -- 自动创建 sqlite_autoindex
CREATE INDEX idx_symbol_date ON stock_daily (symbol, date);   -- 完全重复
```

实测 `sqlite_master` 里两者都存在。多余索引会：
- 多占约 50~80 MB 磁盘
- 每次 INSERT 多维护一棵 B 树，写入变慢

**修法**：删掉 `_CREATE_INDEX_SQL`（对已存在的库执行 `DROP INDEX idx_symbol_date;`）。

---

<a id="p3-4"></a>
## P3-4 · 文档与实现不一致（已确认）

| 位置 | 文档说 | 实际 |
|---|---|---|
| `high_tight_flag.py:14-17` | 3 个条件 | 4 个（多了 `high_level`） |
| `main.py:2-6` | 日常模式"8进程增量补数据" | 准确说是"补每只股票的缺口区间" |
| `README.md:26-35` | 6 个策略 | 7 个（漏 `PrivatePlacement`） |
| `README.md:98-105` | 目录结构 | 漏 `private_placement.py` |
| `engine.py:98` docstring | "拉取增量数据" | 实际是从 `MAX(date)+1` 到今天的**区间** |

**修法**：改代码注释比改 README 便宜得多，建议两者都对齐一次。本 Wiki 已经记录了实际行为，可作为基准。

---

<a id="p3-5"></a>
## P3-5 · `except Exception` 吞掉一切（✅ 已消除于 K 线策略）

> 2026-11 改造后，5 个 K 线策略不再有"逐只股票"的循环，也就不再有逐只 `except`。
> 个别脏数据由 `NaN` 比较恒为 `False` 自然过滤，或由 `MarketSnapshot` 的参数校验直接报错。
> `main.py` 与 `FeishuNotifier` 的兜底 `except` 仍保留（这是合理的）。以下保留原始分析。

每只股票一个 `try/except Exception`（7 个策略都是），加上 `except sqlite3.IntegrityError: pass`、`except Exception: return []`。

**好处**：5,224 只里坏一只不影响整次运行——这是**正确的取舍**。
**坏处**：调试时"某只票为什么没选出来"只能靠 `grep 计算失败`，而且日志级别是 WARNING，容易被刷掉。

**改进**：
- 加一个计数器，最后汇总 `共 N 只计算失败` 一条日志；
- 或把失败的 symbol 收集起来落盘到 `data/failed.txt`。

---

<a id="p3-6"></a>
## P3-6 · 工程化空白（已确认）

| 缺什么 | 影响 | 建议 |
|---|---|---|
| 无 CI | 提交不跑测试/lint | GitHub Actions，见 [08 第 8 节](08-testing-and-quality.md#8-如果要加-ci) |
| 无 mypy/pyright | 类型注解无校验 | 先对 `core/` + `data/` 开 `mypy --ignore-missing-imports` |
| 无回测 / 无信号落库 | **无法回答"A 策略到底有没有用"** | 加一张 `signals(date, strategy, symbol)` 表，每天把结果落库；积累 3 个月就能算胜率 |
| 无 Dockerfile | 部署靠手装 | 一个 `python:3.12-slim` + `uv sync` 就够 |
| 无 CHANGELOG | 版本 2.0.0 的来源不可考 | 配合语义化版本 |
| `build/`、`Sequioa.egg-info/` 残留本地 | 混淆搜索 | 已在 `.gitignore`，可 `rm -rf` |

**最值得做的是"信号落库"**：现在这套系统每天跑完，除了飞书卡片什么都没留下。做任何策略评估都要从零开始。加一张表 + 一段 `INSERT`，成本极低，价值极高。

---

## 修复路线图（建议顺序）

```
第 1 周（防止数据/线上事故）
  ├─ P1-1 修 DELETE-by-date          ← 最高优先，数据是资产
  ├─ P1-4 resp.json 异常处理          ← 20 行代码
  └─ P3-2 ruff 配置 + 跑一次 --fix    ← 5 分钟

第 2 周（提升选股质量）
  ├─ P1-2 板块感知的涨跌停判定
  ├─ P1-3 明确北交所策略（排除 or 接第二个数据源）
  ├─ P2-8 ST/停牌过滤（需要先加字段，见 HOWTO）
  └─ P2-10 交易日历

第 3~4 周（性能与可维护性）
  ├─ P1-5 RPS 改 SQL 聚合
  ├─ P2-5 数据层批量读取 + 指标共享
  ├─ P2-7 阈值提到配置
  └─ P2-1/P2-2/P2-3 清理配置模块

持续
  ├─ P2-6 测试隔离网络
  ├─ P3-6 信号落库 + CI + 回测
  └─ 补 5~8 号属性测试（数据同步）
```

---

## 附：我认为**设计得好**的地方

不要只看问题清单，这套代码有几个地方做得比大多数个人量化项目好：

1. **数据层封装干净**：策略只依赖 3 个方法（`get_local_symbols` / `get_ohlcv` / `db_path`），换数据源成本很低。
2. **策略模式用得标准**：加策略只需新增一个文件 + `main.py` 一行，符合开闭原则。
3. **幂等设计**：`UNIQUE(symbol,date)` + 断点续传，让"中断重跑"成为安全操作——这是批处理系统最重要的性质。
4. **后复权 + 增量**的取舍是对的，README 也解释清楚了原因。
5. **失败不拖垮全局**：单只股票异常、单次推送失败都不影响其他部分（P1-4 是唯一漏网的）。
6. **配置用 pydantic 而非 `os.getenv` 到处取**，类型和必填校验一步到位。
7. **属性测试**的思路用对了地方（唯一约束、配置优先级、返回值类型确实是"性质"）。
8. 代码风格统一，docstring 覆盖率高，注释写得有信息量（"拒绝郑州煤电式的高开低走大阴线"这种注释比 `// 判断阳线` 有用一百倍）。

---

下一步：动手改 → [10 常见任务 HOWTO](10-how-to-recipes.md)
