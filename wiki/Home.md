# Sequoia-X V2 · 项目 Wiki

> A 股量化选股系统（A-Share Quantitative Stock Selection System），基于 baostock + SQLite + pandas。
> 这份 Wiki 面向**有 Java 开发背景、但不熟悉 Python / pandas** 的读者编写。

---

## 一句话说清这个系统

每天收盘后跑一次 `python main.py`：

```
从 baostock 补当天行情 → 存入本地 SQLite → 8 个选股策略各扫一遍全市场 5224 只股票
→ 命中结果的策略各自推一条飞书卡片到对应的机器人
```

没有 Web 服务、没有调度框架、没有消息队列。它就是一个**命令行批处理脚本**（相当于一个 Java 的 `main()` 定时任务）。

### 最常用的三条命令

```bash
python main.py                     # 日常：同步 + 全部策略 + 推送（2~5 分钟）
python main.py --push-now          # 跳过同步，立即选股推送（6~9 秒）
python main.py --dry-run --push-now  # 只看结果不推送
```

完整命令行参数见 [07 §5.0](07-run-and-ops.md)。

---

## 推荐阅读路径

| 你的目的 | 按顺序读 |
|---|---|
| 只想装好跑起来 | [07 运行与运维](07-run-and-ops.md) |
| 不懂 Python / pandas，想先补基础 | [02 Python 速成](02-python-for-java-devs.md) → [03 pandas 手册](03-pandas-field-guide.md) |
| 想搞明白行情数据从哪来、怎么存 | [04 数据层](04-data-layer.md) |
| 想搞懂每个策略在选什么股票 | [05 策略系统](05-strategy-system.md) |
| 想改配置 / 换飞书机器人 | [06 配置与通知](06-config-and-notify.md) |
| 准备加一个新策略 | [10 HOWTO](10-how-to-recipes.md) |
| 准备接手维护、想先看风险点 | [09 代码评注与改进建议](09-code-review-findings.md) |
| 想跑测试 | [08 测试与质量](08-testing-and-quality.md) |

---

## 全部页面

| 页面 | 内容 |
|---|---|
| [01 项目全景](01-overview.md) | 架构图、数据流、目录职责、技术栈对照（Java ↔ Python） |
| [02 Python 速成（Java 视角）](02-python-for-java-devs.md) | 语法/对象模型/模块/异常/打包，全部对照 Java 概念 |
| [03 pandas 实战手册](03-pandas-field-guide.md) | DataFrame 心智模型 + 本项目用到的每一个 pandas API |
| [04 数据层 DataEngine](04-data-layer.md) | 表结构、baostock 对接、三种同步路径、多进程模型、容错 |
| [05 策略系统](05-strategy-system.md) | BaseStrategy 契约 + 通用过滤层 + 8 个策略的设计与阈值 |
| [06 配置与通知](06-config-and-notify.md) | pydantic-settings、环境变量优先级、Webhook 路由、飞书卡片 |
| [07 运行与运维](07-run-and-ops.md) | 安装、回填、日常运行、cron、故障排查表 |
| [08 测试与质量](08-testing-and-quality.md) | pytest + hypothesis 属性测试、ruff、如何跑 |
| [09 代码评注与改进建议](09-code-review-findings.md) | 已发现的 bug / 风险 / 代码异味，分级 + 修复建议 |
| [10 常见任务 HOWTO](10-how-to-recipes.md) | 加策略、加推送渠道、加字段、换数据源的操作步骤 |

---

## 事实速查（实测，非 README 转述）

| 项 | 值 |
|---|---|
| Python 运行时 | 项目要求 `>=3.10`；仓库内 `.venv` 实际为 **3.12.10** |
| 关键依赖版本 | pandas **3.0.1**、numpy 2.4.3、baostock **0.9.1**、akshare 1.18.60、pydantic 2.12.5、pydantic-settings 2.13.1、rich 14.3.3、requests 2.33.0 |
| 数据库文件 | `data/sequoia_v2.db`，约 **481 MB** |
| 行情表规模 | `stock_daily` 共 **3,549,519** 行，覆盖 **5,224** 只股票 |
| 数据时间范围 | `2024-01-02` ~ `2026-11-18`（后复权日 K） |
| 单只股票平均 K 线数 | 约 679 根（最少 7，最多 696） |
| Git 提交历史 | 仅 1 条：`444c0db fix(backfill): 增加重试和自动重连，解决长连接超时问题 (#91)`（历史被 squash 过） |
| 工作区状态 | 干净；`data/*.db`、`.env`、`build/`、`*.egg-info/` 均被 `.gitignore` 忽略 |

> 依赖版本很重要：本仓库跑的是 **pandas 3.0**。pandas 3.0 默认开启 Copy-on-Write、默认字符串 dtype 变成 `str`，网上大量 pandas 2.x 教程里"链式赋值"的写法在这里行为不同。详见 [03](03-pandas-field-guide.md)。

### 2026-11 性能改造（已落地，两轮）

**第一轮：策略层批量向量化**

| | 改造前 | 改造后 |
|---|---:|---:|
| 5 个 K 线策略 | 113.1 s | **4.9 s**（首次快照 4.5 s + 各 0.02~0.2 s） |
| 策略层总耗时 | 125.1 s | 15.3 s（8.2×） |

**第二轮：RPS SQL 化 + 股票名/市值本地缓存**

| | 改造前 | 改造后 |
|---|---:|---:|
| RPS 策略 | 10.5 s / **440 MB** | **1.5 s / 0.4 MB**（SQL 聚合，7.3×） |
| 查股票名（208 只） | **37.7 s**（每只 180 ms） | **3 ms**（一次 30 s 建目录，之后长期命中） |
| 查流通市值（海龟排序用） | ~9 s（每天 50~200 次请求） | **已移除**（改为相对强度排序） |
| `main()` 策略+推送阶段（不含同步） | **~187 s** | **6.9 s**（热跑） |

选股结果在每一轮都做了**逐只比对：6/6 策略完全一致**。

详见 [05 §2](05-strategy-system.md)（策略侧）、[04 §4.4](04-data-layer.md)（数据侧）、
[03 §17](03-pandas-field-guide.md)（`全局 rolling + cumcount 掩码` 手法，比 `groupby.transform` 快 43 倍）。

> ⚠️ 新增的本地表 `stock_info`（代码→名称，同时用于 ST 过滤）。
> 用 `CREATE TABLE IF NOT EXISTS` 自动建，老库无需迁移。

### 本次写文档时实测验证过的事实| 结论 | 验证方式 |
|---|---|
| `query_stock_basic` 的字段顺序是 `code, code_name, ipoDate, outDate, **type, status**` | 实调接口打印 `rs.fields`，代码里两个变量名写反了（[P2-1](09-code-review-findings.md#p2-1)） |
| **baostock 不支持北交所**：`bj.430047` 返回 `10004011 股票代码未识别sh或sz`，`sz.430047` 返回 `err=0` + 0 行 | 实调接口（[P1-3](09-code-review-findings.md#p1-3)） |
| 不加 socket 超时查询未识别代码会**挂死 10 分钟以上** | 实测（[04 §7](04-data-layer.md)）——所以 `main.py:16` 那行超时不能删 |
| baostock 服务端是 `public-api.baostock.com:10030`，裸 socket 非 HTTP | 读 `baostock/common/contants.py` |
| `pd.read_sql` 传裸 sqlite3 连接在 pandas 3.0.1 下**不发** SQLAlchemy 警告 | 实跑 `read_sql` 捕获 warnings |
| `stock_daily` 有 355 万行 / 5224 只 / 481 MB | 直接查 SQLite |

---

## README 与代码的不一致（读 README 时请注意）

| README 说法 | 代码实际情况 |
|---|---|
| 内置 **6** 个策略 | 实际 **7** 个——README 漏了 `PrivatePlacementStrategy`（定增公告） |
| 目录结构里 `strategy/` 只列了 6 个文件 | 实际有 `private_placement.py` |
| "日常模式：8 进程**增量补数据**" | 准确说是"补**缺失的日期区间**"：从每只股票库中的 `MAX(date)+1` 一直补到今天，不是只补当天 |
| "回填约 12 分钟完成 ~5200 只" | 逻辑正确，但**断点续传**（已是最新的会自动跳过），所以重跑不会重复拉取 |

---

## 关于本 Wiki 的组织方式

- 每页都以「**这个文件/模块解决什么问题**」开头，再进入细节，最后给出「**坑与注意**」。
- 所有代码引用都标注了 `文件:行号`，可直接跳转，例如 [turtle_trade.py:81](../sequoia_x/strategy/turtle_trade.py#L81)。
- 涉及 Python / pandas 的语法，首次出现时都会给 Java 类比。

> 💡 如果要把这套文档发布到 **GitHub Wiki**：新建同名页面即可，但需要把文内链接的 `.md` 后缀去掉（GitHub Wiki 的页面 URL 不带扩展名）。首页 `Home.md` 会被自动识别为 Wiki 首页。
