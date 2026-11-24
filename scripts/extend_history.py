"""等 baostock 恢复后自动向前补齐历史（一次性运维脚本）。

为什么需要它
------------
baostock 会整段时间不可用（登录直接返回 ``10002007 网络接收错误``）。手动重跑要么
空转、要么错过恢复的时刻。这个脚本做的事很简单：

1. 调一次 ``DataEngine.extend_history()``（一轮，有预算）；
2. 如果这一轮**有进展**，立刻继续下一轮；
3. 如果**没有进展**，说明服务端大概率不可用，等 ``--probe-interval`` 秒再试；
4. 直到 ``extend_history`` 返回 -1（没有待补任务）或超过 ``--max-hours``。

它自己不实现重试细节——续传、幂等、预算、退避都在 ``DataEngine`` 里，
这里只负责"等待 + 循环"。

用法
----
```bash
python scripts/extend_history.py --target 2015-01-01 --max-hours 6
python scripts/extend_history.py --target 2010-01-01 --workers 6 --probe-interval 120
```
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sequoia_x.core.config import get_settings  # noqa: E402
from sequoia_x.core.logger import get_logger  # noqa: E402
from sequoia_x.data.engine import DataEngine  # noqa: E402

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="等 baostock 恢复后自动向前补齐历史")
    parser.add_argument("--target", default="2015-01-01", help="目标起点 YYYY-MM-DD")
    parser.add_argument("--max-hours", type=float, default=6.0, help="最多守多久")
    parser.add_argument("--probe-interval", type=float, default=300.0, help="无进展时的等待秒数")
    parser.add_argument("--timeout", type=float, default=420.0, help="每轮时间预算")
    parser.add_argument("--workers", type=int, default=4, help="并发进程数")
    args = parser.parse_args(argv)

    socket.setdefaulttimeout(10.0)  # baostock 是裸 socket，没有全局超时会挂死

    settings = get_settings()
    engine = DataEngine(settings)
    deadline = time.monotonic() + args.max_hours * 3600
    rounds = 0
    idle = 0

    logger.info(
        f"开始补齐：目标 {args.target}，最多守 {args.max_hours:.1f}h，"
        f"无进展时每 {args.probe_interval:.0f}s 重试"
    )

    while time.monotonic() < deadline:
        rounds += 1
        written = engine.extend_history(
            args.target, max_seconds=args.timeout, workers=args.workers
        )
        if written < 0:
            logger.info(f"补齐完成：没有待补任务（共 {rounds} 轮）")
            return 0
        if written > 0:
            idle = 0
            logger.info(f"第 {rounds} 轮写入 {written} 行，继续")
            continue

        idle += 1
        remaining = int(deadline - time.monotonic())
        logger.warning(
            f"第 {rounds} 轮没有进展（baostock 可能不可用），"
            f"{args.probe_interval:.0f}s 后重试；剩余预算 {remaining // 60} 分钟"
        )
        time.sleep(min(args.probe_interval, max(0, deadline - time.monotonic())))

    logger.error(f"达到时间上限（{args.max_hours:.1f}h）仍未补齐，共 {rounds} 轮；重跑即可续传")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
