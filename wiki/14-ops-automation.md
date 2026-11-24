# 14 · 定时任务与告警

> 入口：[`scripts/run_daily.py`](../scripts/run_daily.py)、[`scripts/healthcheck.py`](../scripts/healthcheck.py)、
> [`scripts/install_task.ps1`](../scripts/install_task.ps1)

---

## 1. 为什么不能直接把 `main.py` 交给定时任务

`main.py` 只做"选股 + 推送"。而一个能长期无人值守的任务需要三件它不管的事：

| 需要 | 不管的后果 |
|---|---|
| **先确认数据能用** | 数据源挂了 → 推送的是过期数据，比不推更糟 |
| **失败要有人知道** | 定时任务静默失败是最危险的失败 |
| **退出码要有意义** | 任务计划程序靠退出码判断成败 |

所以定时任务指向 `run_daily.py`，它做：**健康检查 → 选股推送 → 失败告警**。

---

## 2. 注册定时任务（Windows）

```powershell
.\scripts\install_task.ps1                       # 工作日 15:30
.\scripts\install_task.ps1 -Time 16:10           # 换个时间
.\scripts\install_task.ps1 -Unregister           # 删除
```

做过的处理：

* 自动找 `.venv\Scripts\python.exe`（找不到才退回 `python`）；
* **stdout/stderr 重定向到 `logs\daily.log`**——任务计划程序不会把输出给你，不重定向等于没有日志；
* 强制 `PYTHONIOENCODING=utf-8`，否则中文日志在任务里是乱码；
* 只排周一到周五。

> 15:30 只是"数据源通常已更新"的时刻，**法定节假日也会触发**。所以靠健康检查兜底：
> 数据不新鲜时中止并告警，而不是推送旧数据。

---

## 3. 健康检查

```bash
python scripts/healthcheck.py            # 退出码 0/1
python scripts/healthcheck.py --alert    # 不健康时推告警
python scripts/healthcheck.py --json
```

检查项：

| 项 | 判据 | 为什么 |
|---|---|---|
| 库非空 | `stock_daily` 有行 | 空库等于全链路没数据 |
| **新鲜度** | 最新日期落后 ≤ `MAX_DATA_LAG_DAYS`（默认 1 天） | 容忍周末，不容忍停更 |
| **覆盖度** | 最新交易日 ≥ 90% 的股票有 K 线 | 覆盖率骤降说明同步中途失败 |
| 重复行 | `(symbol, date)` 唯一 | 主键失效会让回测/统计翻倍 |
| 留痕 | `pick_history` 最新日期 | 顺带看一眼跑批有没有执行 |

实测（2026-11-24）：

```
行数 4,018,452 · 股票 5,224 · 最新 2026-11-23（落后 1 天）· 覆盖 99.7% · 重复 0
✅ 数据健康
```

关键技术点：`expected_trade_date()` 只跳过周末、不处理法定节假日，所以阈值默认给 1 天而不是 0——
**宁可偶尔漏报，也不要每周一都误报**。

---

## 4. 告警

告警走**独立的** `ALERT_WEBHOOK_URL`，不复用选股群：

```bash
ALERT_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
MAX_DATA_LAG_DAYS=1
```

理由：选股推送是"给群友看股票"，告警是"给自己看故障"。混在一条 webhook 里，
故障会被淹没在卡片流里。

三条硬性设计：

1. **未配置 → 只写日志**：不允许因为"告警发不出去"把主流程搞挂；
2. **发送失败 → 吞掉异常**：告警本身不能引发新的失败（有单测覆盖不可达地址）；
3. **分级**：`error`（红）/ `warning`（橙）/ `info`（蓝），卡片颜色区分。

触发的三种情况：

| 场景 | 级别 | 消息 |
|---|---|---|
| 健康检查不通过 | error | 列出具体问题 + "本次未推送选股结果" |
| 跑批抛异常 | error | 异常类型 + 消息 |
| 退出码 1 / 2 | error / warning | 失败或部分成功 |

---

## 5. 一次完整的跑批链路

```
15:30 任务计划程序触发
  └─ run_daily.py
      ├─ healthcheck        → 不健康：告警 + 退出 1（不推旧数据）
      └─ main.py --push-now
          ├─ sync_today_bulk（增量同步，带预算）
          ├─ 8 个策略（快照共享，约 30 s）
          ├─ 留痕到 pick_history（幂等）
          └─ 按策略路由推送飞书
```

---

## 6. 手动执行

```bash
python scripts/run_daily.py                 # 完整链路
python scripts/run_daily.py --dry-run       # 不推送
python scripts/run_daily.py --no-sync       # 不联网，用本地数据
python scripts/run_daily.py --skip-health   # 跳过健康检查（调试用）
```

---

## 7. 局限

1. **只支持 Windows 任务计划程序**；Linux 用 `cron`（`30 15 * * 1-5 cd ... && .venv/bin/python scripts/run_daily.py`）；
2. **不处理法定节假日**：靠"数据不新鲜就中止 + 告警"兜底，节假日会安静退出；
3. **告警没有重试**：webhook 偶发失败就丢了（日志里留痕）；
4. **没有跑批历史表**：目前靠日志，想统计"最近 30 天跑批成功率"需要再加 `run_log`；
5. 任务以当前用户身份运行，**要求该用户登录过**才能触发（除非配置为"不管用户是否登录"）。
