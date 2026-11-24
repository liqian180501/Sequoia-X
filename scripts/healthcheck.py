"""数据健康检查命令行：跑批前先确认数据能用。

用法
----
```bash
python scripts/healthcheck.py                 # 只打印，返回码 0/1
python scripts/healthcheck.py --alert         # 不健康时推飞书告警
python scripts/healthcheck.py --json          # 机器可读
```

返回码：0=健康，1=不健康（可直接用于定时任务/CI 的判据）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sequoia_x.core.config import get_settings  # noqa: E402
from sequoia_x.core.logger import get_logger  # noqa: E402
from sequoia_x.monitor.health import check  # noqa: E402
from sequoia_x.notify.alert import send_alert  # noqa: E402

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据健康检查")
    parser.add_argument("--alert", action="store_true", help="不健康时推送告警")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--max-lag", type=int, default=None, help="覆盖允许落后的天数")
    args = parser.parse_args(argv)

    settings = get_settings()
    result = check(
        settings.db_path,
        max_lag_days=(
            settings.max_data_lag_days if args.max_lag is None else args.max_lag
        ),
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        stats = result["stats"]
        print(
            f"行数 {stats.get('rows', 0):,} · 股票 {stats.get('symbols', 0):,} · "
            f"最新 {stats.get('last_date')}（落后 {stats.get('lag_days', '?')} 天）· "
            f"覆盖 {stats.get('coverage', 0):.1%} · 重复 {stats.get('duplicates', 0)}"
        )
        if result["ok"]:
            print("✅ 数据健康")
        else:
            print("❌ 数据不健康：")
            for issue in result["issues"]:
                print(f"   - {issue}")

    if not result["ok"] and args.alert:
        lines = result["issues"] + [
            "",
            f"数据库：`{settings.db_path}`",
            f"最新日期：{result['stats'].get('last_date')}",
        ]
        send_alert(settings, "Sequoia-X 数据健康检查未通过", lines, level="error")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
