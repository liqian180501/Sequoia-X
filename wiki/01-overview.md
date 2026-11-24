# 01 · 项目全景

## 1. 系统定位

Sequoia-X V2 是一个**离线批处理**的 A 股选股系统。它的运行形态非常接近一个 Java 的定时任务：

| 维度 | Sequoia-X | Java 世界的等价物 |
|---|---|---|
| 形态 | `python main.py` 单次执行后退出 | `Spring Boot` 启动 + `@Scheduled`，或 `java -jar app.jar` |
| 进程模型 | 单进程主流程 + 数据同步时临时 fork 8 个子进程 | 主线程 + `ExecutorService` 固定线程池 |
| 存储 | 本地 SQLite 单文件 | 嵌入式 H2 / SQLite JDBC |
| 通信 | 出站 HTTP（飞书 Webhook） | `RestTemplate` / `WebClient` 出站调用 |
| 配置 | `.env` + 环境变量 → Pydantic Settings | `application.yml` → `@ConfigurationProperties` |
| 测试 | pytest + hypothesis（属性测试） | JUnit 5 + jqwik |

它**没有**：Web 接口、数据库连接池、依赖注入容器、异步任务队列、消息中间件。
所有"编排"就是 `main.py` 里一个 `for` 循环。

---

## 2. 架构总览

```
                       ┌──────────────────────────────┐
                       │           main.py            │  ← CLI 入口，argparse 分发
                       │  --backfill ? 回填 : 日常     │
                       └──────┬───────────────┬───────┘
                              │               │
             ┌────────────────▼──┐         ┌──▼──────────────────┐
             │      DataEngine    │         │    FeishuNotifier    │
             │  (sequoia_x/data)  │         │  (sequoia_x/notify)  │
             └───┬──────────┬─────┘         └──▲───────────────────┘
                 │          │                  │
        baostock │          │ SQLite           │ send(symbols, strategy)
      (HTTP API) │          │                  │
             ┌───▼───┐   ┌──▼──────────┐       │
             │ 外部   │   │stock_daily  │       │
             │ 数据源 │   │   .db       │       │
             └───────┘   └──▲──────────┘       │
                            │ get_ohlcv()      │
                            │ get_local_symbols│
                       ┌────┴───────────────────┴────┐
                       │      7 × BaseStrategy        │  ← 策略层（多态扩展点）
                       │ MaVolume / TurtleTrade /     │
                       │ HighTightFlag / LimitUp... / │
                       │ UptrendLimitDown / Rps /     │
                       │ PrivatePlacement             │
                       └──────────────────────────────┘
```

**依赖方向是单向的**：`strategy` → `data` → `core`。`main` 依赖所有层。
`core`（配置 + 日志）不依赖任何其他内部模块。

---

## 3. 一次日常运行的完整时序

`python main.py`（无参数）执行时，[main.py](../main.py) 依次做 7 件事：

```python
# main.py:41-93（简化）
settings = get_settings()                       # 1. 读 .env / 环境变量 → Settings
logger   = get_logger(__name__)                 # 2. rich 日志
engine   = DataEngine(settings)                 # 3. 建库建表（幂等）
count    = engine.sync_today_bulk()             # 4. 8 进程并行补数据 → 3.5M 行的表
strategies = [ MaVolumeStrategy(...), ... ]     # 5. 实例化 9 个策略
notifier = FeishuNotifier(settings)             # 6. 推送器
for s in strategies:                            # 7. 逐个跑，有结果就推送
    selected = s.run()
    if selected:
        notifier.send(selected, type(s).__name__, s.webhook_key)
```

关键观察：

1. **策略是串行执行的**，一个跑完才跑下一个。但读数和算指标都只做一次：第一个策略触发 `engine.get_snapshot()`（约 4.5 s 载入 67 万行），之后 6 个策略共享这份快照与指标缓存。
2. **5 个 K 线策略的耗时已经降到 0.02~0.2 秒级**（v2 改造后，策略层合计 125 s → 15 s，其中 RPS 独占 10.5 s）。现在整次运行的瓶颈是网络：同步 + 市值/名称富化。见 [04 §6](04-data-layer.md)。
3. **推送是逐个策略独立的**，某个策略没选出股票就跳过推送，不会发空卡片（`main.py:86-93`）。
4. **任何异常都会让整个进程 `sys.exit(1)`**（`main.py:95-102`）。所以某一环挂掉时，后面的策略和推送都不会执行。

---

## 4. 目录职责

```
Sequoia-X/
├── main.py                      108 行   CLI 入口 + 编排（唯一的"业务总装线"）
├── pyproject.toml                28 行   依赖声明 + ruff / pytest 配置
├── .env.example                  18 行   环境变量模板
├── uv.lock                     ~254 KB  uv 锁定的完整依赖树
├── data/
│   └── sequoia_v2.db           ~481 MB  运行时生成，不入 git
├── sequoia_x/                            ← 可安装的包（等价于 src/main/java）
│   ├── __init__.py
│   ├── core/
│   │   ├── config.py             92 行   Pydantic Settings：配置模型 + 单例
│   │   └── logger.py             39 行   rich 日志工厂（幂等）
│   ├── data/
│   │   ├── engine.py            409 行   行情仓储：建表 / 增量同步 / 回填 / 全市场快照
│   │   └── snapshot.py          209 行   MarketSnapshot：一次读入 + 分组安全的向量化指标
│   ├── strategy/
│   │   ├── base.py               42 行   抽象基类 BaseStrategy
│   │   ├── ma_volume.py          40 行   MA5 上穿 MA20 + 放量
│   │   ├── turtle_trade.py       84 行   20 日新高 + 成交额过亿 + 阳线过滤 + 市值排序
│   │   ├── high_tight_flag.py    52 行   40 日大涨后 10 日窄幅缩量整理
│   │   ├── limit_up_shakeout.py  48 行   昨日涨停 + 今日放量收阴不破昨收
│   │   ├── uptrend_limit_down.py 42 行   均线多头 + 放量跌停
│   │   ├── rps_breakout.py       62 行   RPS 相对强度前 10% + 接近 120 日高点
│   │   └── private_placement.py  73 行   akshare 拉定增公告（不走 SQLite）
│   └── notify/
│       └── feishu.py            140 行   飞书卡片构造 + Webhook 推送 + 股票名查询
├── tests/                               ← hypothesis 属性测试
│   ├── test_config.py            配置：环境变量覆盖、必填校验
│   ├── test_logger.py            日志：同名返回同一实例、handler 不重复
│   ├── test_data_engine.py       数据库：(symbol,date) 唯一约束
│   ├── test_strategy.py          策略：run() 返回类型
│   ├── test_feishu.py            推送：卡片含全部代码、URL 正确、失败记日志
│   └── test_main.py              入口：异常时非 0 退出
└── build/ + sequoia_x.egg-info/         `pip install .` 留下的构建产物（git 已忽略）
```

---

## 5. 技术栈对照表（Java 开发者速查）

| 本项目用的 | 作用 | Java 类比 |
|---|---|---|
| `pydantic-settings` | 从环境变量/`.env` 加载强类型配置 | `@ConfigurationProperties` + `@Value` + Bean Validation |
| `pydantic` 的 `BaseSettings` | 配置类基类，自动做类型转换和校验 | `@Validated` DTO |
| `rich` | 彩色终端日志 | logback + logstash encoder（彩色 pattern） |
| `logging` | 标准日志门面 | SLF4J |
| `baostock` | 免费行情 SDK（Python 版"行情数据客户端"） | 一个第三方 HTTP SDK jar |
| `pandas` | 表格计算（DataFrame） | 无直接等价物；最接近 `List<Map<String,Object>>` + 列式算子库，或 Spark `Dataset` 的单机版 |
| `sqlite3`（标准库） | 嵌入式数据库驱动 | `java.sql` + SQLite JDBC |
| `requests` | HTTP 客户端 | `RestTemplate` / `OkHttp` |
| `akshare` | 财经数据抓取库（爬东财等） | 一组爬虫 SDK |
| `python-dotenv` | 把 `.env` 注入 `os.environ` | Spring 的 `spring.config.import` / dotenv-java |
| `argparse` | 命令行参数解析 | picocli / `args4j` |
| `abc.ABC` + `@abstractmethod` | 抽象基类 | `abstract class` + `abstract method` |
| `multiprocessing.Pool` | 多进程并行 | `ExecutorService` 固定线程池（但跨的是**进程**，绕过 GIL） |
| `pytest` | 测试框架 | JUnit 5 |
| `hypothesis` | 属性测试（随机生成输入） | jqwik |
| `ruff` | 格式化 + lint | Spotless + Checkstyle/PMD（但更快、二合一） |
| `uv` | 依赖管理 + 虚拟环境 | Maven / Gradle |

> ⚠️ 一个容易误解的点：**`multiprocessing` 用的是进程而不是线程**。Python 有 GIL，线程无法并行跑 CPU 密集代码；而这里的主要瓶颈是网络 IO + 解析，用进程池是为了让 8 个 baostock 客户端各自独立跑，顺便避开 baostock 全局 socket 状态共享的问题。

---

## 6. 三条设计原则（README 声称的，代码确实这么做了）

### 6.1 面向对象 + 多态扩展

新增一个策略 = 新建一个类继承 `BaseStrategy`，实现 `run()`，然后在 `main.py:66-74` 的列表里加一行。**不需要改任何已有策略的代码**——这是典型的策略模式（Strategy Pattern），和 Java 里 `List<Handler> handlers` 的写法一模一样。

### 6.2 向量化计算，严禁 `iterrows`

所有策略内部都用 pandas 的整列运算（`rolling` / `shift` / 布尔索引），而不是逐行 `for` 循环。等价于 Java 里"用 Stream + parallel 代替手写 for 循环"。

对 5000 只股票 × 700 根 K 线来说，`iterrows()` 会慢 100 倍以上（每个元素都要构造一个 Python 对象）。所以策略文件里统一遵循「向量化，禁止 Python 循环」。

### 6.3 增量更新

- 日常模式：按每只股票库中的 `MAX(date)` 续拉，不重复拉已有区间（[engine.py:114-120](../sequoia_x/data/engine.py#L114-L120)）。
- 回填模式：同样跳过已是最新的股票，所以**中断后重跑就是续传**（[engine.py:192-200](../sequoia_x/data/engine.py#L192-L200)）。
- 复权方式选**后复权**（`adjustflag="1"`）：历史价格不变，新数据追加不会让旧数据失效。如果选前复权，每次除权后历史全部要重算——那就没法增量了。这是本系统能"增量存储"的根本原因。

---

## 7. 系统的扩展点（改代码时从这几处入手）

| 想做的事 | 改哪里 |
|---|---|
| 加一个选股策略 | 新建 `sequoia_x/strategy/xxx.py` + 在 `main.py:66` 追加一行（详见 [10](10-how-to-recipes.md)） |
| 换数据源（tushare/自建） | 重写 `DataEngine` 的三个方法：`get_all_symbols` / `sync_today_bulk` / `backfill` |
| 加一个推送渠道（钉钉/企微） | 仿照 `FeishuNotifier`，在 `main.py` 的策略循环里调用 |
| 加配置项 | `core/config.py` 的 `Settings` 加字段 + `.env.example` 加注释 |
| 调整策略阈值（比如涨幅 1.095） | 各策略文件里的字面量，目前**硬编码**，没有抽成配置（见 [09](09-code-review-findings.md)） |
| 增加数据字段（如换手率、市盈率） | 改 `_CREATE_TABLE_SQL`、`_bs_fetch_batch` 的字段串、`backfill` 的字段串，三处要同步 |

---

## 8. 下一步

- 不懂 Python：→ [02 Python 速成（Java 视角）](02-python-for-java-devs.md)
- 不懂 pandas：→ [03 pandas 实战手册](03-pandas-field-guide.md)
- 想看数据怎么落库：→ [04 数据层 DataEngine](04-data-layer.md)
- 想看策略数学逻辑：→ [05 策略系统](05-strategy-system.md)
