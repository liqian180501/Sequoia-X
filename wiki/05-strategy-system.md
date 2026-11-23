# 05 · 策略系统

> 目录：[sequoia_x/strategy/](../sequoia_x/strategy/)
> 一句话职责：**给定每只股票的历史 K 线，判断它今天是否满足某个形态，返回代码列表。**

---

## 1. 契约：BaseStrategy

[base.py](../sequoia_x/strategy/base.py) 只有 42 行，但定义了整个策略层的协议：

```python
class BaseStrategy(ABC):
    webhook_key: str = "default"                 # 路由键：决定推到哪个飞书机器人

    def __init__(self, engine: DataEngine, settings: Settings) -> None:
        self.engine = engine                     # 数据访问
        self.settings = settings                 # 配置访问

    @abstractmethod
    def run(self) -> list[str]:
        ...
```

**子类必须做三件事**：

1. 继承 `BaseStrategy`
2. 覆盖 `webhook_key`（否则全部推到默认机器人）
3. 实现 `run() -> list[str]`（返回纯数字代码，如 `['600519', '000001']`；无结果返回 `[]`）

仅此而已。策略**不应该**自己发 HTTP、自己写库、自己读 `.env`——那些都是 `main.py` 和 `FeishuNotifier` 的事。这是标准的"策略只算、编排在外部"的分层。

---

## 2. 六个策略共用的骨架（v2 批量向量化）

> **2026-11 性能改造（P0+P1）后的现状。**
> 旧实现是"逐只 `get_ohlcv` + 逐只计算"，5 个策略各自把同一份行情读一遍（2.6 万次查询），
> 实测占策略层耗时的 75%。现在改为**一次批量读 + 全市场向量化**，5 个策略共享同一份
> `MarketSnapshot`，指标只算一次。

### 2.1 新的执行骨架

```python
def run(self) -> list[str]:
    snap = self.engine.get_snapshot()          # ① 全市场最近 130 个交易日（进程内只加载一次）

    ma5  = snap.rolling("close", 5)            # ② 按股票分组安全的滚动指标（带缓存，跨策略共享）
    ma20 = snap.rolling("close", 20)
    ma5_prev  = snap.rolling_shifted("close", 1, 5)    # "昨日 ma5"
    ma20_prev = snap.rolling_shifted("close", 1, 20)

    condition = (ma5_prev < ma20_prev) & (ma5 > ma20)  # ③ 全市场一次算完（布尔 Series）
    selected = snap.latest(condition & snap.enough_bars(self._MIN_BARS))  # ④ 只取"每只股票最后一行"

    logger.info(f"MaVolumeStrategy 选出 {len(selected)} 只股票")
    return selected
```

### 2.2 为什么这样写

| 设计 | 原因 |
|---|---|
| `engine.get_snapshot()` 而不是 `get_local_symbols()` + `get_ohlcv()` | 一条 SQL 取回 67 万行（≈2.6 s），替代 5,224 次单只查询（≈18.6 s） |
| 指标缓存在 `MarketSnapshot` 实例上 | 7 个策略里 `MA20`、`20 日均量` 会被多次用到，只算一次 |
| 不用 `groupby(...).transform(...)` | 每次分组都要回调进 Python，实测比"整列 rolling + 掩码"慢 **43 倍** |
| `snap.latest(cond)` 而不是逐行判断 | 用 `cumcount(ascending=False) == 0` 直接定位每只股票的最后一行 |
| `snap.enough_bars(n)` 代替 `if len(df) < n: continue` | 向量化版本：`bars >= n` 广播成整列布尔 |
| **不再需要 `try/except` 包住单只股票** | 向量化后没有"单只"的概念；个别脏数据由 `NaN` 比较恒为 `False` 自然过滤 |

> ⚠️ **语义差异（唯一一处）**：旧实现的 `_MIN_BARS` 判断的是该股票的**全部历史**行数，
> 新实现判断的是**快照窗口（默认 130 个交易日）内**的行数。对长期停牌股，旧代码会拿陈旧数据
> 参与计算，新代码因为窗口内行数不足而直接跳过——这更符合预期，但在极端情况下可能少选几只。
> 用 `SNAPSHOT_DAYS`（默认 130）可调大窗口提高保真度。

### 2.3 改造实测（同一份数据库，逐只比对）

| 策略 | 改造前 | 改造后 | 选股结果 |
|---|---:|---:|---|
| MaVolume | 27.7 s | 4.6 s（含首次快照加载 4.5 s） | 15 只，**完全一致** |
| TurtleTrade | 21.7 s | 0.05 s | 54 只，**完全一致** |
| HighTightFlag | 21.1 s | 0.20 s | 0 只，**完全一致** |
| LimitUpShakeout | 18.9 s | 0.02 s | 1 只，**完全一致** |
| UptrendLimitDown | 25.2 s | 0.04 s | 2 只，**完全一致** |
| RpsBreakout | 10.5 s | **1.5 s**（SQL 聚合，见第 8 节） | 208 只，**完全一致** |
| **合计** | **125.1 s** | **6.4 s**（热跑，含快照 4.5 s） | 6/6 完全一致 |

### `_MIN_BARS` 一览（这是理解"为什么某只票从不入选"的关键）

| 策略 | `_MIN_BARS` | 说明 |
|---|---|---|
| `LimitUpShakeout` | 3 | 只看最近 3 根 |
| `HighTightFlag` | 40 | 40 日区间 + 20 日均量 |
| `MaVolume` | **21** | 20 日均线 + 昨日一行（旧实现写的 20 偏小，已修正） |
| `TurtleTrade` | 21 | 20 日窗口 + 今日 |
| `UptrendLimitDown` | **61** | 60 日均线（昨日值）+ 今日（旧实现写 60 靠 NaN 兜住，已修正） |
| `RpsBreakout` | 无 | 需要 121 根；由一条 SQL 聚合完成，见第 8 节 |
| `PrivatePlacement` | 无 | 完全不用 K 线 |

> `snap.enough_bars()` 统计的是**快照窗口内**的 K 线数；窗口下限由 `Settings.snapshot_days`
> 控制（默认 130，最小 62）。

---

## 3. MaVolumeStrategy — 均线金叉 + 放量

> 文件：`ma_volume.py`（40 行）· 路由键：`ma_volume` · `_MIN_BARS = 21`

**想捕捉的形态**：短均线上穿长均线，同时成交量放大 —— 经典的"突破启动"信号。

### 条件

| # | 条件 | 公式 |
|---|---|---|
| 1 | 5 日均线上穿 20 日均线（金叉） | `ma5[t-1] < ma20[t-1]` 且 `ma5[t] > ma20[t]` |
| 2 | 今日放量 | `volume[t] > 1.5 × mean(volume[t-20 .. t-1])` |

其中 `ma5[t] = mean(close[t-4..t])`，`ma20[t] = mean(close[t-19..t])`。

注意条件 2 的"20 日均量"**包含今天**（`rolling(20).mean()` 的最后一个值），这会让今天的巨量把自己所在的均值抬高，稍微减弱了"放量"的敏感度——与 `HighTightFlag` 里用 `iloc[-21:-1]`（不含今天）的做法不一致，属于风格不统一。

### 核心代码

```python
snap = self.engine.get_snapshot()                    # ma_volume.py

ma5  = snap.rolling("close", 5)                      # 含当日
ma20 = snap.rolling("close", 20)
ma5_prev  = snap.rolling_shifted("close", 1, 5)      # 昨日 ma5
ma20_prev = snap.rolling_shifted("close", 1, 20)     # 昨日 ma20
vol_ma20  = snap.rolling("volume", 20)               # 含当日

golden_cross = (ma5_prev < ma20_prev) & (ma5 > ma20)
volume_surge = snap.col("volume") > vol_ma20 * self._VOL_SURGE_RATIO
selected = snap.latest(golden_cross & volume_surge & snap.enough_bars(self._MIN_BARS))
```

### 图形直觉

```
     收盘价
       │        ╱╲            ← 今天：短均线上穿 + 放量
       │      ╱   ╲
       │    ╱  ──── ╲  ma5（快）
       │  ╱ ────────  ╲ ma20（慢）
       │╱______________╲____
              ↑
           金叉点（必须正好是"昨天在下、今天在上"）
```

### 注意

- 金叉判定要求**恰好发生在今天**。如果金叉是三天前，即使今天均线仍多头排列也不会选。这是"事件型"而非"状态型"策略。
- 20 日均量含今天 → 见上。
- 排序：无（按股票代码升序输出）。

---

## 4. TurtleTradeStrategy — 海龟突破（本项目最复杂的策略）

> 文件：`turtle_trade.py`（84 行）· 路由键：`turtle` · `_MIN_BARS = 21`

**想捕捉的形态**：创 20 日新高 + 有真实资金参与 + 不是高开低走的假阳线。

### 条件

| # | 条件 | 公式 | 实现 |
|---|---|---|---|
| 1 | 突破 20 日新高 | `close[t] > max(high[t-20 .. t-1])` | `snap.rolling_shifted("high", 1, 20, "max")` |
| 2 | 流动性过亿 | `turnover[t] > 100_000_000` | `snap.col("turnover") > _MIN_TURNOVER` |
| 3 | 实体阳线（防空头陷阱） | `close[t] > open[t]` | `close > snap.col("open")` |
| 4 | 真涨（非假阳线） | `close[t] > close[t-1]` | `close > snap.shift("close", 1)` |
| 5 | 结果按**流通市值降序**排列 | — | `_get_market_caps` + `sort` |

条件 3 和 4 是这个策略的"本地化改良"，注释写得很生动：

```python
# 【新增防守条件】拒绝郑州煤电式的高开低走大阴线！
is_yang = close > snap.col("open")          # 实体必须是阳线（红柱）
is_up   = close > snap.shift("close", 1)    # 必须是真涨，不能是假阳线
```

为什么要两条：`close > open` 只能保证"当天是阳线"，但可能是**低开后的假阳线**（比如昨天 10 元，今天开 8.5 收 8.8，仍是阳线但实际跌了 12%）。加上 `close > prev_close` 才排除了这种。

### 市值排序（唯一会二次访问 baostock 的策略）

```python
def _get_market_caps(self, symbols) -> dict[str, float]:      # turtle_trade.py
    # 流通股本 = 成交量 / (换手率% / 100)
    circulating_shares = volume / (turn / 100)
    # 流通市值 = 流通股本 × 不复权收盘价
    market_caps[symbol] = circulating_shares * close
```

- 用 `adjustflag="3"`（**不复权**）查当天数据 —— 因为市值必须用真实股价，不能用复权价。
- 换手率 `turn` 为 0 或空时跳过（`if turn > 0`），这类股票拿不到市值。
- 排序：`candidates.sort(key=lambda s: market_caps.get(s, 0), reverse=True)` —— 拿不到市值的按 0 处理，排在最后。

**为什么按市值排？** 大市值股票流动性更好，突破后更容易延续；小市值容易一日游。这是经验法则，不是硬性风控。

### 注意

- 这一步是 `O(候选数)` 次**同步 HTTP 查询**，在 `bs.login()` 之后串行执行。候选 30 只就要 30 次往返。如果候选有几百只，这里会很慢。
- 如果当天非交易日 / 盘中运行，`query_history_k_data_plus(start=today, end=today)` 返回空 → 所有市值缺失 → 排序等于没排（全部 0，`sort` 稳定，保持原顺序）。
- 市值计算依赖"成交量单位是股、换手率是百分数"这个 baostock 约定。如果换数据源，这里是**最容易静默算错**的地方。

---

## 5. HighTightFlagStrategy — 高而窄的旗形

> 文件：`high_tight_flag.py`（52 行）· 路由键：`flag` · `_MIN_BARS = 40`

**想捕捉的形态**：欧奈尔 CANSLIM 里的 "High Tight Flag" —— 先暴涨（旗杆），然后极窄幅横盘（旗面），缩量整理后等待二次突破。

### 条件（代码里其实是 4 条，docstring 只写了 3 条）

| # | 条件 | 公式 | 阈值含义 |
|---|---|---|---|
| 1 | 强动量（旗杆） | `max(high[40]) / min(low[40]) > 1.6` | 40 日内振幅超 60% |
| 2 | 极度收敛（旗面） | `max(high[10]) / min(low[10]) < 1.15` | 最近 10 日振幅低于 15% |
| 3 | 高位抗跌 | `min(low[10]) >= max(high[40]) × 0.8` | 回调不破前高 80% |
| 4 | 缩量 | `volume[t] < 0.6 × mean(volume[t-20 .. t-1])` | 今天的量只有前 20 日均量的 6 成 |

### 核心代码

```python
high40 = snap.rolling("high", 40, "max")                   # high_tight_flag.py
low40  = snap.rolling("low", 40, "min")
high10 = snap.rolling("high", 10, "max")
low10  = snap.rolling("low", 10, "min")

momentum      = high40 / low40 > self._MOMENTUM_RATIO      # 1.6
consolidation = high10 / low10 < self._TIGHT_RATIO         # 1.15
high_level    = low10 >= high40 * self._HIGH_LEVEL_RATIO   # 0.8（docstring 曾漏写）
vol_ma20_prev = snap.rolling_shifted("volume", 1, 20)      # 不含今天的 20 日均量
shrink        = snap.col("volume") < vol_ma20_prev * self._SHRINK_RATIO   # 0.6
valid         = (low40 > 0) & (low10 > 0)                  # 除零保护
```

### 图形直觉

```
  价格
   │  ╱╲                      ← 旗杆：40 天内从低点涨 60%+
   │ ╱  ╲    ┌──┐┌─┐┌──┐      ← 旗面：最近 10 天振幅 < 15%
   │╱    ╲───┘  └┘ └┘  └──    ← 且回调不低于旗杆顶的 80%
   │                          ← 同时成交量萎缩到均量的 60% 以下
   └──────────────────────────
       40 日窗口 ┘  └ 10 日窗口
```

### 注意

- 四个区间是**重叠**的：40 日窗口包含了 10 日窗口。所以条件 1 和 2 天然有张力——要"40 天振幅大"又"10 天振幅小"，本质就是在找"刚暴涨完正在横盘"的票。
- 除零保护：`valid = (low40 > 0) & (low10 > 0)`，避免价格为 0 时除出 `inf`。
- 这个策略选出的票通常很少（个位数），因为条件非常苛刻。

---

## 6. LimitUpShakeoutStrategy — 涨停洗盘

> 文件：`limit_up_shakeout.py`（48 行）· 路由键：`shakeout` · `_MIN_BARS = 3`

**想捕捉的形态**：昨天涨停，今天放巨量收阴（洗掉浮筹），但**不跌破昨天的收盘价**（支撑有效）。次日往往继续涨。

### 条件

| # | 条件 | 公式 |
|---|---|---|
| 1 | 昨日涨停 | `close[t-1] >= close[t-2] × 1.095` |
| 2 | 今日收阴 | `close[t] < open[t]` |
| 3 | 今日放巨量 | `volume[t] > volume[t-1] × 2.0` |
| 4 | 支撑不破 | `low[t] >= close[t-1]` |

```python
prev1_close  = snap.shift("close", 1)                        # limit_up_shakeout.py
prev2_close  = snap.shift("close", 2)
prev1_volume = snap.shift("volume", 1)

limit_up_yesterday = prev1_close >= prev2_close * self._LIMIT_UP_RATIO
bearish_today      = snap.col("close") < snap.col("open")
volume_surge       = snap.col("volume") > prev1_volume * self._VOL_SURGE_RATIO
support_hold       = snap.col("low") >= prev1_close
selected = snap.latest(... & snap.enough_bars(self._MIN_BARS))
```

### 图形直觉

```
   │              ┌──┐  ← 今天：放量收阴，但最低价不破昨收
   │      ┌───────┘  │
   │      │涨停       └──┐
   │      │               ← 昨收 = 支撑线
   └──────┴───────────────
          t-2   t-1   t
```

### 涨跌停判定：按板块（v2 已修）

旧实现把阈值写死成 `1.095`，只对主板成立；现在按代码前缀取乘数（`sequoia_x/strategy/limits.py`）：

| 板块 | 代码前缀 | 涨跌幅限制 | 涨停乘数 | 跌停乘数 |
|---|---|---|---|---|
| 主板 | 60 / 00 | 10% | 1.095 | 0.905 |
| 创业板 | 30 | **20%** | 1.195 | 0.805 |
| 科创板 | 68 | **20%** | 1.195 | 0.805 |
| 北交所 | 43/83/87/92 | **30%** | 1.295 | 0.705 |

**主板乘数与旧实现完全一致**（1.095 / 0.905），所以这是"补上其它板块"，不是改变主板行为。
0.5% 的容差用于吸收"涨跌停价四舍五入到分"带来的误差。

修正掉的问题：创业板/科创板"涨 10% 的普通阳线"以前会被误判成"昨日涨停"，
"跌 10%"会被误判成"跌停"——这两类**假信号**现在不会了。单元测试
`tests/test_limits.py::test_gem_10pct_day_is_not_limit_up` 把这条钉死了。

ST 股不需要特殊处理：主板 ST 的 5% 涨跌幅达不到 9.5% 门槛，这两个策略本来就不会在 ST 上触发。
如需退回旧口径，命令行加 `--no-board-limits`（或设 `BOARD_AWARE_LIMITS=false`）。

---

## 7. UptrendLimitDownStrategy — 上升趋势中的放量跌停

> 文件：`uptrend_limit_down.py`（42 行）· 路由键：`limit_down` · `_MIN_BARS = 61`

**想捕捉的形态**：趋势本来是好的，突然放巨量跌停 —— 往往是恐慌错杀或利空一次性释放，博反弹。

### 条件

| # | 条件 | 公式 |
|---|---|---|
| 1 | 昨日处于上升趋势（均线多头） | `ma20[t-1] > ma60[t-1]` |
| 2 | 今日放量跌停 | `close[t] <= close[t-1] × 0.905` |
| 3 | 巨量确认 | `volume[t] > 2.0 × ma20(volume)[t]` |

```python
ma20_prev = snap.rolling_shifted("close", 1, 20)                 # 昨日 ma20
ma60_prev = snap.rolling_shifted("close", 1, 60)                 # 昨日 ma60
vol_ma20  = snap.rolling("volume", 20)
prev_close = snap.shift("close", 1)

uptrend      = ma20_prev > ma60_prev
limit_down   = snap.col("close") <= prev_close * self._LIMIT_DOWN_RATIO   # 0.905
volume_surge = snap.col("volume") > vol_ma20 * self._VOL_SURGE_RATIO      # 2.0
```

趋势判定用**昨日**均线（`prev`）而不是今日，是为了避免今天的跌停把均线拉下来导致自我否定。

### 注意

- 涨跌停同样按板块判定（见第 6 节表格），创业板跌 10% 不再被当成跌停。
- 三个指标都可能是 `NaN`（窗口不足），`NaN` 参与比较恒为 `False`，所以边界情况会自动跳过；`_MIN_BARS = 61` 已显式保证 60 日均线有昨日值。
- 这个策略**本质上是在接下跌的刀**，任何风控（比如"跌停封单量""是否一字板"）都没有，实盘需谨慎。

---

## 8. RpsBreakoutStrategy — RPS 相对强度突破

> 文件：`rps_breakout.py`（62 行）· 路由键：`rps` · 无 `_MIN_BARS`

**这是唯一一个不使用 `MarketSnapshot` 的策略**：它需要每只股票 120 个交易日以上的完整历史
（`shift(120)` + 120 日滚动最高价），而快照窗口默认只有 130 个交易日，对长期停牌股会少算，
导致横截面排名偏差。因此保持"整表读入 + groupby 计算"，确保选股结果不受窗口影响。
它本身已经是一次批量读 + 全市场向量化，只是读取范围是全表（4 列，6.3 s / 440 MB）。

### 两个概念

1. **RPS（Relative Price Strength）**：把全市场按 N 日涨幅排名，取百分位（0~100）。`RPS=90` 表示"涨幅跑赢 90% 的股票"。
2. **突破判定**：现价接近 120 日最高价（在 90% 以内）。

### 条件

| # | 条件 | 公式 |
|---|---|---|
| 1 | 120 日涨幅排名前 10% | `rank(pct=True) × 100 >= 90` |
| 2 | 现价不低于 120 日高点的 90% | `close[t] >= max(high[t-119 .. t]) × 0.90` |

可调参数（类属性，**硬编码不可配**）：

```python
rps_period: int = 120       # 回看窗口
rps_threshold: int = 90     # RPS 阈值
```

### 实现拆解（v2：SQL 聚合，1.5 s / 0.4 MB）

旧实现把整表 355 万行读进 pandas（440 MB）再分组计算。现在改成**一条 SQL 只返回
每只股票的 4 个聚合值**（5,224 行），横截面排名仍在 pandas 里做（保证与旧实现逐位一致）：

```python
# sequoia_x/strategy/rps_breakout.py
_RPS_AGGREGATE_SQL = """
SELECT q.* FROM (
    SELECT s.symbol,
           (SELECT t.close FROM stock_daily t
             WHERE t.symbol = s.symbol ORDER BY t.date DESC LIMIT 1)                AS close_now,
           (SELECT t.close FROM stock_daily t
             WHERE t.symbol = s.symbol ORDER BY t.date DESC LIMIT 1 OFFSET :period) AS close_period,
           (SELECT MAX(t.high) FROM (
                SELECT high FROM stock_daily u
                 WHERE u.symbol = s.symbol ORDER BY u.date DESC LIMIT :period) t)   AS roll_high,
           (SELECT COUNT(*) FROM (
                SELECT 1 FROM stock_daily v
                 WHERE v.symbol = s.symbol LIMIT :period))                          AS bars_in_window
    FROM (SELECT DISTINCT symbol FROM stock_daily) s
) q
WHERE q.close_period IS NOT NULL
  AND q.bars_in_window >= :min_bars
  AND (SELECT MAX(date) FROM stock_daily)
      = (SELECT MAX(z.date) FROM stock_daily z WHERE z.symbol = q.symbol)
"""
```

每一列都是一个**走索引 `idx_symbol_date` 的小范围扫描**（最多 121 行），5,224 只股票
合计只读约 63 万行，而不是 355 万行。

后面两段仍在 pandas 里，语义与旧实现逐位相同：

```python
valid = agg[agg["close_period"].notna()].copy()          # 等价 dropna(pct_change)
valid["pct_change"] = (valid["close_now"] - valid["close_period"]) / valid["close_period"]
valid["rps"] = valid["pct_change"].rank(pct=True) * 100  # 横截面百分位
strong = valid[valid["rps"] >= self.rps_threshold]
selected = strong[strong["close_now"] >= strong["roll_high"] * self._BREAKOUT_RATIO]
```

> ⚠️ **踩坑记录**：最初写成 `WHERE EXISTS (SELECT 1 FROM stock_daily z, latest WHERE ...)`
> 让 SQLite 对外层 5,224 只股票各做一次全表扫描，**卡死数分钟**。改成"两个标量
> `MAX(date)` 子查询相等"后 1.5 s。另外试过 `ROW_NUMBER()` / `LAG()` + 窗口帧的写法，
> 结果正确但要 12.6 s / 20 s（3.55M 行过临时 B 树），反而更慢。

### 注意（这个策略的坑最多）

| 问题 | 说明 |
|---|---|
| **内存** | 旧实现整表 355 万行 × 4 列 一次性载入（440 MB），小内存 VPS 上会 OOM。**已修复**：SQL 聚合后只有 0.4 MB |
| **`latest_date` 不一定是全市场都有数据的日期** | 取的是表里最大的日期。如果只有少数股票更新到那天，RPS 排名就失真（SQL 里用 `MAX(z.date)` 过滤，缺数据的自然不参与） |
| **突破 ≠ 新高** | 条件 `close >= roll_high × 0.90` 只要求"距 120 日高点不超过 10%"，策略名里的 "Breakout" 名不副实 |
| **`min_periods=60`** | 上市不足 120 天但有 60 天数据的票也会算出 `roll_high`，但 `shift(120)` 为空 → 被剔除。所以实际门槛仍是 121 根 K 线；`bars_in_window >= 60` 只是保留旧语义 |
| **除零产生 `inf`** | 若 120 日前收盘为 0（实测库中不存在），`pct_change` 为 `inf`，`rank` 会把 `inf` 排第一 |
| **绕过 `DataEngine` 抽象** | 直接 `sqlite3.connect(self.engine.db_path)` 读库，不走 `get_ohlcv()`。因为需要"每股取最后 121 行"的 SQL 聚合，用 `get_ohlcv` 逐只查反而更慢（5,224 次）。换数据源时这里要单独改 |
| **SQL 方言** | 用到 `LIMIT :param OFFSET :param` 与标量子查询，SQLite 3.25+ 均支持（实测 3.49.1） |

---

## 9. PrivatePlacementStrategy — 定增公告监控

> 文件：`private_placement.py`（73 行）· 路由键：`private_placement` · 无 `_MIN_BARS`

**这个策略和其他 6 个完全不同**：它不碰 K 线、不碰 SQLite，而是**实时爬 akshare** 拉定增公告。本质上是个"公告监控"，只是借用了策略框架。

### 数据源与流程

```python
df = ak.stock_qbzf_em()                       # 东方财富-全部增发
df = df[df["发行方式"] == "定向增发"]           # 排除公开增发
df["发行日期"] = pd.to_datetime(df["发行日期"], errors="coerce")
df = df.dropna(subset=["发行日期"])
df = df[df["发行日期"].dt.date >= today - 7天]  # 最近 7 天
df = df.sort_values("发行日期", ascending=False)
symbols = df["股票代码"].astype(str).str.extract(r"(\d{6})")[0].dropna().tolist()
# 去重（同一只票可能多次定增）
```

| 项 | 值 |
|---|---|
| 回看天数 | `_LOOKBACK_DAYS = 7` |
| 依赖 | `akshare.stock_qbzf_em()`（第三方，**接口可能随时变**） |
| 失败行为 | `except Exception` → 记 error → 返回 `[]`（不影响其他策略） |
| 输出 | 去重后的 6 位代码 |
| 排序 | 按发行日期降序（最新在前） |

### 注意

- 列名 `"发行方式"`、`"发行日期"`、`"股票代码"` 是**中文硬编码**，akshare 一改接口就 `KeyError`。虽然被 try 包住不会崩，但会静默变成"每天都没结果"。
- 正则 `r"(\d{6})"` 只取第一个 6 位数字，对 `"SH600000"` 这类也能正确提取。
- 这是唯一一个**结果不依赖于本地数据库**的策略，所以即使 `--backfill` 没跑，它也能出结果。

---

## 10. 七个策略横向对比

| 策略 | 路由键 | 盯的形态 | 市场状态假设 | 最少 K 线 | 典型命中数 | 额外数据源 |
|---|---|---|---|---|---|---|
| MaVolume | `ma_volume` | 金叉 + 放量 | 趋势启动 | 21 | 几只~几十 | — |
| TurtleTrade | `turtle` | 20 日新高 + 阳线 | 强势突破 | 21 | 几只~几十 | baostock（市值） |
| HighTightFlag | `flag` | 暴涨后窄幅缩量 | 强势整理 | 40 | 0~5 | — |
| LimitUpShakeout | `shakeout` | 涨停后放量洗盘 | 短线接力 | 3 | 几只 | — |
| UptrendLimitDown | `limit_down` | 趋势中放量跌停 | 错杀反弹 | 61 | 0~几只 | — |
| RpsBreakout | `rps` | RPS>90 且近 120 日高点 | 强者恒强 | 121 | 几十~上百 | — |
| PrivatePlacement | `private_placement` | 有定增公告 | 事件驱动 | 0 | 几只~几十 | akshare |

### 三个"事件型" vs 四个"状态型"

- **事件型**（要求形态发生在**今天**）：MaVolume 金叉、LimitUpShakeout 昨涨停、UptrendLimitDown 今跌停。错过一天就永远不选。
- **状态型**（满足条件即可，可能连续多天入选）：HighTightFlag、RpsBreakout、PrivatePlacement、TurtleTrade（新高当天算事件，但可能连涨多天连续入选）。

---

## 11. 共同的设计缺陷（改代码前必读）

| # | 缺陷 | 影响 | 详见 |
|---|---|---|---|
| 1 | ~~涨跌停阈值 1.095 / 0.905 硬编码，未按板块区分~~ | **已修复**（`limits.py`，可用 `--no-board-limits` 回退） | [09](09-code-review-findings.md#p1-2) |
| 2 | 所有阈值（5/20/40/60/120 日、1.5/1.6/1.15/0.6/2.0/0.9/100000000）全是魔法数字 | 调参要改代码，无法回测不同参数 | [09](09-code-review-findings.md) |
| 3 | ~~每个策略独立扫全市场，重复计算 MA20 等指标 7 次~~ | **已修复**（v2 批量快照，见第 2 节） | [09](09-code-review-findings.md#p2-5) |
| 4 | ~~`except Exception` 吞掉单只股票异常~~ | **已消除**（向量化后不再有单只循环） | [09](09-code-review-findings.md#p3-5) |
| 5 | 没有回测 / 没有历史信号记录 | 无法评估策略有效性 | [09](09-code-review-findings.md) |
| 6 | 没有排除 ST / 停牌 / 次新 | 选到 ST 或刚上市 7 天的票 | [09](09-code-review-findings.md) |

---

## 12. 想自己加一个策略？

三步，参见 [10 HOWTO](10-how-to-recipes.md)：

```python
# 1. 新建 sequoia_x/strategy/my_strategy.py
class MyStrategy(BaseStrategy):
    webhook_key = "my_strategy"
    _MIN_BARS = 30

    def run(self) -> list[str]:
        snap = self.engine.get_snapshot()

        ma30 = snap.rolling("close", 30)
        condition = snap.col("close") > ma30

        selected = snap.latest(condition & snap.enough_bars(self._MIN_BARS))
        logger.info(f"MyStrategy 选出 {len(selected)} 只股票")
        return selected
```

```python
# 2. 在 sequoia_x/strategy/registry.py 里登记一行
STRATEGIES[MyStrategy.webhook_key] = MyStrategy

# 3. 可选：.env 里配专属机器人
#    STRATEGY_WEBHOOK_MY_STRATEGY=https://open.feishu.cn/...
```

注册表让 `main.py` 不需要改动，并且立刻支持
`--list-strategies` 与 `--strategy my_strategy`。        logger.info(f"MyStrategy 选出 {len(selected)} 只股票")
        return selected

# 2. main.py 的 strategies 列表加一行
MyStrategy(engine=engine, settings=settings),

# 3. .env 加一行（可选，不加就推到默认机器人）
STRATEGY_WEBHOOK_MY_STRATEGY=https://open.feishu.cn/...
```

下一步：配置与推送机制 → [06 配置与通知](06-config-and-notify.md)
