"""每日跑批入口：健康检查 → 选股推送 → 失败告警。

为什么需要一层包装
------------------
``main.py`` 只管"选股 + 推送"。但定时任务真正需要的是：

1. **先确认数据能用**（否则推送的是过期数据）；
2. **失败要有人知道**（定时任务静默失败是最危险的）；
3. **退出码要有意义**（任务计划程序靠它判断成败）。

所以把这三件事包在一层里，定时任务指向本脚本，而不是 ``main.py``。

用法
----
```bash
python scripts/run_daily.py                 # 健康检查 + 同步 + 选股 + 推送
python scripts/run_daily.py --dry-run       # 不推送
python scripts/run_daily.py --no-sync       # 不联网同步，只用本地数据
python scripts/run_daily.py --skip-health   # 跳过健康检查
```

返回码：0=成功；1=失败；2=部分成功（沿用 main.py 的约定）。
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sequoia_x.core.config import get_settings  # noqa: E402
from sequoia_x.core.logger import get_logger  # noqa: E402
from sequoia_x.monitor.health import check  # noqa: E402
from sequoia_x.notify.alert import send_alert  # noqa: E402

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每日跑批：健康检查 + 选股 + 告警")
    parser.add_argument("--dry-run", action="store_true", help="只选股，不推送")
    parser.add_argument("--no-sync", action="store_true", help="跳过联网同步")
    parser.add_argument("--skip-health", action="store_true", help="跳过健康检查")
    parser.add_argument("--backend", default=None, help="同步数据源（传给 main.py）")
    args = parser.parse_args(argv)

    settings = get_settings()

    # ① 健康检查：数据不可用就不要推送
    if not args.skip_health:
        health = check(settings.db_path, max_lag_days=settings.max_data_lag_days)
        stats = health["stats"]
        logger.info(
            f"健康检查：最新 {stats.get('last_date')}，落后 {stats.get('lag_days')} 天，"
            f"覆盖 {stats.get('coverage', 0):.1%}"
        )
        if not health["ok"]:
            detail = "\n".join(f"- {issue}" for issue in health["issues"])
            logger.error(f"数据不健康，终止本次跑批：\n{detail}")
            if not args.dry_run:
                send_alert(
                    settings,
                    "Sequoia-X 跑批中止：数据不健康",
                    health["issues"]
                    + ["", "本次未推送选股结果。", f"最新数据：{stats.get('last_date')}"],
                    level="error",
                )
            return 1

    # ② 选股 + 推送：交给 main.py，保留它的退出码语义
    cli_args: list[str] = []
    if args.dry_run:
        cli_args.append("--dry-run")
    else:
        cli_args.append("--push-now")
    if args.no_sync:
        cli_args.append("--no-sync")
    if args.backend:
        cli_args.extend(["--backend", args.backend])

    try:
        from main import main as cli_main

        code = cli_main(cli_args)
    except SystemExit as exc:  # main.py 用 sys.exit 也未尝不可
        code = int(exc.code or 0)
    except Exception as exc:
        logger.error(f"跑批异常：{type(exc).__name__}: {exc}")
        logger.error(traceback.format_exc())
        if not args.dry_run:
            send_alert(
                settings,
                "Sequoia-X 跑批异常",
                [f"`{type(exc).__name__}`: {exc}", "", "详见日志"],
                level="error",
            )
        return 1

    # ③ 结果告警：部分成功也要说一声
    if code == 0:
        logger.info("跑批完成")
    elif code == 1:
        logger.error("跑批失败")
        if not args.dry_run:
            send_alert(settings, "Sequoia-X 跑批失败", ["退出码 1，详见日志"], level="error")
    else:
        logger.warning(f"跑批部分成功（退出码 {code}）")
        if not args.dry_run:
            send_alert(
                settings,
                "Sequoia-X 跑批部分成功",
                [f"退出码 {code}：部分策略失败或有股票未同步", "", "详见日志"],
                level="warning",
            )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
