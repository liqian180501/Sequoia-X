# 08 · 测试与质量

> 目录：[tests/](../tests/) · 配置：`pyproject.toml` 的 `[tool.pytest.ini_options]` 与 `[tool.ruff]`

---

## 1. 技术选型

| 工具 | 作用 | Java 类比 |
|---|---|---|
| `pytest` | 测试框架（自动发现 `test_*.py`、`test_*` 函数） | JUnit 5 |
| `hypothesis` | **属性测试**：给一个函数喂随机输入，验证"对所有输入都应成立的属性" | jqwik |
| `pytest-mock` | `mocker` fixture（本项目其实直接用 `unittest.mock`） | Mockito |
| `ruff` | lint + format 二合一 | Checkstyle + Spotless |

关键区别：**pytest 不需要继承任何基类，不需要 `@Test` 注解**。函数名以 `test_` 开头、文件叫 `test_*.py` 就会被发现。

```python
def test_unique_symbol_date_constraint(...):   # 一个普通函数就是一个测试
    assert count == 1                          # assert 就是断言（不是 assert 语句的"编译期开关"，pytest 会重写它）
```

**Java 对照**：

| JUnit | pytest |
|---|---|
| `@Test void foo()` | `def test_foo():` |
| `assertEquals(a, b)` | `assert a == b` |
| `assertTrue(x)` | `assert x` |
| `assertThrows(E.class, ...)` | `with pytest.raises(E):` |
| `@BeforeEach` | fixture 函数 + 参数注入 |
| `@RepeatedTest` / jqwik `@Property` | `@given(...)` |
| `@MockBean` | `unittest.mock.patch` / `monkeypatch` |

---

## 2. hypothesis 是什么：用随机输入找反例

**传统测试**（example-based）：手写 3 个输入，验证 3 个结果。

**属性测试**（property-based）：
1. 你描述**输入的形状**（"任意 6 位数字字符串"、"任意 2024~2025 之间的日期"）
2. 你描述**必须成立的属性**（"重复插入两次后，库里只有 1 行"）
3. hypothesis **自动生成 100 个随机输入**（还能自动缩小到最小反例）

```python
@given(                                                    # ① 声明随机输入的生成器
    symbol=st.text(min_size=6, max_size=6, alphabet="0123456789"),
    trade_date=st.dates(min_value=date(2024, 1, 1), max_value=date(2025, 12, 31)),
)
@h_settings(max_examples=50, deadline=None)                # ② 生成 50 组，不限单次耗时
def test_unique_symbol_date_constraint(symbol: str, trade_date: date) -> None:
    """任意 (symbol, date) 插入两次，库中都应只有 1 条记录。"""      # ③ 属性描述
    ...
```

为什么适合这个项目：SQLite 唯一约束、配置优先级、返回类型这些**都是"对所有输入都该成立"的性质**，比手写 3 个 case 覆盖力强得多。

`deadline=None` 关掉了"单次执行超过 200ms 就失败"的默认限制——因为每个用例都要建临时数据库 + 建表，必然超过。

---

## 3. 现有测试清单（13 个属性中的 9 个）

| 文件 | 测试函数 | 验证的属性 | 手段 |
|---|---|---|---|
| `test_config.py` | `test_env_overrides_default` | **P1**：任意合法 `DB_PATH` 通过环境变量设置后，`Settings` 应反映该值 | hypothesis + `monkeypatch.setenv` |
| `test_config.py` | `test_missing_required_field_raises` | **P2**：缺 `feishu_webhook_url` 时抛 `ValidationError` | `Settings(_env_file=None)` |
| `test_logger.py` | `test_get_logger_same_instance` | **P3**：任意 name，两次 `get_logger` 返回同一实例，且只有 1 个 handler | hypothesis |
| `test_data_engine.py` | `test_unique_symbol_date_constraint` | **P4**：同一 `(symbol,date)` 插两次，库里只有 1 行 | 临时库 + `IntegrityError` |
| `test_strategy.py` | `test_strategy_run_returns_list_of_str` | **P9**：`run()` 返回 `list[str]`，元素非空 | `patch.object` 桩 |
| `test_feishu.py` | `test_notification_contains_all_symbols` | **P10**：请求体包含全部 symbol | `patch("requests.post")` |
| `test_feishu.py` | `test_notification_uses_config_url` | **P11**：请求 URL 等于 `settings.feishu_webhook_url` | 同上 |
| `test_feishu.py` | `test_http_failure_logs_error` | **P12**：非 200 时记 ERROR 且不抛异常 | 挂自定义 handler 抓日志 |
| `test_main.py` | `test_main_exits_nonzero_on_exception` | **P13**：任意未捕获异常导致 `sys.exit(1)` | `patch.object(main_module, "get_settings")` |

### 3.1 属性编号 5、6、7、8 去哪了？

测试函数里的注释写的是 "Property 1/2/3/4/9/10/11/12/13"，**5~8 缺失**。结合 `.gitignore` 里的 `.kiro` 条目（Kiro 是一个规范驱动开发工具），可以推断：项目最初按一份 spec 编号了 13 条属性，其中 5~8（大概率是"数据同步/回填的正确性"）**没有被落地成测试**。

这解释了为什么现在的测试**完全没有覆盖**：
- `sync_today_bulk()` 的增量和 DELETE-by-date 行为
- `backfill()` 的续传/重试行为
- `get_all_symbols()` 的过滤逻辑

这是当前测试体系最大的空洞，见第 6 节。

---

## 4. 怎么跑

```bash
# ① 先装测试依赖（当前 .venv 里没有 pytest）
uv sync --extra dev
# 或
pip install ".[dev]"

# ② 跑全部
pytest                      # 自动读 pyproject.toml 的 testpaths = ["tests"]
python -m pytest            # 等价写法

# ③ 常用选项
pytest -v                   # 显示每个测试名
pytest tests/test_config.py # 只跑一个文件
pytest -k "feishu"          # 只跑名字含 feishu 的
pytest -x                   # 第一个失败就停
pytest --tb=short           # 简短的堆栈
pytest -p no:cacheprovider  # 不写 .pytest_cache
```

### 4.1 ⚠️ 这些测试会**真的访问网络**

`test_feishu.py` 只 patch 了 `requests.post`，但 `_build_card()` 内部会调用 `FeishuNotifier._get_stock_names()` → `baostock.login()` + `query_stock_basic()` —— **没有 mock**。所以：

- 无网络时：`bs.login()` 打一行 `login failed`，`mapping` 为空，走 fallback，测试**仍然通过**（只是慢 1~2 秒/例）。
- 有网络时：每个用例几十次真实查询 × `max_examples=50` → 可能跑几分钟。

这是测试设计的缺陷：**单元测试不应依赖外网**。修法见第 7 节。

同理 `test_strategy.py` 里 `DataEngine(settings)` 会真的建临时库（这是对的），而 `MaVolumeStrategy.run()` 里 `get_ohlcv` 被 patch 成返回空 DataFrame，所以不触网。

### 4.2 预期结果

```
tests/test_config.py ....                    [ 4%]  （各 hypothesis 用例）
tests/test_data_engine.py .....              [ 8%]
tests/test_feishu.py ..........              [50%]
tests/test_logger.py .........               [70%]
tests/test_main.py ......
tests/test_strategy.py ...
```

全绿即通过。hypothesis 会在 `.hypothesis/` 存数据库缓存（已在 `.gitignore`）。

---

## 5. 代码规范：ruff

```toml
[tool.ruff]
line-length = 100
target-version = "py311"
select = ["E", "F", "I", "UP"]
```

| 规则组 | 含义 | Java 类比 |
|---|---|---|
| `E` | pycodestyle 错误（缩进、空白、行长） | Checkstyle |
| `F` | Pyflakes（未使用导入、未定义变量） | `-Xlint` / PMD |
| `I` | isort（导入排序） | 组织 import 顺序 |
| `UP` | pyupgrade（自动用新语法，如 `Optional[X]` → `X \| None`） | 现代化重构 |

```bash
ruff check .            # 只检查
ruff check . --fix      # 自动修（含 import 排序、未用导入）
ruff format .           # 格式化（替代 black）
```

> ⚠️ **配置有个坑**：从 ruff 0.2 起，`select` 必须放在 `[tool.ruff.lint]` 下面。现在写在 `[tool.ruff]` 顶层，新版 ruff 会给出 deprecation 警告甚至忽略该配置。建议改成：
>
> ```toml
> [tool.ruff]
> line-length = 100
> target-version = "py311"
>
> [tool.ruff.lint]
> select = ["E", "F", "I", "UP"]
> ```

### 5.1 当前大概率会被 ruff 抓到的问题

| 文件 | 问题 | 规则 |
|---|---|---|
| `ma_volume.py:3` | `import pandas as pd` 未使用 | F401 |
| `high_tight_flag.py:3` | 同上 | F401 |
| `limit_up_shakeout.py:3` | 同上 | F401 |
| `main.py:13` | `from datetime import date` 未使用 | F401 |
| `rps_breakout.py:1-2` | `import pandas` 在 `import sqlite3` 之前（排序） | I001 |
| `config.py:22` | `import os` 在函数内、且重复导入 | 规则外（不算 F401） |
| `config.py:37` | 赋值给未使用的变量 `original_init` | F841 |
| `turtle_trade.py` | 行内注释超 100 字符（中文按宽度算） | E501 可能触发 |

跑一次 `ruff check . --statistics` 就能看到准确结果。

---

## 6. 测试覆盖的空洞（这是接手后最该补的）

| 缺口 | 风险 | 建议补的测试 |
|---|---|---|
| **策略数学逻辑零测试** | 改一个阈值就可能静默改变选股结果，没人发现 | 用固定 OHLC 序列构造"该入选/不该入选"的用例，断言 `run()` 结果 |
| `sync_today_bulk` 无测试 | DELETE-by-date 会误删（见 [09](09-code-review-findings.md#p1-1)） | 造两天的数据，只让部分股票有增量，断言其他股票数据仍在 |
| `backfill` 无测试 | 续传/重试逻辑没验证 | mock `bs.query_history_k_data_plus`，验证重试次数和 skip 行为 |
| `_to_baostock_code` / `_to_xueqiu_code` 无测试 | 北交所映射错误（见 09） | 参数化：`'600000'→'sh.600000'`、`'430047'→'bj.430047'` |
| 无集成测试 | 整条链路（同步→策略→推送）从没端到端跑过测试 | 一个 `@pytest.mark.slow` 的冒烟测试，用 10 只股票的假库 |
| 无 CI | 提交不跑测试 | `.github/workflows/test.yml`：`uv sync --extra dev && pytest` |
| 无 mypy / pyright | 类型注解全靠自觉 | 加 `mypy --strict` 会暴露一堆问题，可先只对 `core/` 开 |

---

## 7. 怎么补第一个像样的测试（示范）

**目标**：验证"代码前缀 → 市场"的映射，并把北交所这个已知缺口钉死。

```python
# tests/test_data_engine.py 追加
import pytest
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier


@pytest.mark.parametrize("symbol,expected", [
    ("600000", "sh.600000"),     # 上交所主板
    ("688001", "sh.688001"),     # 科创板
    ("000001", "sz.000001"),     # 深交所主板
    ("300750", "sz.300750"),     # 创业板
])
def test_to_baostock_code(symbol: str, expected: str) -> None:
    assert DataEngine._to_baostock_code(symbol) == expected


@pytest.mark.parametrize("symbol,expected", [
    ("600000", "SH600000"),
    ("000001", "SZ000001"),
    ("430047", "BJ430047"),      # 推送端正确识别北交所
])
def test_to_xueqiu_code(symbol: str, expected: str) -> None:
    assert FeishuNotifier._to_xueqiu_code(symbol) == expected
```

> 这里刻意**没有**给 `_to_baostock_code` 断言 `430047` 的结果——因为 baostock 不支持北交所（实测 `err=10004011`），"应该返回什么"取决于你选择"排除"还是"接第二个数据源"（见 [09 · P1-3](09-code-review-findings.md#p1-3)）。等你定下策略后，再把这个用例补成断言。

`@pytest.mark.parametrize` ≈ JUnit 的 `@ParameterizedTest` + `@CsvSource`。

**目标**：给策略逻辑加一个确定性测试（不联网）。

```python
def test_turtle_selects_on_breakout(tmp_path):
    settings = Settings(db_path=str(tmp_path / "t.db"),
                        start_date="2024-01-01",
                        feishu_webhook_url="https://example.com/hook")
    engine = DataEngine(settings)

    # 造 20 天横盘 + 今天放量突破
    df = pd.DataFrame({
        "symbol": ["600000"] * 21,
        "date": [f"2024-01-{i:02d}" for i in range(1, 22)],
        "open":  [10.0] * 20 + [10.0],
        "high":  [10.5] * 20 + [12.0],
        "low":   [ 9.5] * 20 + [ 9.9],
        "close": [10.0] * 20 + [11.5],
        "volume":[1e6]  * 20 + [5e6],
        "turnover": [1.2e8] * 21,
    })
    with patch.object(engine, "get_ohlcv", return_value=df), \
         patch.object(engine, "get_local_symbols", return_value=["600000"]), \
         patch.object(TurtleTradeStrategy, "_get_market_caps", return_value={}):
        result = TurtleTradeStrategy(engine, settings).run()

    assert result == ["600000"]
```

注意要 patch 掉 `_get_market_caps`，否则它会真的去连 baostock——这正是第 4.1 节说的"测试要隔离外部依赖"。

---

## 8. 如果要加 CI

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --extra dev
      - run: uv run ruff check .
      - run: uv run pytest -q
```

注意：加了 CI 之后，`test_feishu.py` 的真实网络调用会在 CI 里跑（可能偶发失败），所以应该先把 `_get_stock_names` mock 掉。

下一步：已发现的问题与改进建议 → [09 代码评注与改进建议](09-code-review-findings.md)
