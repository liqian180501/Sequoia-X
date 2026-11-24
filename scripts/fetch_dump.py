"""从同花顺 "Market Dumps" 下载全市场 Parquet（日K / 复权因子）。

为什么用它
----------
逐只拉历史（baostock 或 REST 单标的接口）要几千次请求、几小时，而且容易被限流/拉黑。
Market Dumps 是**一次签名 + 一次下载**拿到全市场 10 年数据：

| dump | 内容 | 规模 |
|---|---|---|
| ``daily-k`` | 全市场约 10 年日 K（**未复权**） | ~945 万行 |
| ``daily-k-10d`` | 全市场最近 10 个交易日 | ~25 万行 |
| ``adjustment-factors`` | 全市场复权事件（分红/送股/配股） | ~5.2 万行 |

用法
----
```bash
python scripts/fetch_dump.py --kind daily-k --out-dir data/dumps
python scripts/fetch_dump.py --kind daily-k-10d
python scripts/fetch_dump.py --kind daily-k --probe      # 只看大小，不下载
```

凭据
----
从 ``HITHINK_FINANCE_API_KEY`` 环境变量或
``%APPDATA%\\hithink-finance\\credentials.env`` 读取；Key 与预签名 URL 都只在进程内使用，
不打印、不落盘。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests

KINDS = {
    "daily-k": "全市场 10 年日K（未复权）",
    "daily-k-10d": "全市场最近 10 交易日",
    "adjustment-factors": "全市场复权事件",
}

BASE = "https://fuyao.aicubes.cn"
ENDPOINT = BASE + "/api/dump/market-dumps/{kind}/download-url"
FILENAMES = {
    "daily-k": "daily_k_full.parquet",
    "daily-k-10d": "daily_k_10d.parquet",
    "adjustment-factors": "adjustment_factors.parquet",
}
TIMEOUT = 60


def load_api_key() -> str:
    """按统一来源顺序取 API Key（环境变量 → 用户级 credentials.env）。"""
    key = os.environ.get("HITHINK_FINANCE_API_KEY", "").strip()
    if key:
        return key

    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "hithink-finance" / "credentials.env")
    candidates.append(Path.home() / ".config" / "hithink-finance" / "credentials.env")

    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() in ("HITHINK_FINANCE_API_KEY", "FUYAO_TOKEN", "API_KEY"):
                if value.strip():
                    return value.strip().strip('"').strip("'")
    raise SystemExit(
        "未找到 API Key：请设置 HITHINK_FINANCE_API_KEY，或在 "
        "%APPDATA%\\hithink-finance\\credentials.env 中配置"
    )


def sign_url(kind: str, key: str) -> str:
    """调签名端点拿到预签名下载地址（只在进程内使用）。"""
    resp = requests.get(ENDPOINT.format(kind=kind), headers={"X-api-key": key}, timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0:
        raise SystemExit(f"签名失败：code={payload.get('code')} {payload.get('message')}")
    url = (payload.get("data") or {}).get("presigned_url")
    if not url:
        raise SystemExit("签名响应里没有 presigned_url")
    return url


def _safe_error(exc: Exception) -> str:
    """脱敏错误信息：预签名 URL 是短期凭据，不能出现在日志/输出里。"""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def probe(url: str) -> None:
    """只探测文件大小（预签名 URL 通常只授权 GET，HEAD 会 403）。"""
    try:
        with requests.get(url, stream=True, timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            size = int(resp.headers.get("Content-Length") or 0)
    except Exception as exc:  # noqa: BLE001 统一脱敏
        raise SystemExit(f"探测失败：{_safe_error(exc)}") from None
    print(f"文件大小：{size / 1024 / 1024:.1f} MB")


def download(url: str, dest: Path) -> None:
    """流式下载到本地（大文件不占内存）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    written = 0
    try:
        resp = requests.get(url, stream=True, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 统一脱敏
        raise SystemExit(f"下载失败：{_safe_error(exc)}") from None
    with resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    fh.write(chunk)
                    written += len(chunk)
                    if total:
                        pct = written * 100 // total
                        if pct % 10 == 0:
                            print(f"\r下载中 {pct:3d}% ({written / 1024 / 1024:.0f} MB)", end="")
    tmp.replace(dest)
    print(f"\r已保存 {dest}（{written / 1024 / 1024:.1f} MB）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载同花顺全市场 Parquet dump")
    parser.add_argument("--kind", choices=sorted(KINDS), default="daily-k")
    parser.add_argument("--out-dir", default="data/dumps")
    parser.add_argument("--probe", action="store_true", help="只探测大小，不下载")
    args = parser.parse_args(argv)

    key = load_api_key()
    print(f"dump: {args.kind} —— {KINDS[args.kind]}")
    url = sign_url(args.kind, key)
    if args.probe:
        probe(url)
        return 0

    dest = Path(args.out_dir) / FILENAMES[args.kind]
    download(url, dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
