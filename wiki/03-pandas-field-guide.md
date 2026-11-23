# 03 · pandas 实战手册（本项目版）

> 只讲 Sequoia-X 里真实出现的 pandas 用法。每个 API 都标注了出处文件与行号，可以边读边点开对照。

---

## 1. 心智模型：先把 DataFrame 想成一张表

```
              stock_daily 表（pd.read_sql 的结果）
        ┌────┬────────┬────────────┬─────────┬─────────┬─────────┐
        │ id │ symbol │    date    │  close  │  volume │ turnover│
        ├────┼────────┼────────────┼─────────┼─────────┼─────────┤
  行 0  │ 1  │ 600000 │ 2024-01-02 │ 78.8686 │ 22066700│ 1.46e8  │
  行 1  │ 2  │ 600000 │ 2024-01-03 │ 79.3465 │ 18203654│ 1.20e8  │
  行 2  │ 3  │ 600000 │ 2024-01-04 │ 79.1075 │ 28885978│ 1.90e8  │
        └────┴────────┴────────────┴─────────┴─────────┴─────────┘
              ↑        ↑            ↑
           行索引      列          列（每一列是一个 Series）
        (RangeIndex 0,1,2...)
```

| pandas | Java 近似物 | 说明 |
|---|---|---|
| `DataFrame` | `List<Map<String,Object>>` + 列式存储 + 算子库 | 二维表，**同列同 dtype**，跨列可混合 |
| `Series` | `List<T>`（带索引标签） | 一维列/行 |
| `df.dtypes` | 无 | 每列一个类型：`int64/float64/object/datetime64[ns]/bool` |
| `df.shape` | `size()` | `(行数, 列数)` 元组 |
| `len(df)` | `size()` | **行数**（不是元素数） |
| `df.index` | 无 | 行标签；`read_sql` 默认 0..n-1 |

### 本项目里 DataFrame 长什么样

`engine.get_ohlcv(symbol)` 返回 `SELECT *`，所以列是：

```
['id', 'symbol', 'date', 'open', 'high', 'low', 'close', 'volume', 'turnover']
   ↑ 自增主键（策略都不用）    ↑ TEXT，'YYYY-MM-DD'   ↑ 全是 float64
```

- `date` 是 **TEXT 字符串**（SQLite 没有日期类型），排序靠字典序恰好等于时间序。只有 RPS 策略需要真正的时间类型，才用 `pd.to_datetime` 转（[rps_breakout.py:27](../sequoia_x/strategy/rps_breakout.py#L27)）。
- 行顺序已经 `ORDER BY date`（[engine.py:83](../sequoia_x/data/engine.py#L83)），所以策略里的 `iloc[-1]` 就是"最新一天"。

---

## 2. 取数据：四种方式

```python
df["close"]              # ① 取一列 → Series（最常用）
df.iloc[-1]              # ② 按"位置"取一行 → Series（index 是列名）
df.iloc[-21:-1]          # ③ 按位置切片 → DataFrame
df[df["volume"] > 0]     # ④ 布尔掩码过滤行
```

对照表：

| 写法 | 取什么 | 返回 | 本项目用例 |
|---|---|---|---|
| `df["col"]` | 一列 | Series | 到处都有 |
| `df[["a","b"]]` | 多列 | DataFrame | `df[["symbol", ...]]`（engine.py:275） |
| `df.iloc[i]` | 第 i **行**（位置） | Series | `df.iloc[-1]` 取今天 |
| `df.iloc[a:b]` | 位置切片（含头不含尾） | DataFrame | `df.iloc[-21:-1]` 取前 20 天 |
| `df.loc[label]` | 按**标签**取 | — | 本项目未用（索引就是 0..n） |
| `df[bool_series]` | 行过滤 | DataFrame | `df[df["volume"] > 0]` |
| `df.tail(n)` | 末尾 n 行 | DataFrame | `df.tail(40)`（high_tight_flag.py:43） |
| `df.head(n)` | 开头 n 行 | DataFrame | 未用 |

### 逐行解剖：`last = df.iloc[-1]`

```python
last = df.iloc[-1]        # turtle_trade.py:83
prev = df.iloc[-2]        # 倒数第二行
```

- `iloc` = **i**nteger **loc**ation，按"第几行"取，**从 0 开始**。
- `-1` 是 Python 负索引，表示最后一行。
- `df.iloc[-1]` 返回一个 **Series**，它的 **index 是列名**，所以能用 `last["close"]`、`last["high_20"]` 取字段。
- ⚠️ 这是**行级标量访问**，性能不高但只取 2 行，无所谓。真正要避免的是 `for idx, row in df.iterrows()`（见第 4 节）。

> 如果列名里有空格/中文，仍可 `last["收盘价"]`。如果列名是合法标识符，`last.close` 也能用，但容易和 `Series.close` 等方法重名，本项目统一用方括号。

---

## 3. 加列 / 改列 / 整列广播

```python
df["ma5"] = df["close"].rolling(5).mean()          # ma_volume.py:41
df["symbol"] = symbol                              # engine.py:273 —— 标量广播到整列
df = df.rename(columns={"amount": "turnover"})     # engine.py:274
```

- `df["新列名"] = Series` → 按**索引对齐**后写入（索引相同就是逐行赋值）。
- `df["新列名"] = 标量` → **所有行都填这个值**（这正是 `backfill` 里给一次查询结果打上股票代码的方式）。
- `df["旧列"] = 表达式` → 覆盖原列。
- 没有 `df.addColumn()`、没有 setter。列就是"属性"，赋值即定义。

> **索引对齐陷阱**：`df["x"] = other_series` 时，pandas 是按 **index 标签**匹配的，不是按位置。如果 `other_series` 的索引是 0..19 而 `df` 的索引是 100..119，你会得到一整列 NaN。本项目的 DataFrame 索引都是干净的 `RangeIndex`，所以没暴露这个问题——但你自己写的时候要注意。

---

## 4. 为什么"严禁 iterrows"（本项目最重要的性能红线）

**慢写法（绝对不要）**

```python
for i, row in df.iterrows():          # 每一行都要新建一个 Series 对象
    if row["close"] > row["open"]: ...
```

**本项目写法（向量化）**

```python
mask = df["close"] > df["open"]        # 一次整列比较，底层是 C/numpy 循环
```

量级差异：5224 只股票 × 700 根 K 线 ≈ **366 万个元素**。`iterrows` 每个元素约几十微秒 → 单次全市场扫描要几十秒到几分钟；向量化是毫秒级。

这就是为什么 7 个策略文件的注释里都明确写着「向量化，严禁 iterrows」。而 `df.iloc[-1]` 这种"取最后一两行"是允许的——它不是遍历。

**类比 Java**：`iterrows` ≈ 把 `double[]` 装箱成 `List<Double>` 再逐元素 `for`；向量化 ≈ 对 `double[]` 直接做 SIMD/循环优化。

---

## 5. 滚动窗口三件套：`shift` / `rolling` / `expanding`

这是全部策略的数学基础。

### 5.1 `shift(n)`：整体下移 n 行（取"n 天前"）

```python
df["close"].shift(1)     # 第 i 行是"昨天的 close"
```

```
原:   [10, 11, 12, 13]
shift(1): [NaN, 10, 11, 12]
```

用途：**避免"用今天的数据判断今天"的自我引用**。例如"突破前 20 日最高价"必须把今天排除掉：

```python
df["high_20"] = df["high"].shift(1).rolling(20).max()    # turtle_trade.py:81
```

### 5.2 `rolling(window)`：滑动窗口函数

```python
df["close"].rolling(5).mean()      # 5 日简单移动均线 MA5
df["volume"].rolling(20).mean()    # 20 日均量
df["high"].rolling(20).max()       # 20 日最高价
df["low"].rolling(20).min()        # 20 日最低价
df["close"].rolling(20).std()      # 20 日标准差（本项目未用）
```

返回的是**新 Series，长度与原序列相同**，但前 `window-1` 个位置是 `NaN`（因为凑不满窗口）。

```
rolling(5).mean() on [1,2,3,4,5,6]:
[NaN, NaN, NaN, NaN, 3.0, 4.0]
```

**这直接导致了策略里的 `if pd.isna(...): continue`**——数据不够长时窗口值就是 NaN。

可用参数：

| 参数 | 作用 | 本项目用例 |
|---|---|---|
| `window` | 窗口大小 | 5 / 20 / 60 / 120 |
| `min_periods` | 最少要几个点才算数（默认 = window） | RPS 用 `min_periods=60` 放宽（rps_breakout.py:44） |

### 5.3 `expanding`：从第一行扩张到当前行（累计）

本项目未使用，但知道它有好处：`df["close"].expanding().max()` = 上市以来最高价。

### 5.4 组合拳：链式调用

pandas 的算子都返回新对象，可以链式串起来（**顺序即语义**）：

```python
df["high"].shift(1).rolling(20).max()
   ①取列    ②下移1行        ③20日窗   ④取最大
```

含义：**今天之前 20 个交易日的最高价**。同一个表达式如果写成 `.rolling(20).max().shift(1)` 就是"20 日窗口最高价，再下移一天"，结果**不一样**（少了今天、多了一天）。这是策略实现里最容易写错的地方。

---

## 6. 布尔掩码（boolean mask）

```python
mask = df["volume"] > df["vol_ma20"] * 1.5      # 得到一个 True/False 的 Series
df[mask]                                        # 只保留 True 的行
df[~mask]                                       # 取反
df[mask1 & mask2]                               # 与（必须用 &，不能用 and）
df[mask1 | mask2]                               # 或
```

⚠️ **三个必须记住的点**：

1. 组合条件用 `&` `|` `~`，**不是** `and` `or` `not`，而且每个条件要加括号：`(a > 1) & (b < 2)`。
2. 含 `NaN` 的比较**返回 False**（不是报错，也不是 True）：
   ```python
   float('nan') > 5      # False
   float('nan') < 5      # False
   ```
3. 本项目的策略其实是"单行判定"，所以看到的是 `if a and b and c:`，其中 `a/b/c` 都是 `numpy.bool_` 标量（从 `last["close"] > ...` 这种单元素比较得来），此时 `and` 是合法的。**但如果你把它们用在整列上，`and` 会抛 `ValueError: The truth value of a Series is ambiguous`**——这就是"整列必须 `&`，单标量可以 `and`"的由来。

---

## 7. NaN 的传播与处理

| 函数 | 作用 | 本项目用例 |
|---|---|---|
| `pd.isna(x)` | 判断缺失（None/NaN/NaT 通用） | `if pd.isna(last["high_20"])`（turtle:86） |
| `df.dropna(subset=["close"])` | 丢掉某列为空的行 | engine.py:145、266 |
| `df.dropna(subset=["pct_change"])` | RPS 里丢掉算不出涨幅的 | rps_breakout.py:36 |
| `df.fillna(0)` | 填空（本项目未用） | — |

`dropna(subset=[...])` 是"只在指定列上判空"，不是整行判空——因为 `id`、`symbol` 这些列不会空，整行判空没意义。

---

## 8. dtype 转换：外部数据必须强制转型

```python
df = pd.DataFrame(rows, columns=["symbol", "date", "open", ...])   # 从 baostock 拿的是**字符串**
for col in ["open", "high", "low", "close", "volume", "turnover"]:
    df[col] = pd.to_numeric(df[col], errors="coerce")               # engine.py:143-144
df = df.dropna(subset=["close"])
df = df[df["volume"] > 0]
```

为什么要转：baostock 的 `rs.get_row_data()` 返回的是**一堆字符串**（`['2024-01-02', '79.227', ...]`）。`pd.DataFrame` 会自动推断 dtype，但遇到空串 `""` 或 `"0.0000"` 就会把整列变成 `object`（字符串），之后 `df["volume"] > 0` 会抛 `TypeError`。

`errors="coerce"` 的含义：转换失败的格 → `NaN`（而不是抛异常）。紧接着的 `dropna` 就是把那些坏行扔掉。这是"宽进严出"的清洗套路。

**类比 Java**：`pd.to_numeric(..., errors="coerce")` ≈ 一个不抛异常的 `Double.parseDouble` 包装器 + 过滤。

---

## 9. 分组聚合：`groupby`（RPS 策略核心）

其他 6 个策略都是"一次只处理一只股票"，所以不需要分组。**只有 RPS 需要一次性处理全市场**，于是用到了 `groupby`。

```python
df = df.sort_values(['symbol', 'date'])                              # ① 必须先排序

df['close_shift'] = df.groupby('symbol')['close'].shift(120)          # ② 按股票分组后各自下移
df['pct_change']  = (df['close'] - df['close_shift']) / df['close_shift']   # ③ 120 日涨幅

roll_high = df.groupby('symbol')['high'].rolling(
    window=120, min_periods=60
).max().reset_index(level=0, drop=True)                              # ④ 分组滚动最高价
df['roll_high'] = roll_high
```

**`groupby(...).shift()` ≈ SQL 的 `PARTITION BY symbol ORDER BY date` 窗口函数**：

```sql
-- 等价 SQL
close_120_ago = LAG(close, 120) OVER (PARTITION BY symbol ORDER BY date)
roll_high     = MAX(high) OVER (PARTITION BY symbol ORDER BY date
                                ROWS BETWEEN 119 PRECEDING AND CURRENT ROW)
```

如果你熟悉 SQL 窗口函数，这里一秒就懂了；如果熟悉 Java，就是"先把数据按 key 分桶，再在每个桶里独立做窗口计算，最后拼回去"。

### 9.1 那个奇怪的 `reset_index(level=0, drop=True)` 是什么

`groupby().rolling()` 返回的 Series 有 **MultiIndex**（两级索引：`(symbol, 原行号)`）：

```
symbol  date_idx
000001  0          10.5
        1          10.8
600000  0          78.9      ← 索引变成两层了
```

而 `df['roll_high'] = roll_high` 要求两边索引**能对齐**（`df` 的索引只有一层）。`reset_index(level=0, drop=True)` 的意思是：

- `level=0` → 丢掉第一层索引（`symbol`）
- `drop=True` → 直接丢弃，不要变成普通列
- 结果：索引恢复成原来的单层 `RangeIndex`，值顺序不变

**等价 Java 思路**：`flatMap` 把分组结果按原顺序摊平回来。

> 这是 pandas 里最容易照抄错的写法。记住模板：
> `df.groupby(k)[col].rolling(n).max().reset_index(level=0, drop=True)`
> 四个部分缺一不可。

---

## 10. 排名：`rank(pct=True)`

```python
latest_df['rps'] = latest_df['pct_change'].rank(pct=True) * 100      # rps_breakout.py:39
strong_stocks = latest_df[latest_df['rps'] >= 90]                     # 取前 10%
```

- `rank()` 默认从 1 开始排名；`pct=True` 返回**百分位**（0~1），×100 变成 0~100 的"RPS 分数"。
- `RPS = 90` 意味着"这只股票的 120 日涨幅排在市场前 10%"。
- 这就是欧奈尔 CANSLIM 里的 RS Rating。

对照 SQL：`PERCENT_RANK() OVER (ORDER BY pct_change)`。

---

## 11. 表连接：`merge`

```python
latest_roll_high = df[df['date'] == latest_date][['symbol', 'roll_high']]
strong_stocks = strong_stocks.merge(latest_roll_high, on='symbol')    # rps_breakout.py:49
```

- `merge(on='symbol')` = SQL 的 `INNER JOIN ON symbol`。
- 默认 `how='inner'`；其他取值 `left` / `right` / `outer`。
- 冲突列名会自动加后缀 `_x` / `_y`（本项目两边列不重叠，没触发）。

**为什么这里要 merge 而不是直接读？** 因为 `roll_high` 是在**加了 MultiIndex 处理之后**才写回 `df['roll_high']` 的，而 `strong_stocks` 是从 `latest_df`（早于这个赋值）过滤出来的副本，没有这一列。merge 是把两边重新对齐的最直白写法。

---

## 12. 时间与字符串访问器：`.dt` / `.str`

```python
df["发行日期"] = pd.to_datetime(df["发行日期"], errors="coerce")   # 转 datetime64
df = df[df["发行日期"].dt.date >= cutoff]                          # 取日期部分比较

symbols = df["股票代码"].astype(str).str.extract(r"(\d{6})")[0].dropna().tolist()
```

| 写法 | 作用 | 类比 |
|---|---|---|
| `pd.to_datetime(col, errors="coerce")` | 字符串 → 时间戳，坏值变 NaT | `LocalDate.parse` + 容错 |
| `.dt.date` | 取日期部分（Python `date` 对象） | `.toLocalDate()` |
| `.dt.year` / `.dt.month` / `.dt.dayofweek` | 取年月日/星期 | `.getYear()` |
| `.astype(str)` | 整列转字符串 | `String.valueOf` |
| `.str.extract(r"(\d{6})")` | 正则捕获第一个分组，返回 DataFrame | `Pattern.matcher().group(1)` |
| `.str.contains("x")` | 子串判断 | `String.contains` |
| `[0]` | 取 extract 结果的第 0 列 | — |

意义：`.dt` 和 `.str` 是 pandas 的**访问器（accessor）**，让"只对 datetime / 只对字符串"的方法挂到 Series 上。类比 Java 里对 `List<LocalDate>` 做批量操作必须先 `map`——pandas 省掉了这层。

---

## 13. 读写数据库

```python
# 读
df = pd.read_sql("SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                 conn, params=(symbol,))                          # engine.py:82-86

# 写
df.to_sql("stock_daily", conn, if_exists="append", index=False,
          method="multi", chunksize=500)                          # engine.py:152
```

| 参数 | 含义 |
|---|---|
| `params=(symbol,)` | 参数化查询，防注入（注意是**元组**，一个参数的元组要带逗号） |
| `if_exists="append"` | 追加；还有 `"replace"`（删表重建）、`"fail"` |
| `index=False` | 不要把 pandas 的行索引当成一列写进去（**必写**，否则表会多一列 `index`） |
| `method="multi"` | 拼多值 INSERT，比默认逐行快一个数量级 |
| `chunksize=500` | 每 500 行一批 |

**pandas 3.0 实测**（本仓库 3.0.1 + 裸 `sqlite3.Connection`）：

```
pandas 3.0.1
rows: 696
warnings: []                                       ← 没有 SQLAlchemy 警告
dtypes: {'symbol': 'str', 'date': 'str', 'close': 'float64'}
```

两点值得注意：

1. **没有** "pandas only supports SQLAlchemy connectable" 警告（旧版 2.x 会发）。所以本项目不需要引入 SQLAlchemy。
2. `date` 读出来是 **`str`** 而不是 `datetime64` —— SQLite 把日期存成 TEXT，pandas 3.0 的默认字符串 dtype 就是 `str`。所以 RPS 里必须手动 `pd.to_datetime`。

---

## 14. Copy-on-Write 与"链式赋值"（pandas 3.0 行为变化）

本项目跑的是 **pandas 3.0**，默认开启 CoW。表现为：

```python
latest_df = df[df['date'] == latest_date].copy()      # rps_breakout.py:35 —— 显式 copy，安全
latest_df['rps'] = ...                                 # 改副本，绝不污染原 df
```

显式 `.copy()` 是好习惯，RPS 里做对了。而下面这种写法在 3.0 会警告或静默不生效：

```python
df[df['date'] == d]['rps'] = 1     # ❌ 链式赋值：改的是临时副本，原 df 不变
```

**结论**：想改哪一张表，就先把它 `.copy()` 出来再改，别在筛选结果上直接赋值。

---

## 15. API 速查表（按本仓库出处索引）

| API | 作用 | 出处 |
|---|---|---|
| `pd.DataFrame(rows, columns=[...])` | 由二维列表建表 | engine.py:142 |
| `pd.read_sql(sql, conn, params=...)` | 查库 | engine.py:82、rps_breakout.py:19 |
| `df.to_sql(..., if_exists="append", method="multi")` | 写库 | engine.py:152、279 |
| `df[col]` / `df[[c1,c2]]` | 选列 | 各处 |
| `df.iloc[-1]` / `[-2]` / `[-3]` | 取倒数第 n 行 | 5 个策略 |
| `df.tail(n)` | 取末尾 n 行 | high_tight_flag.py:43-44 |
| `df.iloc[-21:-1].mean()` | 取区间均值（前 20 天，不含今天） | high_tight_flag.py:61 |
| `df.shift(n)` | 下移 n 行 | turtle:81、rps:31 |
| `df.rolling(w).mean()/.max()/.min()` | 移动均线/极值 | 5 个策略 |
| `pd.isna(x)` | 判缺失 | turtle:86、uptrend:50 |
| `pd.to_numeric(s, errors="coerce")` | 强制数值化 | engine.py:144、265 |
| `df.dropna(subset=[...])` | 按列剔空 | engine.py:145、rps:36 |
| `df[bool_mask]` | 行过滤 | 各处 |
| `df.groupby(k)[col].shift(n)` | 分组移位 | rps:31 |
| `df.groupby(k)[col].rolling(...)` | 分组滚动 | rps:43 |
| `.reset_index(level=0, drop=True)` | 抹平 MultiIndex | rps:45 |
| `s.rank(pct=True)` | 百分位排名 | rps:39 |
| `df.merge(other, on='symbol')` | 连接 | rps:49 |
| `df.sort_values(cols)` | 排序 | rps:28、private_placement:59 |
| `pd.to_datetime(s, errors="coerce")` | 转时间 | rps:27、private_placement:50 |
| `s.dt.date` | 取日期 | private_placement:52 |
| `s.astype(str).str.extract(r"(\d{6})")` | 正则提取 | private_placement:62 |
| `df[col].max()/.min()` | 列极值 | high_tight_flag:46-49 |
| `df["date"].unique().tolist()` | 去重成列表 | engine.py:150 |
| `df.empty` | 是否空表 | engine.py:269、rps:24 |
| `df.rename(columns={...})` | 改列名 | engine.py:274 |

---

## 16. 逐行解剖：读懂这三行，就读懂了全部策略

### ① 海龟突破的核心一行 — [turtle_trade.py:81](../sequoia_x/strategy/turtle_trade.py#L81)

```python
df["high_20"] = df["high"].shift(1).rolling(20).max()
```

分解：

| 步骤 | 表达式 | 结果（假设第 i 行） |
|---|---|---|
| 取列 | `df["high"]` | `high[i]` |
| 下移 1 行 | `.shift(1)` | `high[i-1]` |
| 20 日窗 | `.rolling(20)` | `{high[i-20] ... high[i-1]}` |
| 取最大 | `.max()` | `max(high[i-20..i-1])` |

于是 `last["close"] > last["high_20"]` 就是"**今天的收盘价 > 前 20 个交易日的最高价**"（严格大于，不含今天）。

### ② 高旗形的基准量 — [high_tight_flag.py:61-62](../sequoia_x/strategy/high_tight_flag.py#L61-L62)

```python
vol_ma20 = df["volume"].iloc[-21:-1].mean()
shrink = df["volume"].iloc[-1] < vol_ma20 * 0.6
```

- `iloc[-21:-1]` = 从倒数第 21 个到倒数第 2 个（**含头不含尾**）→ 正好 20 个元素，**不含今天**。
- 用"前 20 日均量"而不是"含今天的 20 日均量"，是为了避免今天的缩量把自己拉低，从而误判。

### ③ RPS 的 120 日涨幅 — [rps_breakout.py:31-32](../sequoia_x/strategy/rps_breakout.py#L31-L32)

```python
df['close_shift'] = df.groupby('symbol')['close'].shift(self.rps_period)
df['pct_change']  = (df['close'] - df['close_shift']) / df['close_shift']
```

= 每只股票自己的 `(今收 - 120 日前收) / 120 日前收`，即**区间收益率**。除以 0 或 NaN 会得到 inf/NaN，后续 `dropna` 处理掉（注意：除零得 `inf`，`dropna` **不会**去掉 `inf`，见 [09](09-code-review-findings.md) 的注记）。

---

<a id="pandas-rolling-trick"></a>
## 17. 性能技巧：全局 rolling + cumcount 掩码

> 这是本项目 v2 性能改造（[05](05-strategy-system.md) 第 2 节）的核心手法，实测比 `groupby.transform` **快 43 倍**。

**问题**：数据是"多只股票首尾相接"的一张长表，要对每只股票**各自**算 20 日滚动均线。

**慢写法（语义最直观）**

```python
df.groupby("symbol")["close"].transform(lambda s: s.rolling(20).mean())   # 12.63 s
```

慢的原因：`transform` 要为 5,224 个分组各调用一次 Python lambda，每次都要新建一个 rolling 对象。

**快写法**

```python
cnt = df.groupby("symbol").cumcount()        # 该行在组内的序号 0,1,2,...（一次分组）
s = df["close"].rolling(20).mean()           # 整列一次 C 层滑动窗口
s[cnt < 19] = np.nan                         # 把"窗口跨越了上一只股票"的行抹掉
                                             # 0.29 s
```

**为什么这样是对的**：数据已按 `symbol, date` 排序，同一只股票的行是**连续**的。
第 i 行的 20 行窗口要么完全落在本组内，要么向前跨到了上一只股票——后者恰好等价于
"该行的组内序号 < 19"。所以一次布尔掩码就能清掉全部跨组污染。

**边界对照**：

| 运算 | 需要抹掉的行 | 理由 |
|---|---|---|
| `rolling(w)` | `cumcount < w - 1` | 窗口向前跨组 |
| `shift(n)` | `cumcount < n` | 直接取到了上一组 |
| `shift(n).rolling(w)` | `cumcount < n + w - 1` | 两者叠加 |

这三条正是 `MarketSnapshot.rolling` / `.shift` / `.rolling_shifted` 的实现，
并且有 hypothesis 属性测试逐元素对比 `groupby.transform`（`tests/test_snapshot.py`）。

**代价**：必须先保证"组是连续的"（即 `ORDER BY symbol, date`）。一旦顺序被打乱，
这个技巧就会静默算错——所以 `MarketSnapshot.__init__` 的文档里把"必须有序"写成了硬约束。

**Java 类比**：相当于不按 key 分组建 Map，而是利用"数据已按 key 排好序"做一次线性扫描，
再用下标边界把跨组的部分置空。复杂度从 O(n × 分组开销) 降到 O(n)。

---

## 18. 常见报错速查

| 报错 | 原因 | 解法 |
|---|---|---|
| `KeyError: 'close'` | 列名不对（本项目 `turnover` 曾叫 `amount`） | 打印 `df.columns` |
| `TypeError: '>' not supported between 'str' and 'int'` | 列还是字符串 | `pd.to_numeric(...)` |
| `ValueError: The truth value of a Series is ambiguous` | 对整列用了 `and`/`or`/`if df:` | 改用 `&` `|`、`.all()`、`.empty` |
| `SettingWithCopyWarning` / 改了不生效 | 链式赋值 | `x = y[mask].copy()` 再改 |
| `KeyError: 0` after `reset_index` | MultiIndex 没抹平 | 加 `reset_index(level=0, drop=True)` |
| 整列变成 `object` | 混了 `""`/`None`/数值 | `to_numeric(errors="coerce")` + `dropna` |
| 全 NaN | 索引没对齐 | 检查两边的 `index` |

---

## 19. 想深入的话

- 官方 10 分钟入门：<https://pandas.pydata.org/docs/user_guide/10min.html>
- 窗口函数章节（本项目所有策略的数学基础）：<https://pandas.pydata.org/docs/user_guide/window.html>
- groupby 章节：<https://pandas.pydata.org/docs/user_guide/groupby.html>
- pandas 3.0 迁移要点（CoW、字符串 dtype）：<https://pandas.pydata.org/docs/whatsnew/v3.0.0.html>

下一步：把 pandas 用到真实数据上 → [04 数据层 DataEngine](04-data-layer.md) 与 [05 策略系统](05-strategy-system.md)。
