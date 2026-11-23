# 02 · Python 速成（Java 开发者视角）

> 目标：读懂本仓库的每一行 Python。只讲这个项目里**真正出现过**的语法，不铺开讲语言。

---

## 1. 先建立直觉：同样的逻辑，Java vs Python

**Java**

```java
public class MaVolumeStrategy implements Strategy {
    private final DataEngine engine;
    public MaVolumeStrategy(DataEngine engine) { this.engine = engine; }

    @Override
    public List<String> run() {
        List<String> selected = new ArrayList<>();
        for (String symbol : engine.getLocalSymbols()) {
            DataFrame df = engine.getOhlcv(symbol);
            if (df.rowCount() < 20) continue;
            /* ... */
        }
        return selected;
    }
}
```

**Python（就是本仓库 [ma_volume.py](../sequoia_x/strategy/ma_volume.py)）**

```python
class MaVolumeStrategy(BaseStrategy):
    webhook_key: str = "ma_volume"          # 类属性（相当于 static final，但可被实例覆盖）

    def run(self) -> list[str]:
        symbols = self.engine.get_local_symbols()
        selected: list[str] = []
        for symbol in symbols:
            df = self.engine.get_ohlcv(symbol)
            if len(df) < 20:
                continue
            # ...
        return selected
```

需要立刻注意的差异：

| Java | Python | 说明 |
|---|---|---|
| `{ }` 划分代码块 | **缩进**划分（4 空格） | 缩进错了程序语义就变了 |
| `this` 隐式 | `self` **显式**写在第一个参数 | 但调用时不用传，`s.run()` 自动绑定 |
| `String name` | 变量无类型声明 | 类型靠注解（可选，不强制） |
| `new MaVolume(...)` | `MaVolume(...)` | 无 `new` |
| `List<String>` | `list[str]` 或 `List[str]` | 内置泛型，小写 |
| `boolean` | `bool`（`True`/`False` 大写） | |
| `null` | `None` | 且还有 `NaN` 这种第三方"空"，见第 6 节 |
| `argv[0]` | `sys.argv[0]` | |

---

## 2. 语法对照总表

| 概念 | Java | 本仓库里的 Python |
|---|---|---|
| 空值 | `null` | `None` |
| 布尔 | `true` / `false` | `True` / `False` |
| 逻辑运算 | `&&` `\|\|` `!` | `and` `or` `not` |
| 位/整数除 | `/` 整数相除截断 | `/` 永远是**浮点**除法；整除用 `//` |
| 字符串插值 | `"a" + b` / `String.format` | **f-string**：`f"选出 {n} 只"` |
| 多行字符串 | 无（文本块 `"""`，Java 15+） | `"""..."""` → 本项目**所有模块首行都是它**（文档字符串） |
| 三元 | `a ? b : c` | `b if a else c`（本仓库少见） |
| 循环 | `for (int i=0; i<n; i++)` | `for i in range(n)` |
| 增强 for | `for (var x : list)` | `for x in list` |
| 枚举下标 | `list.get(list.size()-1)` | `list[-1]`（**负索引从尾部数**） |
| 切片 | `list.subList(a, b)` | `list[a:b]`（**含头不含尾**） |
| 字典 | `Map<K,V>` | `dict`，字面量 `{"k": v}` |
| 列表 | `ArrayList<T>` | `list`，字面量 `[1, 2]` |
| 元组 | 无（用 record/数组） | `tuple`，`(a, b)`，**不可变**；函数返回多值靠它 |
| 集合 | `HashSet` | `set` |
| 接口/抽象类 | `interface` / `abstract class` | `abc.ABC` + `@abstractmethod` |
| 方法重载 | 支持 | **不支持**（同名即覆盖，靠默认参数区分） |
| 泛型 | 编译期擦除但强校验 | 注解形式 `list[str]`，**运行时不校验** |
| 访问修饰符 | `public/private/protected` | 只有约定：`_x` 表示"请不要从外部用" |
| 常量 | `static final` | 全大写命名约定（`_MIN_BARS`、`_CREATE_TABLE_SQL`） |
| 受检异常 | `throws IOException` | **不存在**受检异常，全部是非受检 |
| try-with-resources | `try (var c = ...) {}` | `with open(...) as f:`（上下文管理器） |
| 方法引用 | `Foo::bar` | `Foo.bar`（一等公民，可直接传） |
| Stream | `list.stream().filter().map().collect()` | 列表推导式 / 生成器表达式 |

---

## 3. 类与对象模型

### 3.1 `__init__` 就是构造函数，`self` 就是 `this`

```python
class DataEngine:
    def __init__(self, settings: Settings) -> None:   # engine.py:59
        self.db_path: str = settings.db_path          # 声明+赋值，无需先声明字段
        self.start_date: str = settings.start_date
        self._init_db()                               # 调用自己的方法
```

- Python **没有字段声明**：`self.xxx = ...` 第一次赋值就等于"新增了一个字段"。
- 没有 `private`：`self._init_db` 的下划线是**约定**"内部方法"，外部仍然能调用（`engine._to_baostock_code(...)` 在 [turtle_trade.py:42](../sequoia_x/strategy/turtle_trade.py#L42) 就真的这么干了）。
- 不写 `__init__` 也能实例化（继承父类的）。

### 3.2 类属性 vs 实例属性（本仓库大量使用）

```python
class TurtleTradeStrategy(BaseStrategy):
    webhook_key: str = "turtle"     # 类属性：所有实例共享
    _MIN_BARS: int = 21             # 类属性：当常量用
```

`main.py` 里读的是 `strategy.webhook_key`（实例访问 → 找不到实例属性时回落到类属性）。这和 Java 的 `static final` 常量读起来一样，但 Python **允许**子类/实例覆盖它，这正是"每个策略路由到不同飞书机器人"的实现方式。

### 3.3 三种"方法"

```python
class FeishuNotifier:
    @staticmethod                                # 不接收 self，相当于 Java static 方法
    def _to_xueqiu_code(code: str) -> str: ...

    def send(self, ...): ...                     # 普通实例方法

    # @classmethod 本仓库在 config.py:20 用过（第一个参数是 cls 而不是 self）
    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs): ...
```

### 3.4 抽象基类 ≈ `abstract class`

[base.py](../sequoia_x/strategy/base.py)：

```python
class BaseStrategy(ABC):           # 继承 abc.ABC 才能用 @abstractmethod
    @abstractmethod
    def run(self) -> list[str]:
        ...                        # 三个点 = 空实现（pass 的惯用写法）
```

- 和 Java 一样：**子类不实现 `run()` 就无法实例化**（`TypeError: Can't instantiate abstract class`）。
- 和 Java 不一样：Python 没有 `interface` 关键字，用"只有抽象方法的 ABC"或 `typing.Protocol` 来表达接口。

### 3.5 命名约定

| 写法 | 含义 | 例子 |
|---|---|---|
| `name` | 公开 | `engine`, `settings` |
| `_name` | 内部（约定） | `_MIN_BARS`, `_init_db`, `_bs_fetch_batch` |
| `__name` | 触发名称改写（name mangling），罕见 | 本仓库没用 |
| `__xxx__` | 双下划线魔术方法 | `__init__` |
| `UPPER_CASE` | 模块级常量 | `_CREATE_TABLE_SQL`, `_FORMAT` |

---

## 4. 类型注解：只是"文档"，运行时不检查

```python
def get_ohlcv(self, symbol: str) -> pd.DataFrame:      # engine.py:80
    ...
```

- `symbol: str` / `-> pd.DataFrame` **不产生任何运行时校验**。传个整数进去也能跑，只是编辑器/ruff/mypy 会报警。
- 所以本仓库才要在**外部数据入口**手动做类型清洗：
  ```python
  df["close"] = pd.to_numeric(df["close"], errors="coerce")   # engine.py:265
  ```
  如果指望"注解是 `str` 所以一定是数字"，就会在 pandas 运算时炸掉。这是 Python 和 Java 最大的心态差异之一。
- `list[str]`、`dict[str, str]`、`str | None`（= `Optional[str]`）都是 3.9/3.10+ 的写法，项目要求 `>=3.10` 正是为了这个。

> 例外：**pydantic 会真的校验**。`Settings.feishu_webhook_url: str` 少了这个环境变量就抛 `ValidationError`（[config.py:9](../sequoia_x/core/config.py#L9)）。pydantic 把注解变成了运行时校验，这也是它像 Lombok+BeanValidation 的原因。

---

## 5. 模块与包

```
sequoia_x/               ← 包（package）
├── __init__.py          ← 标记这是一个包（等价于 package 声明）
├── core/
│   ├── __init__.py
│   └── config.py        ← 模块（module）≈ 一个 .java 文件，但**一个模块里可以放多个类**
```

导入：

```python
from sequoia_x.strategy.base import BaseStrategy       # 绝对导入（本项目统一用这种）
import pandas as pd                                    # 起别名
from datetime import date, timedelta                   # 从标准库
```

要点：

1. **一个 `.py` 文件可以包含任意多个类/函数**。`engine.py` 里除了 `DataEngine` 类还有模块级函数 `_bs_fetch_batch` 和两个 SQL 常量。
2. `__init__.py` 是空文件也必须有（或者用 namespace package，但本项目老老实实放了空文件，只有一个文档字符串）。
3. 没有 `package-info.java`；模块的文档写在文件第一行 `"""..."""`。

### 5.1 为什么有些 `import` 写在函数体里面？

```python
def sync_today_bulk(self) -> int:            # engine.py:97
    from datetime import date, timedelta     # ← 函数内导入
    from multiprocessing import Pool
    ...
```

这**不是**坏习惯，两个常见原因：

1. **延迟加载**：`baostock`、`akshare` 这些库首次 import 要几秒，只在真正用到时才导入，启动更快。
2. **避开循环导入**（Python 里 `import` 是运行时执行模块，循环依赖会直接报错，不像 Java 编译期能解决）。

本仓库里 `baostock`、`akshare` 全是函数内导入；`pandas`、`sqlite3` 是模块顶层导入。

---

## 6. 空值：`None` 之外还有 `NaN`（重点）

这是 Java 开发者最容易踩的坑。

```python
# Java
if (x == null) { ... }

# Python 基础
if x is None:          # 判断 None 用 `is`，不要用 `==`
    ...
```

但本仓库的数据里还有 pandas 的 `NaN`（"非数字"）：

| 来源 | 值 | 判断方法 |
|---|---|---|
| Python 空 | `None` | `x is None` |
| 浮点缺失 | `float('nan')` | `math.isnan(x)`、`pd.isna(x)` |
| pandas 缺失 | `pd.NA` / `NaT` | `pd.isna(x)` |
| DataFrame 空 | — | `df.empty`（返回 bool） |

**致命细节：`NaN` 不等于自己。**

```python
float('nan') == float('nan')     # False ！
```

所以本仓库用两种方式处理缺失值：

**方式一：显式判空（turtle / uptrend）**

```python
if pd.isna(last["high_20"]):     # engine 数据不足时滚动窗口是 NaN
    continue
```

**方式二：靠 NaN 比较恒为 False 静默跳过（ma_volume / limit_up_shakeout）**

```python
golden_cross = prev["ma5"] < prev["ma20"] and last["ma5"] > last["ma20"]
# 如果 prev["ma20"] 是 NaN → NaN < NaN 是 False → 整个条件 False → 自然不选中
```

方式二能跑，但要理解"NaN 参与比较永远 False"这个前提，否则看到没有 `isna` 判断会以为漏了。

另外 `len(df) < 20: continue` 这种"数据不够就跳过"的守卫，在 7 个策略里每个都有（门槛 3 / 20 / 21 / 40 / 60 / 120 不等，见 [05](05-strategy-system.md)）。

---

## 7. 真值判断：`if` 里什么算"假"

Python 里**任何对象**都能放进 `if`，以下都算假：

```
False, None, 0, 0.0, "", [], {}, set(), 以及 df.empty == True 的 DataFrame
```

所以本仓库有这些写法：

```python
if not rows:                 # 空列表 → True（engine.py:259）
if df.empty:                 # DataFrame 专用（engine.py:269）
if selected:                 # 空 list → 跳过推送（main.py:86）
if logger.handlers:          # 空 list → 说明还没配置过 handler（logger.py:25）
if last_date and last_date >= today_str:    # last_date 可能是 None
```

`if x:` 和 `if x is not None:` 在 `x` 可能是 `""`/`0` 时**语义不同**。本仓库第 4 种写法就是靠 `and` 短路保护 `None`。

> 顺带：`and`/`or` 返回的是**操作数本身**，不是 bool。`x or "默认值"` 是 Python 的"空值兜底"惯用法，本仓库用过：`start = last_date or self.start_date`（[engine.py:212](../sequoia_x/data/engine.py#L212)）。

---

## 8. 异常处理

```python
try:
    ...
except sqlite3.IntegrityError:      # 捕获特定异常
    pass                            # 什么也不做（故意吞掉唯一约束冲突）
finally:
    bs.logout()                     # 一定执行
```

几个和 Java 不同的点：

1. **没有 checked exception**，方法签名不写 `throws`。
2. 顶层兜底可以写 `except Exception:`（相当于 `catch (Exception e)`），本仓库在 `main.py:95` 和每个策略里都这么写。
3. `logger.exception(...)` 会在日志里自动附带完整堆栈（相当于 `log.error("msg", e)` 的 Python 惯用法），见 [main.py:98](../main.py#L98)。
4. `raise RuntimeError(rs.error_msg)`（[engine.py:233](../sequoia_x/data/engine.py#L233)）——没有 `new`，直接 `异常类(...)`。

**注意本仓库的异常策略是有代价的**：策略内部 `except Exception: logger.warning(...); continue` 会让单只股票的异常被吞掉（这是有意为之：5224 只里坏一只不该毁掉整次运行）。但如果你在调试"某只票为什么没选出来"，记得去日志里搜 `计算失败`。详见 [09](09-code-review-findings.md)。

---

## 9. 装饰器 ≈ 注解，但真的会改变行为

```python
@staticmethod                      # 声明为静态方法
@abstractmethod                    # 声明为抽象方法（必须子类实现）
@given(...)                        # hypothesis 属性测试的输入生成器（见 tests/）
```

语法上是 `@` + 函数名，但**语义比 Java 注解强**：装饰器是"接收函数、返回新函数"的高阶函数，调用时会真的替换原函数。本仓库只用了上面这 3 种标准场景，没有自定义装饰器。

---

## 10. 列表推导式 / 生成器 ≈ Stream API

```python
symbols = [row[0] for row in rows]                      # map
unique = [s for s in symbols if s not in seen]          # filter
tasks = [tasks[i::n_workers] for i in range(n_workers)] # 切片分片（GIL 无关的负载切分）
```

对应 Java：

```java
List<String> symbols = rows.stream().map(r -> r.get(0)).toList();
List<String> unique  = symbols.stream().filter(s -> !seen.contains(s)).toList();
```

`[f(x) for x in xs]` 生成 list；`(f(x) for x in xs)` 生成**惰性**迭代器（不占内存）。

切片 `tasks[i::n_workers]` 的含义是"从第 i 个开始，每隔 n_workers 取一个"，用来把任务均匀切成 8 份（[engine.py:129](../sequoia_x/data/engine.py#L129)）。

---

## 11. `if __name__ == "__main__":` ≈ `public static void main`

```python
if __name__ == "__main__":      # main.py:107
    main()
```

- 每个 `.py` 既是"模块"又是"可执行脚本"。
- 直接 `python main.py` 运行时，`__name__ == "__main__"`，于是执行 `main()`。
- 被 `import main` 时（比如 `tests/test_main.py` 就这么干），`__name__ == "main"`，**不会**执行 `main()`。

这个守卫在本项目的多进程场景里尤其重要：Windows 上 `multiprocessing` 用 spawn 启动子进程，子进程会重新 import 主模块——没有这个守卫就会无限递归起进程。

---

## 12. 依赖与运行环境

| Java | Python（本项目） |
|---|---|
| `pom.xml` / `build.gradle` | `pyproject.toml` |
| `mvn install` | `uv sync` 或 `pip install .` |
| `~/.m2/repository` | `.venv/`（项目内虚拟环境，**不入 git**） |
| `dependencyManagement` | `[project] dependencies` |
| `mvn test` | `pytest` |
| `spotless:apply` | `ruff format` |

```toml
# pyproject.toml
[project]
name = "sequoia-x"
version = "2.0.0"
requires-python = ">=3.10"
dependencies = ["akshare>=1.10", "baostock>=0.9", "pydantic-settings>=2.0",
                "python-dotenv>=1.0", "rich>=13.0", "pandas>=2.0", "requests>=2.31"]

[project.optional-dependencies]
dev = ["pytest>=8.0", "hypothesis>=6.100", "pytest-mock>=3.12"]
```

`[project.optional-dependencies]` 就是 Maven 的 `<scope>test</scope>` / profile，安装时加 `--extra dev`。

> 注意 `pandas>=2.0` 只是个下限，实际 `.venv` 里装的是 **pandas 3.0.1**。3.0 有几个破坏性变更（Copy-on-Write 默认开启、字符串 dtype），见 [03](03-pandas-field-guide.md) 最后一节。

---

## 13. 逐条解读本项目里"看起来奇怪"的写法

| 代码 | 位置 | 为什么这么写 |
|---|---|---|
| `webhook_key: str = "turtle"` | 各策略 | 类属性当"策略标识"，让 `main.py` 用统一方式取路由键 |
| `...`（三个点）作为函数体 | base.py:42 | `Ellipsis`，等价 `pass`，表示空实现 |
| `def model_post_init(self, __context: object)` | config.py:45 | pydantic v2 生命周期钩子，实例构造完后自动调用 |
| `object.__setattr__(self, "strategy_webhooks", webhooks)` | config.py:57 | 绕过 pydantic 的"不可变"保护，强行改字段值 |
| `cls._parsed_strategy_webhooks = webhooks` | config.py:41 | 挂在类上存临时数据（实际上**没被读过**，是死代码，见 [09](09-code-review-findings.md)） |
| `extra="ignore"` | config.py:16 | 允许 `.env` 里有 Settings 没定义的变量，否则 pydantic 报错 |
| `socket.setdefaulttimeout(10.0)` | main.py:16 | 给**所有** socket 设全局超时，防止 baostock 卡死 |
| `df["symbol"] = symbol` | engine.py:273 | 在整列上广播一个标量（pandas 特性，见 [03](03-pandas-field-guide.md)） |
| `min(8, len(tasks))` | engine.py:128 | 任务数小于 8 时不要开 8 个进程 |
| `method="multi", chunksize=500` | engine.py:152 | `to_sql` 的批量插入优化，等价 JDBC `addBatch` |
| `if not self._login(): return` | engine.py:182 | 提前返回，Python 里没有"必须返回值"约束，`return` 即 `return None` |

---

## 14. 还需要补的一块：GIL 与多进程

```python
with Pool(n_workers) as pool:                       # engine.py:131
    batch_results = pool.map(_bs_fetch_batch, chunks)
```

- `Pool.map(func, iterable)` ≈ `executor.invokeAll(tasks)`，**阻塞**直到全部完成，返回结果列表（顺序与输入一致）。
- 被提交的 `func` 必须**可 pickle**（能被序列化）——所以 `_bs_fetch_batch` 是**模块级函数**而不是 `DataEngine` 的方法（方法会把整个 `self` 连 SQLite 句柄一起序列化，直接失败）。这是 Python 多进程最典型的约束，Java 的 `Runnable` 没这个问题。
- `with Pool(...) as pool:` ≈ try-with-resources，退出时自动 `close()` + `join()`。

---

## 15. 自测清单

读到这里，你应该能回答：

1. `self` 是什么，为什么 `run()` 的签名是 `def run(self)` 而调用是 `s.run()`？
2. `webhook_key` 是类属性还是实例属性？`main.py` 怎么读到它的？
3. 为什么 `_bs_fetch_batch` 不能写成 `DataEngine` 的方法？
4. `pd.isna()` 和 `is None` 分别在什么场景用？
5. 为什么策略里 `if len(df) < 20: continue` 是必要守卫？
6. `if selected:` 为假时意味着什么？

答不上来就回到对应小节；接下来进入 pandas 部分 → [03 pandas 实战手册](03-pandas-field-guide.md)。
