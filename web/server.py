"""Sequoia-X 本地网页控制台（纯标准库，无需安装新依赖）。

启动
----
```bash
python web/server.py                 # http://127.0.0.1:8000
python web/server.py --port 8123 --host 0.0.0.0   # 谨慎：默认只监听本机
```

为什么用标准库
--------------
这个项目是"每天跑一次批处理"的形态，不需要 ORM、模板引擎或前端构建链。
``http.server`` + 一个静态 HTML 足够，换来的是：**不用改 pyproject、不用联网装包、
在任何有 Python 的机器上都能跑**。

接口
----
* ``GET  /``                    单页控制台
* ``GET  /api/status``          数据库规模、日期范围、数据新鲜度、策略清单、上次运行
* ``GET  /api/strategies``      策略标识与说明
* ``POST /api/select``          跑选股（不推送）：body ``{"strategies":["ma_volume"],"limit":30}``
* ``POST /api/push``            跑选股并推送飞书，必须带 ``"confirm": true``
* ``GET  /api/kline?symbol=600000&limit=120``   K 线数据
* ``GET  /api/backtest``        读取 ``data/backtest_summary.json``（由 backtest --out 生成）

安全
----
默认只绑定 ``127.0.0.1``，没有鉴权。``/api/push`` 会真的往飞书群发消息，
所以要求显式 ``confirm``，并且**只允许推送当前进程内刚跑出来的结果**，
避免被随手 GET 一下就把卡片发出去。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

import sys  # noqa: E402

sys.path.insert(0, str(ROOT))

from sequoia_x.core.config import get_settings  # noqa: E402
from sequoia_x.core.logger import get_logger  # noqa: E402
from sequoia_x.data.cache import StockDirectory  # noqa: E402
from sequoia_x.data.engine import DataEngine  # noqa: E402
from sequoia_x.data.ledger import SignalLedger  # noqa: E402
from sequoia_x.notify.feishu import FeishuNotifier  # noqa: E402
from sequoia_x.strategy.base import BaseStrategy  # noqa: E402
from sequoia_x.strategy.registry import STRATEGIES, resolve_strategies  # noqa: E402

logger = get_logger(__name__)

INDEX_HTML = ROOT / "web" / "index.html"
BACKTEST_JSON = ROOT / "data" / "backtest_summary.json"

#: 选股是一次重计算（读快照 + 9 个策略），用一把锁串行化，避免并发把内存顶爆
_RUN_LOCK = threading.Lock()

#: 上一次选股结果（进程内）：/api/push 只允许推它，避免"GET 一下就推送"
_LAST_RUN: dict = {}

#: 允许在网页上调的策略参数：``{策略: {属性: (标题, 默认值, 最小, 最大)}}``。
#: 只暴露少量关键参数——参数越多越容易过拟合，能调不等于该调。
TUNING: dict[str, dict[str, tuple[str, float, float, float]]] = {
    "turtle": {
        "_BREAKOUT_WINDOW": ("突破窗口(日)", 90, 20, 250),
        "_VOL_SURGE": ("量比下限", 1.2, 1.0, 3.0),
        "_MAX_BREAKOUT_MARGIN": ("突破幅度上限", 1.03, 1.0, 1.10),
    },
    "rps": {
        "rps_threshold": ("RPS 阈值", 90, 50, 99),
        "_SHORT_WEIGHT": ("短周期权重", 0.4, 0.0, 1.0),
        "_NEAR_HIGH": ("距新高比例", 0.95, 0.80, 1.0),
    },
    "ma_volume": {
        "_VOL_SURGE": ("量比下限", 1.5, 1.0, 3.0),
        "_CROSS_LOOKBACK": ("金叉回看(日)", 3, 1, 20),
        "_MAX_EXTENSION": ("不追高上限", 1.20, 1.0, 1.50),
    },
    "flag": {
        "_POLE_RATIO": ("旗杆涨幅", 1.6, 1.2, 3.0),
        "_FLAG_RANGE": ("旗面振幅上限", 0.18, 0.05, 0.40),
    },
    "shakeout": {
        "_SHRINK": ("缩量上限", 0.8, 0.3, 1.0),
        "_MAX_CLOSE_DROP": ("收盘下限", 0.97, 0.90, 1.0),
    },
    "limit_down": {
        "_DROP": ("跌幅阈值", 0.97, 0.90, 1.0),
        "_VOL_SURGE": ("量比下限", 1.5, 1.0, 3.0),
    },
    "trend_pullback": {
        "_MIN_GAIN_60": ("60日涨幅下限", 0.35, 0.05, 1.00),
        "_SHRINK": ("缩量上限", 0.8, 0.3, 1.0),
    },
}


class JobManager:
    """极简后台任务：跑子进程，状态靠轮询。

    为什么不把回测塞进 HTTP 请求：一次回测要读几百万行、跑几分钟，
    同步等待只会让浏览器超时、用户以为页面卡死。
    """

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def start(self, name: str, argv: list[str]) -> dict:
        """启动一个子进程任务，返回任务描述。"""
        with self._lock:
            self._counter += 1
            job_id = f"job{self._counter}"
        log_path = self.log_dir / f"{job_id}.log"
        # PYTHONUNBUFFERED：不加的话子进程的 stdout 会被缓冲，日志文件要等退出才有内容，
        # 页面上就看不到进度；PYTHONIOENCODING：避免中文日志写成乱码。
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        with log_path.open("w", encoding="utf-8") as fh:
            proc = subprocess.Popen(
                argv, stdout=fh, stderr=subprocess.STDOUT, cwd=str(ROOT), env=env
            )
        job = {
            "id": job_id,
            "name": name,
            "status": "running",
            "started": time.strftime("%H:%M:%S"),
            "log": str(log_path.name),
            "proc": proc,
        }
        self._jobs[job_id] = job
        return self._public(job)

    def get(self, job_id: str) -> dict | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        proc = job["proc"]
        if job["status"] == "running" and proc.poll() is not None:
            job["status"] = "done" if proc.returncode == 0 else "failed"
            job["exit_code"] = proc.returncode
            job["finished"] = time.strftime("%H:%M:%S")
        return self._public(job)

    def tail(self, job_id: str, lines: int = 20) -> list[str]:
        job = self._jobs.get(job_id)
        if job is None:
            return []
        path = self.log_dir / job["log"]
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return [line.rstrip() for line in fh.readlines()[-lines:]]

    @staticmethod
    def _public(job: dict) -> dict:
        return {k: v for k, v in job.items() if k != "proc"}


# ── 纯函数（便于单测） ──


def db_stats(db_path: str) -> dict:
    """读取数据库规模与日期范围。

    Args:
        db_path: SQLite 路径。

    Returns:
        含 ``rows / symbols / first_date / last_date / trading_days / size_mb`` 的字典。
    """
    path = Path(db_path)
    stats = {
        "path": str(path),
        "exists": path.exists(),
        "size_mb": round(path.stat().st_size / 1024 / 1024, 3) if path.exists() else 0,
        "rows": 0,
        "symbols": 0,
        "first_date": None,
        "last_date": None,
        "trading_days": 0,
    }
    if not path.exists():
        return stats
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10) as conn:
        conn.execute("PRAGMA temp_store=MEMORY")
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(date), MAX(date) FROM stock_daily"
        ).fetchone()
        stats.update(rows=row[0], symbols=row[1], first_date=row[2], last_date=row[3])
        stats["trading_days"] = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM stock_daily"
        ).fetchone()[0]
        stats["stale_symbols"] = conn.execute(
            "SELECT COUNT(*) FROM (SELECT symbol, MAX(date) d FROM stock_daily "
            "GROUP BY symbol HAVING d < (SELECT MAX(date) FROM stock_daily))"
        ).fetchone()[0]
    return stats


def enrich_picks(snap, symbols: list[str], names: dict[str, str]) -> list[dict]:
    """把代码列表补上名称、最新价、当日涨跌幅与成交额。

    Args:
        snap: ``MarketSnapshot``。
        symbols: 策略返回的代码（已按强度排序）。
        names: ``{code: name}``。

    Returns:
        表格行列表，顺序与 ``symbols`` 一致。
    """
    if not symbols or snap.row_count == 0:
        return []

    frame = snap.df
    last = frame[snap._is_last]  # noqa: SLF001 同仓库内部使用
    by_symbol = dict(zip(last["symbol"], last.index, strict=False))
    prev_close = snap.shift("close", 1)

    rows = []
    for symbol in symbols:
        index = by_symbol.get(symbol)
        if index is None:
            rows.append({"symbol": symbol, "name": names.get(symbol, ""), "close": None})
            continue
        close = float(frame.at[index, "close"])
        prev = prev_close.at[index]
        pct = None if prev != prev or not prev else (close / float(prev) - 1) * 100
        rows.append(
            {
                "symbol": symbol,
                "name": names.get(symbol, ""),
                "close": round(close, 2),
                "pct": None if pct is None else round(pct, 2),
                "turnover": round(float(frame.at[index, "turnover"]) / 1e8, 2),
            }
        )
    return rows


def strategy_meta() -> list[dict]:
    """策略清单（标识 + 类名 + 文档首行）。"""
    out = []
    for key, cls in STRATEGIES.items():
        doc = (cls.__doc__ or "").strip().splitlines()
        out.append(
            {
                "key": key,
                "name": cls.__name__,
                "doc": doc[0] if doc else "",
                "needs_market_data": key != "private_placement",
            }
        )
    return out


def candles(db_path: str, symbol: str, limit: int = 120) -> dict:
    """读取某只股票最近 ``limit`` 根日线（按时间升序返回）。"""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10) as conn:
        rows = conn.execute(
            "SELECT date,open,high,low,close,volume FROM stock_daily "
            "WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
    rows.reverse()
    return {
        "symbol": symbol,
        "bars": [{"d": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in rows],
    }


# ── 应用状态 ──


class App:
    """进程内共享的引擎/通知器，避免每次请求都重新加载快照。"""

    def __init__(self) -> None:
        load_dotenv(str(ROOT / ".env"))
        self.settings = get_settings()
        self.engine = DataEngine(self.settings)
        self.directory = StockDirectory(self.settings.db_path)
        self.notifier = FeishuNotifier(self.settings)
        self.ledger = SignalLedger(self.settings.db_path)
        self.jobs = JobManager(ROOT / "logs")

    @staticmethod
    def tuning_meta() -> list[dict]:
        """把 TUNING 转成前端能渲染的结构。"""
        out = []
        for key, params in TUNING.items():
            cls = STRATEGIES.get(key)
            if cls is None:
                continue
            out.append(
                {
                    "key": key,
                    "name": cls.__name__,
                    "params": [
                        {
                            "attr": attr,
                            "label": label,
                            "default": cls.__dict__.get(attr, default),
                            "min": lo,
                            "max": hi,
                        }
                        for attr, (label, default, lo, hi) in params.items()
                    ],
                }
            )
        return out

    def status(self) -> dict:
        db = db_stats(self.settings.db_path)
        return {
            "db": db,
            "settings": {
                "snapshot_days": self.settings.snapshot_days,
                "sync_workers": self.settings.sync_workers,
                "max_picks": self.settings.max_picks,
                "min_turnover": self.settings.min_turnover,
                "board_aware_limits": self.settings.board_aware_limits,
            },
            "strategies": strategy_meta(),
            "last_run": _LAST_RUN or None,
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def select(
        self,
        keys: list[str] | None,
        limit: int | None,
        overrides: dict[str, dict[str, float]] | None = None,
    ) -> dict:
        """跑选股（不推送），结果缓存在 ``_LAST_RUN`` 供 /api/push 使用。"""
        classes: list[type[BaseStrategy]] = (
            resolve_strategies("all") if not keys else [STRATEGIES[k] for k in keys]
        )
        names = self.directory.get_names(self.engine.get_local_symbols())
        snap = self.engine.get_snapshot()

        results = []
        started = time.perf_counter()
        for cls in classes:
            t0 = time.perf_counter()
            try:
                strategy = cls(engine=self.engine, settings=self.settings)
                # 参数覆盖只作用在这一个实例上，不污染类属性
                for attr, value in (overrides or {}).get(cls.webhook_key, {}).items():
                    if attr in TUNING.get(cls.webhook_key, {}):
                        setattr(strategy, attr, value)
                picks = strategy.run()
                error = None
            except Exception as exc:  # 单个策略失败不影响其它
                logger.exception(f"{cls.__name__} 运行失败")
                picks, error = [], str(exc)
            results.append(
                {
                    "key": cls.webhook_key,
                    "name": cls.__name__,
                    "count": len(picks),
                    "elapsed_s": round(time.perf_counter() - t0, 2),
                    "rows": enrich_picks(snap, picks[: limit or None], names),
                    "symbols": picks,
                    "error": error,
                }
            )

        payload = {
            "took_s": round(time.perf_counter() - started, 2),
            "snapshot": {
                "symbols": snap.symbol_count,
                "rows": snap.row_count,
                "last_date": snap.market_last_date,
            },
            "results": results,
            "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        # 留痕（幂等）：同一天重复运行不会重复写入
        run_date = snap.market_last_date
        for item in results:
            if item["symbols"]:
                try:
                    self.ledger.record(run_date, item["key"], item["symbols"])
                except Exception as exc:
                    logger.warning(f"留痕失败（忽略）：{exc}")

        _LAST_RUN.clear()
        _LAST_RUN.update(payload)
        return payload

    def push(self, keys: list[str] | None) -> dict:
        """把**上一次选股**的结果推送到飞书（需显式 confirm）。"""
        if not _LAST_RUN:
            raise ValueError("还没有选股结果，请先运行一次选股")
        wanted = set(keys) if keys else None
        sent = []
        for item in _LAST_RUN["results"]:
            if wanted and item["key"] not in wanted:
                continue
            if not item["symbols"]:
                continue
            self.notifier.send(
                symbols=item["symbols"],
                strategy_name=item["name"],
                webhook_key=item["key"],
            )
            sent.append({"key": item["key"], "count": item["count"]})
        return {"sent": sent}


# ── HTTP 层 ──


class Handler(BaseHTTPRequestHandler):
    """极简路由：静态首页 + 几个 JSON 接口。"""

    app: App | None = None
    server_version = "SequoiaX/2.0"

    def log_message(self, fmt: str, *args) -> None:  # 降低噪音
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # 工具 ──
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, message: str, code: int = 400) -> None:
        self._json({"error": message}, code)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")) or {}
        except json.JSONDecodeError:
            return {}

    # 路由 ──
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path in ("/", "/index.html"):
                html = INDEX_HTML.read_bytes()
                self._send(200, html, "text/html; charset=utf-8")
                return
            if parsed.path == "/api/status":
                self._json(self.app.status())
                return
            if parsed.path == "/api/strategies":
                self._json({"strategies": strategy_meta()})
                return
            if parsed.path == "/api/kline":
                symbol = (query.get("symbol") or [""])[0].strip()
                if not symbol.isdigit() or len(symbol) != 6:
                    self._error("symbol 必须是 6 位数字代码")
                    return
                limit = min(max(int((query.get("limit") or ["120"])[0]), 20), 500)
                data = candles(self.app.settings.db_path, symbol, limit)
                if not data["bars"]:
                    self._error(f"{symbol} 没有行情数据", 404)
                    return
                data["name"] = self.app.directory.get_names([symbol]).get(symbol, "")
                self._json(data)
                return
            if parsed.path == "/api/params":
                self._json({"strategies": App.tuning_meta()})
                return
            if parsed.path == "/api/jobs":
                job_id = (query.get("id") or [""])[0]
                job = self.app.jobs.get(job_id)
                if job is None:
                    self._error("任务不存在", 404)
                    return
                job["tail"] = self.app.jobs.tail(job_id)
                self._json(job)
                return
            if parsed.path == "/api/ledger":
                days = min(max(int((query.get("days") or ["60"])[0]), 5), 365)
                strategy = (query.get("strategy") or [None])[0]
                self._json(self.app.ledger.report(days=days, strategy=strategy))
                return
            if parsed.path == "/api/backtest":
                if not BACKTEST_JSON.exists():
                    self._json({"available": False})
                    return
                self._json(
                    {"available": True, "data": json.loads(BACKTEST_JSON.read_text("utf-8"))}
                )
                return
            self._error("not found", 404)
        except Exception as exc:  # 任何异常都返回 JSON，便于前端显示
            logger.exception("GET 处理失败")
            self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        body = self._read_json()
        try:
            if parsed.path == "/api/select":
                keys = body.get("strategies") or None
                if keys:
                    unknown = [k for k in keys if k not in STRATEGIES]
                    if unknown:
                        self._error(f"未知策略：{unknown}")
                        return
                limit = body.get("limit")
                limit = int(limit) if limit else None
                overrides = body.get("overrides") or {}
                if overrides:
                    bad = [k for k in overrides if k not in STRATEGIES]
                    if bad:
                        self._error(f"未知策略：{bad}")
                        return
                with _RUN_LOCK:
                    self._json(self.app.select(keys, limit, overrides))
                return

            if parsed.path == "/api/backtest/start":
                try:
                    top = int(body.get("top") or 30)
                    capital = float(body.get("capital") or 10_000_000)
                except (TypeError, ValueError):
                    self._error("top/capital 必须是数字")
                    return
                argv = [
                    sys.executable,
                    "scripts/backtest.py",
                    "--horizons",
                    "1,5",
                    "--top",
                    str(top),
                    "--capital",
                    str(capital),
                    "--out",
                    "data/backtest_summary.json",
                ]
                if body.get("start"):
                    argv.extend(["--start", str(body["start"])])
                if body.get("train_end"):
                    argv.extend(["--train-end", str(body["train_end"])])
                self._json(self.app.jobs.start("回测", argv))
                return

            if parsed.path == "/api/push":
                if body.get("confirm") is not True:
                    self._error("推送需要显式 confirm=true")
                    return
                with _RUN_LOCK:
                    self._json(self.app.push(body.get("strategies") or None))
                return

            self._error("not found", 404)
        except ValueError as exc:
            self._error(str(exc), 400)
        except Exception as exc:
            logger.error("POST 处理失败\n%s", traceback.format_exc())
            self._error(f"{type(exc).__name__}: {exc}", 500)


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """启动 HTTP 服务（阻塞）。"""
    Handler.app = App()
    httpd = ThreadingHTTPServer((host, port), Handler)
    logger.info(f"控制台已启动：http://{host}:{port}  （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断，退出")
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X 本地网页控制台")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    args = parser.parse_args(argv)
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
