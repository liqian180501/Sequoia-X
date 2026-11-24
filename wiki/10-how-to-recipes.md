# 10 · 常见任务 HOWTO

> 每个配方都是"照做就能跑"的步骤。改完记得 `ruff check . --fix && pytest`。

---

## 配方 1 · 新增一个选股策略（最常见的需求）

**以"多日缩量回踩 20 日线"为例。**

### 步骤 1：新建文件

`sequoia_x/strategy/pullback_ma20.py`

```python
"""缩量回踩 20 日线策略：上升趋势中回踩均线且缩量。"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class PullbackMa20Strategy(BaseStrategy):
    """上升趋势 + 缩量回踩 20 日均线。

    选股条件（全市场向量化，一次算完）：
    1. 趋势向上：20 日均线 > 60 日均线
    2. 回踩到位：今日 low <= 20 日均线 <= 今日 high（当天触碰过均线）
    3. 收盘未破：今日 close >= 20 日均线 * 0.98
    4. 缩量：今日 volume < 20 日均量（不含当日）* 0.8

    Attributes:
        webhook_key: 路由到 'pullback' 专属飞书机器人。
    """

    webhook_key: str = "pullback"
    _MIN_BARS: int = 61  # 60 日均线 + 当日

    def run(self) -> list[str]:
        snap = self.engine.get_snapshot()

        ma20 = snap.rolling("close", 20)
        ma60 = snap.rolling("close", 60)
        vol_ma20_prev = snap.rolling_shifted("volume", 1, 20)  # 不含今天

        low, high, close = snap.col("low"), snap.col("high"), snap.col("close")

        up_trend = ma20 > ma60
        touched = (low <= ma20) & (ma20 <= high)
        hold = close >= ma20 * 0.98
        shrink = snap.col("volume") < vol_ma20_prev * 0.8

        selected = snap.latest(
            up_trend & touched & hold & shrink & snap.enough_bars(self._MIN_BARS)
        )
        logger.info(f"PullbackMa20Strategy 选出 {len(selected)} 只股票")
        return selected
```

### 步骤 2：在注册表里登记

`sequoia_x/strategy/registry.py`（**唯一**需要改的登记处，`main.py` 不用动）：

```python
from sequoia_x.strategy.pullback_ma20 import PullbackMa20Strategy

STRATEGIES: dict[str, type[BaseStrategy]] = {
    ...
    PullbackMa20Strategy.webhook_key: PullbackMa20Strategy,   # ← 新增
}
```

登记后即可用命令行单独运行它：

```bash
python main.py --push-now --strategy pullback_ma20
python main.py --list-strategies        # 确认已出现
```

### 步骤 3：配置专属机器人（可选）

```dotenv
STRATEGY_WEBHOOK_PULLBACK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx
```

不配也能跑，会 fallback 到 `FEISHU_WEBHOOK_URL`。

### 步骤 4：加一个确定性测试

`tests/test_pullback_ma20.py`——见 [08 第 7 节](08-testing-and-quality.md#7-怎么补第一个像样的测试示范)的模板：用 mock 引擎 + 合成快照，完全离线。

### 步骤 5：验证

```bash
ruff check . --fix
pytest tests/test_pullback_ma20.py -v
python -c "
from sequoia_x.core.config import get_settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.pullback_ma20 import PullbackMa20Strategy
e = DataEngine(get_settings())
print(PullbackMa20Strategy(e, get_settings()).run()[:10])
"
```

### 检查清单

- [ ] 类继承 `BaseStrategy`
- [ ] 覆盖了 `webhook_key`
- [ ] 声明了 `_MIN_BARS` 且**大于等于**最大窗口 + 1
- [ ] `run()` 返回 `list[str]`，元素是 6 位纯数字代码
- [ ] 通过 `self.engine.get_snapshot()` 取数，**没有**逐只 `get_ohlcv` 循环
- [ ] 条件全部用布尔 Series 一次算完，没有 `iterrows`、没有 `for symbol in ...`
- [ ] 用了 `snap.enough_bars(self._MIN_BARS)` 做数据长度门槛
- [ ] 需要"不含当日"的指标时用 `snap.rolling_shifted(...)`，而不是先 `rolling` 再 `shift`
- [ ] 结尾有 `logger.info(f"... 选出 {n} 只股票")`
- [ ] 窗口需求 ≤ `SNAPSHOT_DAYS`（默认 130）

---

## 配方 2 · 新增一个推送渠道（钉钉 / 企业微信）

### 步骤 1：新建通知类

`sequoia_x/notify/dingtalk.py`

```python
"""钉钉通知模块：将选股结果通过 Webhook 推送至钉钉群。"""

import json

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


class DingTalkNotifier:
    """钉钉 Webhook 推送器（markdown 消息）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _build_payload(self, symbols: list[str], strategy_name: str) -> dict:
        lines = "\n".join(f"- {s}" for s in symbols)
        return {
            "msgtype": "markdown",
            "markdown": {
                "title": f"Sequoia-X | {strategy_name}",
                "text": f"### Sequoia-X 选股播报\n\n"
                        f"**策略：** {strategy_name}\n\n"
                        f"**数量：** {len(symbols)}\n\n{lines}",
            },
        }

    def send(self, symbols: list[str], strategy_name: str, url: str) -> None:
        try:
            resp = requests.post(url, json=self._build_payload(symbols, strategy_name),
                                 timeout=10)
            body = resp.json()
            if resp.status_code != 200 or body.get("errcode") != 0:
                logger.error(f"钉钉推送失败 HTTP={resp.status_code} 响应={resp.text}")
            else:
                logger.info(f"钉钉推送成功，共 {len(symbols)} 只股票")
        except requests.RequestException as exc:
            logger.error(f"钉钉推送异常：{exc}")
        except ValueError:
            logger.error("钉钉返回非 JSON")
```

### 步骤 2：在 `main.py` 里并排调用

```python
from sequoia_x.notify.dingtalk import DingTalkNotifier

notifier = FeishuNotifier(settings)
dingtalk = DingTalkNotifier(settings)
dingtalk_url = os.getenv("DINGTALK_WEBHOOK_URL", "")      # 记得 import os

for strategy in strategies:
    selected = strategy.run()
    if selected:
        notifier.send(selected, type(strategy).__name__, strategy.webhook_key)
        if dingtalk_url:
            dingtalk.send(selected, type(strategy).__name__, dingtalk_url)
```

> 更好的做法：定义一个 `BaseNotifier` 抽象基类，然后 `for n in notifiers: n.send(...)`，和策略层保持一致的风格。

---

## 配方 3 · 给行情表加字段（例如 `is_st`、`tradestatus`）

**这是最容易出错的任务，因为要改 3 处并迁移老库。**

### 步骤 1：改建表语句

`engine.py:14-27`

```sql
CREATE TABLE IF NOT EXISTS stock_daily (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    date        TEXT    NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    turnover    REAL,
    tradestatus INTEGER,      -- 1=正常交易 0=停牌
    is_st       INTEGER,      -- 1=是ST
    UNIQUE (symbol, date)
);
```

### 步骤 2：改两处字段串（**三处必须同步**）

| 位置 | 现状 | 改成 |
|---|---|---|
| `_bs_fetch_batch` engine.py:42 | `"date,open,high,low,close,volume,amount"` | 末尾加 `,tradestatus,isST` |
| `backfill` engine.py:225 | 同上 | 同上 |
| `sync_today_bulk` engine.py:142 | `columns=[... "turnover"]` | 加 `"tradestatus", "is_st"` |
| `backfill` engine.py:275 | `df[[...]]` 选列 | 加两列 |

注意 baostock 字段名是 `isST`（大写 ST），入库时改成小写 `is_st` 以便 SQL 使用。

### 步骤 3：迁移已存在的库（481 MB，不能重建）

```python
# scripts/migrate_add_columns.py
import sqlite3

conn = sqlite3.connect("data/sequoia_v2.db")
cols = {r[1] for r in conn.execute("PRAGMA table_info(stock_daily)")}
for name, ddl in [("tradestatus", "INTEGER"), ("is_st", "INTEGER")]:
    if name not in cols:
        conn.execute(f"ALTER TABLE stock_daily ADD COLUMN {name} {ddl}")
        print(f"added {name}")
conn.commit()
conn.close()
```

历史数据的这两个字段会是 `NULL`。想回填只能重拉历史（或接受"新数据有、老数据没有"）。

### 步骤 4：在策略里用

```python
if last.get("is_st") == 1:
    continue
```

> `last` 是 Series，`last.get("is_st")` 在列不存在时返回 `None`，比 `last["is_st"]` 更安全。

---

## 配方 4 · 换数据源（tushare / 自建服务）

`DataEngine` 对策略层的契约只有 3 项，照着实现即可：

```python
class TushareEngine:
    def __init__(self, settings): self.db_path = ...; self._init_db()

    def get_local_symbols(self) -> list[str]:
        """返回库内所有股票代码（6 位纯数字）。"""
        ...

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        """必须返回含 date/open/high/low/close/volume/turnover 的 DataFrame，
        按 date 升序，dtype 为数值。"""
        ...

    @property
    def db_path(self) -> str:
        """RPS 策略会直接读这个路径（P2-11 的封装破坏）。"""
        ...
```

替换点：`main.py:50` 的 `engine = DataEngine(settings)`。

**必须保持的两条不变量**：

1. `get_ohlcv` 返回的**列名和顺序**与现在一致（`turnover` 不是 `amount`）
2. `get_ohlcv` 返回的行**按 date 升序**（`iloc[-1]` 依赖它）

**一个隐藏坑**：`_to_baostock_code` 被 `turtle_trade.py:42` 通过 `self.engine._to_baostock_code` 调用（私有方法跨类访问）。换引擎时必须保留同名方法，否则海龟策略直接崩。

---

## 配方 5 · 把策略阈值提到配置

### 目标

```dotenv
MA_VOLUME_VOL_SURGE=1.8
TURTLE_MIN_TURNOVER=200000000
LIMIT_UP_PCT=1.098
```

### 步骤 1：给 `Settings` 加字段

```python
class Settings(BaseSettings):
    ...
    ma_volume_vol_surge: float = 1.5
    turtle_min_turnover: float = 100_000_000
    limit_up_pct: float = 1.095
```

### 步骤 2：策略里读配置

```python
# ma_volume.py
def run(self) -> list[str]:
    surge_ratio = self.settings.ma_volume_vol_surge
    ...
    volume_surge = last["volume"] > last["vol_ma20"] * surge_ratio
```

### 步骤 3：更结构化的做法（推荐）

```python
from pydantic import BaseModel

class MaVolumeParams(BaseModel):
    fast: int = 5
    slow: int = 20
    vol_surge: float = 1.5

class StrategyParams(BaseModel):
    ma_volume: MaVolumeParams = MaVolumeParams()
    turtle_min_turnover: float = 100_000_000
```

pydantic-settings 支持嵌套分隔符，但**必须显式配置**（默认 `env_nested_delimiter` 是 `None`）：

```python
model_config = SettingsConfigDict(
    env_file=".env",
    case_sensitive=False,
    extra="ignore",
    env_nested_delimiter="__",      # ← 加上这行，嵌套配置才会生效
)
```

然后 `.env` 里可以写：

```dotenv
STRATEGY_PARAMS__MA_VOLUME__VOL_SURGE=1.8
STRATEGY_PARAMS__MA_VOLUME__FAST=10
```

策略里读：`self.settings.strategy_params.ma_volume.vol_surge`。

---

## 配方 6 · 手工补某只股票 / 某个时间段的数据

现有命令都是"补到最新"，没有"补指定区间"。两种办法：

### 办法 A：直接用 baostock 脚本

```python
# scripts/fetch_one.py
import sqlite3
import pandas as pd
import baostock as bs

symbol, start, end = "600000", "2024-01-01", "2024-06-30"
bs.login()
rs = bs.query_history_k_data_plus(
    f"sh.{symbol}", "date,open,high,low,close,volume,amount",
    start_date=start, end_date=end, frequency="d", adjustflag="1")
rows = []
while rs.next():
    rows.append([symbol] + rs.get_row_data())
bs.logout()

df = pd.DataFrame(rows, columns=["symbol","date","open","high","low","close","volume","turnover"])
for c in ["open","high","low","close","volume","turnover"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df = df.dropna(subset=["close"])

with sqlite3.connect("data/sequoia_v2.db") as conn:
    df.to_sql("stock_daily", conn, if_exists="append", index=False,
              method="multi", chunksize=500)
print(f"写入 {len(df)} 行")
```

已有 `(symbol,date)` 会触发 `IntegrityError`，加 `try/except` 或先 `DELETE WHERE symbol=? AND date BETWEEN ? AND ?`。

### 办法 B：删掉该股的旧数据，让它重新走 backfill

```sql
DELETE FROM stock_daily WHERE symbol = '600000';
```

再 `python main.py --backfill`——它会把这只票从 `START_DATE` 一路补到最新。

---

## 配方 7 · 本地调试：看某只股票为什么没被选中

```python
# scripts/debug_one.py
import pandas as pd
from sequoia_x.core.config import get_settings
from sequoia_x.data.engine import DataEngine

s = get_settings()
e = DataEngine(s)
symbol = "600000"

df = e.get_ohlcv(symbol)
print(f"{symbol} 共 {len(df)} 根 K 线，{df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")

df["ma5"] = df["close"].rolling(5).mean()
df["ma20"] = df["close"].rolling(20).mean()
df["vol_ma20"] = df["volume"].rolling(20).mean()
df["high_20"] = df["high"].shift(1).rolling(20).max()

cols = ["date","open","high","low","close","volume","turnover","ma5","ma20","vol_ma20","high_20"]
print(df[cols].tail(3).to_string())

last, prev = df.iloc[-1], df.iloc[-2]
print("\n--- MaVolume 条件 ---")
print("金叉  :", prev["ma5"] < prev["ma20"], "→", last["ma5"] > last["ma20"])
print("放量  :", last["volume"], ">", last["vol_ma20"] * 1.5,
      "=", last["volume"] > last["vol_ma20"] * 1.5)
print("\n--- Turtle 条件 ---")
print("突破  :", last["close"], ">", last["high_20"], "=", last["close"] > last["high_20"])
print("过亿  :", last["turnover"] > 1e8)
print("阳线  :", last["close"] > last["open"])
print("真涨  :", last["close"] > prev["close"])
```

`df[cols].tail(3).to_string()` 是调试 pandas 的万能手法——`to_string()` 避免宽表被截断显示。

---

## 配方 8 · 加交易日历（避免节假日空跑）

```python
# sequoia_x/data/calendar.py
"""交易日历：避免非交易日空跑全市场同步。"""

from datetime import date

import baostock as bs

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


def is_trading_day(day: date | None = None) -> bool:
    """判断某天是否为 A 股交易日（默认今天）。

    Args:
        day: 待判断的日期，None 表示今天。

    Returns:
        True 表示交易日。

    Raises:
        不抛出异常；查询失败时保守返回 True（宁可空跑，不误跳过）。
    """
    d = (day or date.today()).strftime("%Y-%m-%d")
    lg = bs.login()
    if lg.error_code != "0":
        logger.warning("baostock 登录失败，跳过交易日判断")
        return True
    try:
        rs = bs.query_trade_dates(start_date=d, end_date=d)
        while rs.next():
            if rs.get_row_data()[1] == "0":     # is_trading_day: 1=是 0=否
                return False
        return True
    except Exception as exc:
        logger.warning(f"交易日查询失败：{exc}，按交易日处理")
        return True
    finally:
        bs.logout()
```

在 `main()` 里：

```python
if not args.backfill and not args.force and not is_trading_day():
    logger.info("今天非交易日，跳过")
    return
```

（需要给 `argparse` 加一个 `--force` 参数。）

---

## 配方 9 · 把每天的选股结果落库（强烈建议）

现在跑完只发飞书，**什么都没留下**，导致无法评估策略有效性。加一张表成本极低。

### 步骤 1：加表

```python
# engine.py 里追加
_CREATE_SIGNAL_SQL = """
CREATE TABLE IF NOT EXISTS signal (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    created_at    TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (date, strategy, symbol)
);
"""
```

在 `_init_db` 里一起执行。

### 步骤 2：加写入方法

```python
def save_signals(self, strategy: str, symbols: list[str]) -> None:
    """把某策略当天的选股结果落库（幂等）。"""
    from datetime import date
    if not symbols:
        return
    today = date.today().strftime("%Y-%m-%d")
    with sqlite3.connect(self.db_path) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO signal (date, strategy, symbol) VALUES (?, ?, ?)",
            [(today, strategy, s) for s in symbols],
        )
        conn.commit()
```

### 步骤 3：`main.py` 调用

```python
if selected:
    engine.save_signals(strategy_name, selected)
    notifier.send(...)
```

### 步骤 4：三个月后就能算胜率

```sql
-- 各策略的历史命中数与次日涨幅（需要把后续行情 join 进来）
SELECT strategy, COUNT(*) AS signals, COUNT(DISTINCT date) AS days
FROM signal GROUP BY strategy ORDER BY signals DESC;
```

再写个脚本：对每条 signal，查出 `date` 之后 1/3/5 日的收盘涨幅，就能得到"胜率 / 平均收益 / 最大回撤"——这是从"感觉有用"到"知道有没有用"的分界线。

---

## 配方 10 · 一次性检查整个项目的健康度

```bash
# 代码风格
ruff check . --statistics
ruff format --check .

# 测试
pytest -q

# 数据完整性
sqlite3 data/sequoia_v2.db <<'SQL'
SELECT '总行数', COUNT(*) FROM stock_daily;
SELECT '股票数', COUNT(DISTINCT symbol) FROM stock_daily;
SELECT '日期范围', MIN(date) || ' ~ ' || MAX(date) FROM stock_daily;
SELECT '最新日覆盖', COUNT(*) FROM stock_daily WHERE date = (SELECT MAX(date) FROM stock_daily);
SELECT '落后股票数', COUNT(*) FROM (
  SELECT symbol, MAX(date) d FROM stock_daily GROUP BY symbol
  HAVING d < (SELECT MAX(date) FROM stock_daily)
);
SQL

# 配置
python -c "
from sequoia_x.core.config import get_settings
s = get_settings()
print('db:', s.db_path)
print('start:', s.start_date)
print('默认 webhook:', s.feishu_webhook_url[:45], '...')
print('策略 webhook:', sorted(s.strategy_webhooks))
"
```

期望：`落后股票数` 应该为 0 或个位数；`策略 webhook` 应列出 7 个 key。

---

## 附：目录约定建议

如果打算长期维护，建议加两个目录（目前没有）：

```
scripts/          # 一次性运维脚本（迁移、补数、调试）：不进 main 流程
docs/ 或 wiki/    # 本文档
```

`scripts/` 里的脚本不要 import 到 `sequoia_x` 包里，保持"包内是产品代码、包外是工具"的边界。

---

← 返回 [Home](Home.md)
