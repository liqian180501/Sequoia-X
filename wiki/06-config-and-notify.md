# 06 · 配置与通知

> 涉及文件：`core/config.py`(92 行)、`notify/feishu.py`(140 行)、`.env.example`、`main.py:10-11`

---

## 1. 配置模型：Settings

```python
class Settings(BaseSettings):                    # core/config.py:6
    db_path: str = "data/sequoia_v2.db"          # 有默认值 → 可选
    start_date: str = "2024-01-01"               # 有默认值 → 可选
    feishu_webhook_url: str                      # 无默认值 → ★ 必填
    strategy_webhooks: dict[str, str] = {}       # 由 STRATEGY_WEBHOOK_* 前缀变量填充

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,                    # DB_PATH / db_path 都能命中
        extra="ignore",                          # .env 里多余的键不报错
    )
```

| 字段 | 环境变量名 | 默认值 | 必填 |
|---|---|---|---|
| `db_path` | `DB_PATH` | `data/sequoia_v2.db` | 否 |
| `start_date` | `START_DATE` | `2024-01-01` | 否 |
| `feishu_webhook_url` | `FEISHU_WEBHOOK_URL` | 无 | **是** |
| `snapshot_days` | `SNAPSHOT_DAYS` | `130`（最小 62） | 否 |
| `sync_workers` | `SYNC_WORKERS` | `4` | 否 |
| `sync_max_seconds` | `SYNC_MAX_SECONDS` | `240` | 否 |
| `board_aware_limits` | `BOARD_AWARE_LIMITS` | `true` | 否 |
| `min_turnover` | `MIN_TURNOVER` | `50000000`（5000 万） | 否 |
| `min_price` | `MIN_PRICE` | `2.0` | 否 |
| `min_history_bars` | `MIN_HISTORY_BARS` | `120` | 否 |
| `max_picks` | `MAX_PICKS` | `30`（0=不限） | 否 |
| `exclude_st` | `EXCLUDE_ST` | `true` | 否 |
| `strategy_webhooks` | `STRATEGY_WEBHOOK_<KEY>`（动态） | `{}` | 否 |

**Java 对照**：`Settings` ≈ 一个 `@ConfigurationProperties` 类 + `@NotBlank` 校验；`BaseSettings` 在实例化时读环境变量，读不到且无默认值就抛 `pydantic_core.ValidationError`。

### 1.1 为什么必须 `extra="ignore"`

`.env` 里有 `STRATEGY_WEBHOOK_MA_VOLUME` 这 7 个键，它们**不是** `Settings` 声明的字段。pydantic 默认 `extra="forbid"` 会直接报 `ValidationError: Extra inputs are not permitted`。代码里那行注释写得很直白：

```python
extra="ignore",  # <--- 加上这一行！让 Pydantic 放行未定义的变量
```

### 1.2 配置优先级

```
进程环境变量（含 load_dotenv() 注入的）  >  .env 文件  >  字段默认值
```

`case_sensitive=False` 意味着 `DB_PATH`、`db_path`、`Db_Path` 等价。

---

## 2. 单例：`get_settings()`

```python
_settings: Settings | None = None                # config.py:74

def get_settings() -> Settings:                   # config.py:77
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
```

- 第一次调用才读环境变量、才做校验（延迟初始化），之后复用。
- `global` 关键字：Python 里要给模块级变量重新赋值必须声明 `global`，否则会创建一个同名局部变量。
- **测试里怎么替换**：`monkeypatch.setattr(cfg_module, "_settings", None)`（见 `tests/test_config.py`），因为直接改 `os.environ` 对已缓存的单例无效。

> ⚠️ 这个单例是**模块级全局变量**，等价于 Java 里的 `static Settings INSTANCE`。多进程场景下每个子进程有独立的 `_settings`（spawn 模式重新 import 一遍），但没有代码依赖这一点。

---

## 3. 策略专属 Webhook 的合并机制（本模块最绕的地方）

### 3.1 想要的效果

`.env` 里：

```dotenv
FEISHU_WEBHOOK_URL=https://open.feishu.cn/.../default-token
STRATEGY_WEBHOOK_MA_VOLUME=https://open.feishu.cn/.../ma-token
STRATEGY_WEBHOOK_TURTLE=https://open.feishu.cn/.../turtle-token
```

期望 `Settings.strategy_webhooks == {"ma_volume": "...ma-token", "turtle": "...turtle-token"}`，这样 `MaVolumeStrategy.webhook_key = "ma_volume"` 就能路由到自己的机器人。

### 3.2 实际实现

```python
def model_post_init(self, __context: object) -> None:            # config.py:45
    import os
    prefix = "STRATEGY_WEBHOOK_"
    webhooks: dict[str, str] = dict(self.strategy_webhooks)
    for key, value in os.environ.items():                        # ★ 只扫 os.environ
        if key.upper().startswith(prefix):
            strategy_key = key[len(prefix):].lower()             # 'MA_VOLUME' → 'ma_volume'
            webhooks[strategy_key] = value
    object.__setattr__(self, "strategy_webhooks", webhooks)       # 绕过 pydantic 保护
```

三点解读：

1. `model_post_init` 是 pydantic v2 的钩子，**实例构造完成后自动调用**。
2. `key[len(prefix):].lower()` → `STRATEGY_WEBHOOK_MA_VOLUME` 去掉前缀得 `MA_VOLUME`，转小写得 `ma_volume`，与 `MaVolumeStrategy.webhook_key` **完全对上**。
3. `object.__setattr__` 是因为 pydantic 的模型实例默认不允许随意赋值（`model_config` 没开 `validate_assignment` 时，普通 `self.x = y` 在 v2 里其实可以，但作者选择了显式绕过，属于保守写法）。

### 3.3 🔑 隐藏依赖：`main.py` 里的 `load_dotenv()`

```python
# main.py:10-11 —— 这两行是全项目最容易被误删的两行
from dotenv import load_dotenv
load_dotenv()
```

**为什么必需**：pydantic 的 `env_file=".env"` 只把 `.env` 当成"配置来源"来读字段，**不会**往 `os.environ` 里写。而 `model_post_init` 是遍历 `os.environ` 的。所以：

| 场景 | `feishu_webhook_url` | `strategy_webhooks` |
|---|---|---|
| `python main.py`（有 `load_dotenv`） | ✅ | ✅ 正确填充 |
| 删掉 `load_dotenv()`，值只在 `.env` 里 | ✅ 仍然能读到 | ❌ **空字典** |
| 值在真实环境变量里（Docker/K8s） | ✅ | ✅ |

也就是说：**删掉 `load_dotenv()` 不会报错，只会让 9 个策略静默地全部推到同一个默认机器人**。这是典型的"沉默失败"，排查时先查这里。

### 3.4 `settings_customise_sources` 是死代码

```python
@classmethod
def settings_customise_sources(cls, settings_cls, **kwargs):      # config.py:20
    from pydantic_settings import EnvSettingsSource              # ← 导入后没用
    import os
    sources = super().settings_customise_sources(settings_cls, **kwargs)
    prefix = "STRATEGY_WEBHOOK_"
    webhooks = {...}                                             # 扫了一遍
    if webhooks:
        original_init = kwargs.get("init_settings")              # ← 取到但没用
        os.environ.setdefault("_STRATEGY_WEBHOOKS_PARSED", "1")  # ← 往 os.environ 塞了个垃圾变量
        cls._parsed_strategy_webhooks = webhooks                 # ← 挂到类上，从没被读过
    return sources                                               # ← 原样返回，什么也没改
```

这个方法只做了"扫描 + 存到类属性"，**没有修改任何 settings source**，返回值与父类实现完全相同。真正生效的是 `model_post_init`。副作用是给 `os.environ` 塞了一个 `_STRATEGY_WEBHOOKS_PARSED=1`（无害但脏）。可以直接删除。见 [09](09-code-review-findings.md#p2-2)。

---

## 4. 路由：`get_webhook_url`

```python
def get_webhook_url(self, webhook_key: str) -> str:               # config.py:59
    return self.strategy_webhooks.get(webhook_key.lower(), self.feishu_webhook_url)
```

- 查不到专属 URL 就 fallback 到默认机器人（**不会报错**）。
- `webhook_key.lower()` 保证大小写不敏感。

路由表（当前 9 个策略）：

| `webhook_key` | 对应的环境变量 | 使用该键的策略 |
|---|---|---|
| `ma_volume` | `STRATEGY_WEBHOOK_MA_VOLUME` | `MaVolumeStrategy` |
| `turtle` | `STRATEGY_WEBHOOK_TURTLE` | `TurtleTradeStrategy` |
| `flag` | `STRATEGY_WEBHOOK_FLAG` | `HighTightFlagStrategy` |
| `shakeout` | `STRATEGY_WEBHOOK_SHAKEOUT` | `LimitUpShakeoutStrategy` |
| `limit_down` | `STRATEGY_WEBHOOK_LIMIT_DOWN` | `UptrendLimitDownStrategy` |
| `rps` | `STRATEGY_WEBHOOK_RPS` | `RpsBreakoutStrategy` |
| `trend_pullback` | `STRATEGY_WEBHOOK_TREND_PULLBACK` | `TrendPullbackStrategy` |
| `reversal_bounce` | `STRATEGY_WEBHOOK_REVERSAL_BOUNCE` | `ReversalBounceStrategy` |
| `private_placement` | `STRATEGY_WEBHOOK_PRIVATE_PLACEMENT` | `PrivatePlacementStrategy` |
| `default` | `FEISHU_WEBHOOK_URL` | 任何未覆盖 `webhook_key` 的策略 |

**配置一个策略一个群**的推荐做法：在飞书里建 7 个群，各加一个自定义机器人，把 7 个 webhook 填进 `.env`。这样每个策略的信号独立成流，便于观察哪个策略有效。

---

## 5. 飞书推送器 FeishuNotifier

### 5.1 调用链

```python
notifier.send(symbols, strategy_name, webhook_key)                # main.py:87
    ├─ url = settings.get_webhook_url(webhook_key)                 # 路由
    ├─ payload = self._build_card(symbols, strategy_name)          # 造卡片
    │     └─ self._get_stock_names(symbols)                        # 本地 stock_info 表
    └─ requests.post(url, data=json.dumps(payload), timeout=10)    # 发送
          └─ 判定 resp.status_code == 200 and resp_json["code"] == 0
```

### 5.2 卡片结构（飞书 interactive 卡片）

```json
{
  "msg_type": "interactive",
  "card": {
    "header": {
      "title": { "tag": "plain_text", "content": "📈 Sequoia-X 选股播报 | MaVolumeStrategy" },
      "template": "blue"
    },
    "elements": [
      { "tag": "div", "text": { "tag": "lark_md",
        "content": "**日期：** 2026-11-19\n**策略：** MaVolumeStrategy\n**选股数量：** 3" } },
      { "tag": "hr" },
      { "tag": "div", "text": { "tag": "lark_md",
        "content": "**选股列表：**\n[浦发银行](https://xueqiu.com/S/SH600000) [平安银行](https://xueqiu.com/S/SZ000001)" } }
    ]
  }
}
```

| 元素 | 作用 |
|---|---|
| `msg_type: interactive` | 声明发的是卡片（不是纯文本） |
| `header.template` | 标题栏配色，`blue` 固定写死 |
| `tag: "div"` + `tag: "lark_md"` | 富文本块，支持 `**粗体**` 和 `[文字](链接)` |
| `tag: "hr"` | 分隔线 |
| 股票链接 | 指向雪球个股页，点一下就能看行情 |

### 5.3 股票代码转换

```python
@staticmethod
def _to_xueqiu_code(code: str) -> str:                            # feishu.py:32
    if code.startswith("6"):
        return f"SH{code}"          # 上交所（含 688 科创板）
    elif code.startswith(("4", "8")):
        return f"BJ{code}"          # 北交所
    return f"SZ{code}"              # 深交所（00/30）
```

注意这里**正确处理了北交所**（`4`/`8` → `BJ`），而 `DataEngine._to_baostock_code` 没有。两处不一致，见 [09](09-code-review-findings.md#p1-3)。

### 5.4 股票名称查询

```python
def _get_stock_names(self, symbols: list[str]) -> dict[str, str]:   # feishu.py
    try:
        return self._directory.get_names(symbols)   # 先查本地 stock_info 表
    except Exception as exc:
        logger.warning(f"查询股票名称失败，回落为代码显示：{exc}")
        return {}
```

名称来自本地 `stock_info` 表（`sequoia_x/data/cache.py` 的 `StockDirectory`），
**不再逐只打 baostock**：实测 208 只从 37.7 s 降到 3 ms。

- 冷启动（`stock_info` 为空）：用一次 `query_all_stock(day)` 拉全市场名单，约 30~42 s；
- 之后：纯本地查询；
- 缺失策略：只缺 1~2 只时不重建目录（直接回落显示代码），缺 ≥3 只才重建，
  避免为一只新股票每次多付 30 s；
- 查询失败：返回空字典，卡片回落显示 `SH600000` 这类代码，**不影响推送**。

> 查不到名字时的 fallback 在 `_build_card` 里：`name = names.get(code, xq_code)`，
> 所以卡片永远不会出现空白。

### 5.5 错误处理（这里有个容易看漏的判定）

```python
resp = requests.post(url, data=json.dumps(payload),
                     headers={"Content-Type": "application/json"}, timeout=10)
resp_json = resp.json()

# 飞书真正的成功标志是内部的 code == 0
if resp.status_code != 200 or resp_json.get("code") != 0:
    logger.error(f"飞书推送失败 [{webhook_key}] HTTP状态={resp.status_code} 飞书响应={resp.text}")
else:
    logger.info(f"飞书推送成功 [{webhook_key}]，共 {len(symbols)} 只股票")
```

**关键点**：飞书 Webhook 即使 token 错误、签名不对，也常常返回 **HTTP 200**，真正的错误在响应体的 `code` 字段（如 `19021` = webhook 不存在/已删除）。所以只判断 `status_code == 200` 是不够的，作者在这里检查了业务码，是对的。

`except requests.RequestException` 只捕获网络/超时类异常，然后记 ERROR **不抛出**（docstring 也声明"不抛出异常"）。这保证了"某个群机器人配错了"不会中断整个选股流程。

> ⚠️ 但 `resp.json()` 如果拿到非 JSON 响应（例如公司代理返回 HTML 502 页），会抛 `json.JSONDecodeError`（`ValueError` 子类），**不属于** `RequestException`，会一路冒泡到 `main.py` 的兜底 → 进程 `exit(1)`。见 [09](09-code-review-findings.md#p1-4)。

---

## 6. 配置检查清单（部署时逐项打勾）

```dotenv
# ① 数据库路径（相对路径基于**当前工作目录**，不是项目根目录！）
DB_PATH=data/sequoia_v2.db

# ② 数据起始日期（只在首次回填时生效）
START_DATE=2024-01-01

# ③ 策略读取的行情窗口（交易日数，默认 130，最小 62；调大更准、调小更快）
SNAPSHOT_DAYS=130

# ③ 默认机器人（必填）
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx

# ④ 策略专属机器人（可选，7 个）
STRATEGY_WEBHOOK_MA_VOLUME=...
STRATEGY_WEBHOOK_TURTLE=...
STRATEGY_WEBHOOK_FLAG=...
STRATEGY_WEBHOOK_SHAKEOUT=...
STRATEGY_WEBHOOK_LIMIT_DOWN=...
STRATEGY_WEBHOOK_RPS=...
STRATEGY_WEBHOOK_TREND_PULLBACK=...
STRATEGY_WEBHOOK_REVERSAL_BOUNCE=...
STRATEGY_WEBHOOK_PRIVATE_PLACEMENT=...
```

| 检查项 | 命令 / 方法 |
|---|---|
| `.env` 存在 | `ls -l .env` |
| webhook 可达 | `curl -X POST -H "Content-Type: application/json" -d '{"msg_type":"text","content":{"text":"ping"}}' <URL>` |
| 配置能加载 | `python -c "from sequoia_x.core.config import get_settings; s=get_settings(); print(s.db_path, s.strategy_webhooks.keys())"` |
| 策略路由正确 | 上面命令应打印出 7 个 key（`ma_volume`, `turtle`, ...） |

### 常见配置错误 → 症状

| 现象 | 原因 |
|---|---|
| 启动即 `ValidationError: feishu_webhook_url Field required` | `.env` 没建，或变量名拼错 |
| 9 个策略的卡片全发到同一个群 | `load_dotenv()` 被删，或 `.env` 里变量名拼错（如 `STRATEGY_WEBHOOK_MAVOLUME` 少了 `_`） |
| 日志 `飞书推送失败 [turtle] ... 飞书响应={"code":19021,...}` | webhook token 错/机器人被移出群 |
| 卡片里股票名显示成 `SH600000` | `stock_info` 目录没有该代码（可能是北交所/新上市），或目录刷新失败 |
| `Extra inputs are not permitted` | `extra="ignore"` 被删 |

---

下一步：怎么把这一切跑起来 → [07 运行与运维](07-run-and-ops.md)
