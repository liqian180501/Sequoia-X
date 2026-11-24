"""运维告警：跑批失败、数据不新鲜时推一条消息出去。

为什么单独一个模块
------------------
选股推送是"给群友看股票"，告警是"给自己看故障"。两件事混在一条 webhook 里，
结果是故障被淹没在卡片流里。所以告警走独立的 ``ALERT_WEBHOOK_URL``：

* 配置了 → 推飞书；
* 没配置 → **只写日志**，绝不因为"告警发不出去"把主流程搞挂。

设计原则：**告警失败不能引发新的失败**。所有异常都被吞掉并记录。
"""

from __future__ import annotations

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

TIMEOUT = 10


def _card(title: str, lines: list[str], level: str) -> dict:
    """拼一张飞书文本卡片（不用 markdown 表格，手机上好读）。"""
    color = {"error": "red", "warning": "orange", "info": "blue"}.get(level, "grey")
    body = "\n".join(lines)
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color,
            },
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
        },
    }


def send_alert(
    settings: Settings,
    title: str,
    lines: list[str],
    level: str = "error",
) -> bool:
    """发一条告警。

    Args:
        settings: 配置（读 ``alert_webhook_url``）。
        title: 标题，例如"Sequoia-X 跑批失败"。
        lines: 正文，逐行拼成 markdown。
        level: ``error`` / ``warning`` / ``info``，决定卡片颜色。

    Returns:
        True 表示推送成功；未配置或发送失败都返回 False（但不会抛异常）。
    """
    url = (settings.alert_webhook_url or "").strip()
    if not url:
        logger.warning(f"[告警未配置 webhook] {title}: {'; '.join(lines)}")
        return False

    try:
        resp = requests.post(url, json=_card(title, lines, level), timeout=TIMEOUT)
        if resp.status_code != 200:
            logger.warning(f"告警推送失败：HTTP {resp.status_code}")
            return False
        payload = resp.json()
        if payload.get("code") not in (0, None) and payload.get("StatusCode") != 0:
            logger.warning(f"告警推送被拒：{payload}")
            return False
        logger.info(f"已推送告警：{title}")
        return True
    except Exception as exc:  # 告警失败不能引发新异常
        logger.warning(f"告警推送异常（忽略）：{type(exc).__name__}: {exc}")
        return False
