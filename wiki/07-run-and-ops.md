# 07 · 运行与运维

---

## 1. 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | `>= 3.10` | `pyproject.toml` 声明；仓库自带 `.venv` 为 **3.12.10** |
| 网络 | 能访问 `public-api.baostock.com:10030` 与 `open.feishu.cn` | baostock 是**裸 socket** 协议（非 HTTP），代理/防火墙要放行该端口 |
| 磁盘 | ≥ 1 GB | 当前库 481 MB，涨到稳定后约 500~600 MB |
| 内存 | ≥ 1 GB 可用 | RPS 策略整表加载是内存峰值点 |
| 操作系统 | Linux（生产）/ Windows（开发） | 多进程在两者都能跑，细节见第 6 节 |

---

## 2. 安装

### 2.1 用 uv（推荐，README 也推荐）

```bash
# 安装 uv（如果没有）
curl -LsSf https://astral.sh/uv/install.sh | sh      # Linux/macOS
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows

uv sync                   # 按 pyproject.toml + uv.lock 创建 .venv 并装依赖
uv sync --extra dev       # 额外装 pytest / hypothesis / pytest-mock
```

`uv sync` 等价于 `mvn dependency:resolve` + 自动建隔离环境，结果是项目根目录下多一个 `.venv/`（已被 `.gitignore`）。

### 2.2 用 pip

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install .                      # 只装运行时依赖
pip install ".[dev]"               # 装测试依赖
```

> ⚠️ 当前仓库的 `.venv` 里**没有 pip，也没有 pytest**（是 `uv sync` 建的）。直接 `.venv/Scripts/python -m pytest` 会报 `No module named pytest`。要么 `uv sync --extra dev`，要么 `pip install ".[dev]"`。

### 2.3 验证安装

```bash
python -c "import pandas, baostock, akshare, pydantic_settings, rich; print('ok')"
python -c "from sequoia_x.core.config import get_settings; print(get_settings())"
```

第二条没配 `.env` 会抛 `ValidationError`——这恰好说明配置校验在工作。

---

## 3. 配置

```bash
cp .env.example .env
# 编辑 .env，至少填 FEISHU_WEBHOOK_URL
```

完整字段说明见 [06 配置与通知](06-config-and-notify.md)。

**路径注意**：`DB_PATH` 是**相对当前工作目录**解析的（`Engine._init_db` 里直接 `Path(self.db_path)`）。所以必须在项目根目录执行 `python main.py`，否则会在别处凭空创建一个空库。

---

## 4. 首次：回填历史数据

```bash
python main.py --backfill
```

它会：

1. `get_all_symbols()` 从 baostock 拿全市场清单（约 5,224 只，含已退市的过滤）
2. 对每只股票，从 `START_DATE`（默认 2024-01-01）拉到今天
3. 每 200 只自动重连一次，单只失败重试 3 次（2/4/8 秒退避）
4. 每 500 只打一行进度

**预期输出**：

```
Sequoia-X V2 启动
数据库初始化完成：data/sequoia_v2.db
进入回填模式...
获取股票列表完成，共 5224 只
已处理 500/5224，成功 498 跳过 2 失败 0
已处理 1000/5224，成功 996 跳过 4 失败 0
...
回填完成 — 成功: 5224 | 跳过: 0 | 失败: 0
Sequoia-X V2 回填模式运行完成
```

**耗时**：约 10~15 分钟（README 说 12 分钟）。中途可以 Ctrl+C，重跑即续传。

**如果 `失败` 不为 0**：再跑一遍 `python main.py --backfill`，已完成的会被 `skipped`，只补失败的。

---

## 5. 日常运行

```bash
python main.py                 # 同步（有预算）+ 全部策略 + 推送
python main.py --push-now      # ★ 跳过同步，立即选股推送（秒级）
python main.py --dry-run       # 只选股、不推送
```

流程（详见 [01](01-overview.md#3-一次日常运行的完整时序)）：

```
（可选）同步增量 → 8 个策略串行跑 → 每个有结果的策略发一张飞书卡片 → 打印汇总表
```

**预期耗时**：

| 命令 | 耗时 | 说明 |
|---|---:|---|
| `--push-now`（数据已是最新） | **6~9 s** | 快照 4.5 s + 策略 ~1.5 s + 推送 |
| `--push-now --strategy ma_volume` | ~5 s | 只跑一个策略 |
| 默认（一轮同步） | 2~5 min | 同步接近全市场，取决于网络 |
| 首次（`stock_info` 为空） | +30~40 s | 一次性建名称目录 |

> 相比最初版本，策略层从 ~125 s 降到 ~6 s、RPS 从 10.5 s 降到 1.5 s、
> 查股票名从 ~50 s 降到 ~0。**现在瓶颈几乎只剩同步本身的网络往返**，见 [04 §6](04-data-layer.md)。

> ⚠️ **默认模式不只是"补今天"**：它会为每只股票补从 `MAX(date)+1` 到今天的**所有缺口**。
> 停机一周后跑一次会自动补一周（受时间预算限制，可能需要 `--sync-rounds`）。

### 5.0.-1 扩大历史数据集（向前补齐）

默认 `START_DATE=2024-01-01`，所以库里的起点被截断在 2024。想把样本往前扩：

```bash
python main.py --extend-history 2015-01-01 --sync-rounds 8 --sync-timeout 1800
python main.py --extend-history 2015-01-01 --symbols 600000,000001 --dry-run  # 先小规模验证
```

| 特点 | 说明 |
|---|---|
| 只补"起点之前" | 与 `sync_today_bulk`（补最新之后）和 `--backfill`（补缺口）互补 |
| 可中断续传 | 任务由每只股票的 `MIN(date)` 生成，已补到位的自动跳过 |
| 有预算 | 每轮按时长收工（建议 `--sync-timeout 420`，约 7 分钟落一次库），`--sync-rounds` 控制最多跑几轮 |
| 吞吐 | 实测 4 进程约 **2.7 秒/只**（5 年区间、约 1,000~2,000 行/只）→ 全市场约 **3.5 小时**（可用 `--workers 6` 加速） |
| 代价 | 每只股票多约 1,000~2,000 行，全市场约 **+1,000 万行 / +1.5 GB** |

> 补完会让**回测样本**从 700 个交易日扩到约 2,800 个（含 2015 股灾、2018 熊市、2020 疫情），
> 对参数稳健性判断价值很大；但日常跑批不受影响（策略只读最近 130 天）。

### 5.0.0 回测

```bash
python scripts/backtest.py                                    # 全部策略，扣成本
python scripts/backtest.py --horizons 1,5 --top 30 --train-end 2026-01-01
python scripts/backtest.py --strategy turtle,rps --horizons 1,5,10
```
口径：T 日收盘算信号、**T+1 开盘买入**、T+h 收盘卖出、往返成本 0.2%、基准为同日全市场等权。
详见 [05 §7](05-strategy-system.md)。

### 5.0 命令行速查

```
用法: main.py [-h] [--backfill] [--push-now] [--dry-run] [--force]
              [--strategy IDS] [--symbols CODES] [--symbols-file PATH]
              [--no-board-limits] [--sync-timeout SEC] [--workers N]
              [--sync-rounds N] [--list-strategies]
```

| 参数 | 作用 |
|---|---|
| `--push-now`（别名 `--no-sync`） | 跳过数据同步，直接选股+推送 |
| `--dry-run` | 只选股不推送，日志里打印完整代码列表 |
| `--strategy IDS` | 只跑指定策略，逗号分隔；`all` 表示全部 |
| `--symbols CODES` / `--symbols-file PATH` | 自选股模式，只对这些股票选股 |
| `--list-strategies` | 打印策略标识与说明后退出 |
| `--backfill` | 回填历史（不选股、不推送） |
| `--extend-history YYYY-MM-DD` | 把历史**起点往前**补到该日期（可反复运行续传） |
| `--sync-rounds N` | 最多分 N 轮同步，每轮用一次预算（停机多天后补齐用） |
| `--sync-timeout SEC` | 本轮同步的时间预算，默认 `SYNC_MAX_SECONDS`（240） |
| `--workers N` | 同步并发进程数，默认 `SYNC_WORKERS`（4） |
| `--no-board-limits` | 涨跌停退回旧的固定 9.5% 口径 |
| `--force` | 非交易日也执行（默认非交易日直接退出） |

**退出码**：`0` 正常、`1` 未捕获异常、`2` 回填有失败或未完成、`130` Ctrl+C。

### 5.0.1 为什么同步不会再把机器挂住

之前的版本在网络故障时会出现"跑一晚上没结束"：baostock 的 `send_msg` 把所有 socket
异常吞掉只打印 `timed out`，查询**不抛异常**，于是每只股票都要各自等满 10 s socket 超时，
一个进程 650 只 ≈ 1.8 小时。现在：

| 闸门 | 行为 |
|---|---|
| 时间预算 `sync_max_seconds` | worker 每处理一只前检查，到点立即返回已取到的部分 |
| 连续失败阈值 | 连续 5 次失败 → logout + 重新 login（socket 超时后必须重连） |
| 重连仍失败 | 该 worker 立即收工，不再磨剩余股票 |
| 父进程硬超时 | `budget + 20s` 仍未返回就 `pool.terminate()` |
| 登录失败 | 直接返回空，不再为每只股票打一行错误 |

实测：网络故障时 `--sync-timeout 30` 在 **31.8 s** 结束（exit 0），
取回 1,295 条已成功的行情并继续选股，而不是挂死。

### 5.1 什么时候能跑

- **必须在收盘后**（15:00 之后，建议 19:00）。盘中跑会把未完成的分时当成日线存进库。
- 周末/节假日：默认会先查 baostock 交易日历，非交易日直接退出（不白跑、不重复推送）；
  需要强制跑加 `--force`。

### 5.2 cron（Linux）

```cron
# 每个交易日 19:15 跑一次
15 19 * * 1-5 cd /root/Sequoia-X && .venv/bin/python main.py >> log.txt 2>&1
```

| 要点 | 原因 |
|---|---|
| `cd /root/Sequoia-X &&` | 保证 `DB_PATH` 相对路径与 `.env` 能被找到 |
| 用 `.venv/bin/python` | cron 的 PATH 里没有激活的虚拟环境 |
| `>> log.txt 2>&1` | 保留 rich 的彩色日志（写文件后颜色是转义码，可用 `less -R` 看） |
| `1-5` | 周一到周五 |
| 建议加 `flock` 防重入 | 见下 |

```cron
# 带互斥锁，防止上次没跑完又启动一次
15 19 * * 1-5 flock -n /tmp/sequoia.lock sh -c 'cd /root/Sequoia-X && .venv/bin/python main.py >> log.txt 2>&1'
```

`flock -n` 拿不到锁就直接退出——比让两个进程同时写 SQLite 安全得多。

### 5.3 Windows 计划任务

```powershell
# 每天 19:15 运行
$action  = New-ScheduledTaskAction -Execute "E:\Code\Sequoia-X\.venv\Scripts\python.exe" `
           -Argument "main.py" -WorkingDirectory "E:\Code\Sequoia-X"
$trigger = New-ScheduledTaskTrigger -Daily -At "19:15"
Register-ScheduledTask -TaskName "Sequoia-X" -Action $action -Trigger $trigger
```

**`-WorkingDirectory` 不能省**，理由同 cron 的 `cd`。

---

## 6. Windows 特有注意点

| 点 | 说明 |
|---|---|
| 多进程启动方式 | Windows 用 **spawn**（新解释器），Linux 用 **fork**。8 个子进程都会重新 `import pandas`，首次启动慢 5~10 秒 |
| `if __name__ == "__main__"` 守卫 | 必须存在（`main.py:107` 有），否则 spawn 会递归起进程。**你自己写脚本调 `sync_today_bulk` 时也要加** |
| `_bs_fetch_batch` 可 pickle | 它是模块级函数，OK。若改成实例方法，spawn 模式下会 `PicklingError` |
| 路径分隔符 | `DB_PATH` 用正斜杠 `data/sequoia_v2.db` 也能工作，`Path` 会自动处理 |
| 控制台编码 | rich 在 Windows Terminal 下正常；老 cmd.exe 可能出现中文乱码，`chcp 65001` 或换 Windows Terminal |
| 临时文件 | `multiprocessing` 走 `C:\Users\...\AppData\Local\Temp`，如果被安全软件拦会报 `PermissionError` |

---

## 7. 退出码与日志

| 退出码 | 含义 |
|---|---|
| `0` | 正常完成 |
| `1` | 主流程抛了未捕获异常（`main.py:95-102`），日志里有 `主流程发生未捕获异常，程序终止` + 完整堆栈 |

日志格式（rich 渲染）：

```
[2026-11-19 19:15:02] INFO     main - Sequoia-X V2 启动
[2026-11-19 19:15:02] INFO     sequoia_x.data.engine - 数据库初始化完成：data/sequoia_v2.db
[2026-11-19 19:15:03] INFO     sequoia_x.data.engine - 需要更新 5224 只股票，启动多进程并行拉取...
[2026-11-19 19:15:58] INFO     sequoia_x.data.engine - sync_today_bulk: 写入 5100 条数据
[2026-11-19 19:15:58] INFO     main - 执行策略：MaVolumeStrategy
[2026-11-19 19:16:20] INFO     sequoia_x.strategy.ma_volume - MaVolumeStrategy 选出 3 只股票
...
[2026-11-19 19:17:30] INFO     sequoia_x.notify.feishu - 飞书推送成功 [ma_volume]，共 3 只股票
[2026-11-19 19:17:31] INFO     main - Sequoia-X V2 运行完成
```

**日志里的关键锚点**（写监控脚本时抓这些）：

| 关键字 | 含义 | 告警条件 |
|---|---|---|
| `主流程发生未捕获异常` | 进程会 exit 1 | 立即告警 |
| `写入 0 条数据` / `无新数据` | 非交易日属正常 | 连续交易日出现 → 数据源问题 |
| `计算失败` | 单只股票策略异常 | 多次出现 → 数据脏 |
| `飞书推送失败` | webhook 配置或网络问题 | 立即告警 |
| `baostock 登录失败` | 网络或被限流 | 立即告警 |
| `回填完成 — 成功: X 失败: Y` | Y > 0 | 重跑回填 |

日志级别固定 `DEBUG`（`logger.py:36`），但代码里没有 `logger.debug()` 调用，所以实际只输出 INFO 及以上。

---

## 8. 数据运维

### 8.1 每日健康检查

```bash
sqlite3 data/sequoia_v2.db "SELECT date, COUNT(*) FROM stock_daily GROUP BY date ORDER BY date DESC LIMIT 5;"
```

正常情况下最后一行应为今天（或最近交易日），且数量在 4,800~5,200 之间。**如果某天只有几百条，说明那天的同步被中断了**，需要重跑。

### 8.2 补某一天的缺失

没有专门的命令，直接再跑一次 `python main.py`。因为 `sync_today_bulk` 会按 `MAX(date)+1` 补缺口——注意它**不会**补"中间缺一天"的情况（`MAX(date)` 已经是今天了，就认为是最新的）。

要补中间的单日缺口，手工插一段：

```python
from sequoia_x.core.config import get_settings
from sequoia_x.data.engine import DataEngine
engine = DataEngine(get_settings())
engine.backfill(["600000", "000001"])     # 只补指定股票（它按 MAX(date)+1 拉）
```

### 8.3 备份

```bash
# 热备份（SQLite 自带，比 cp 安全）
sqlite3 data/sequoia_v2.db ".backup 'data/backup_$(date +%F).db'"

# 压缩后 480MB → 约 120MB
gzip -c data/sequoia_v2.db > sequoia_$(date +%F).db.gz
```

库丢了也不致命——`python main.py --backfill` 十几分钟就能重建。

### 8.4 重建（怀疑数据被污染时）

```bash
rm data/sequoia_v2.db
python main.py --backfill
```

---

## 9. 故障排查速查表

| 症状 | 最可能原因 | 处理 |
|---|---|---|
| `ValidationError: feishu_webhook_url Field required` | 没 `.env` 或变量名错 | `cp .env.example .env` 并填值 |
| `Extra inputs are not permitted` | `extra="ignore"` 被删 | 恢复 `config.py:16` |
| `本地无股票数据，请先执行 --backfill` | 空库 | 先跑 `--backfill` |
| 策略全是 0 只，日志无报错 | 数据落后；或全市场真的没信号 | `SELECT MAX(date)` 检查 |
| 7 张卡片都发到同一个群 | `load_dotenv()` 被删 | 恢复 `main.py:11` |
| `飞书推送失败 ... code:19021` | webhook token 无效/机器人被踢 | 重新生成 webhook |
| `baostock 登录失败: 网络接收错误` | 网络/防火墙封 10030 端口 | 换网络、重试 |
| 回填极慢，日志反复 `第1次失败` | baostock 限流或长连接超时 | 等；或降低并发（改 `n_workers`） |
| `sqlite3.OperationalError: database is locked` | 两个进程同时写 | 用 `flock` 防重入；或开 WAL |
| `MemoryError` / 进程被杀 | RPS 整表加载 | 加内存；或改 RPS 为增量查询 |
| Windows 下 `PicklingError` / 无限起进程 | 调用没放在 `if __name__` 里 | 加守卫 |
| `python main.py` 报 `ModuleNotFoundError: sequoia_x` | 不在项目根目录 | `cd` 到根目录 |
| 中文日志乱码 | Windows 控制台代码页 | `chcp 65001` |

---

## 10. 从零到跑通的完整命令序列

```bash
# ① 克隆 & 进入
git clone <repo> && cd Sequoia-X

# ② 装依赖
uv sync --extra dev

# ③ 配环境
cp .env.example .env && vi .env        # 填 FEISHU_WEBHOOK_URL

# ④ 自检
python -c "from sequoia_x.core.config import get_settings; print(get_settings())"

# ⑤ 首次回填（10~15 分钟）
python main.py --backfill

# ⑥ 验证数据
sqlite3 data/sequoia_v2.db "SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(date) FROM stock_daily;"

# ⑦ 日常运行
python main.py

# ⑧ 配 cron
crontab -e    # 粘贴第 5.2 节那行
```

下一步：测试与代码质量 → [08 测试与质量](08-testing-and-quality.md)
