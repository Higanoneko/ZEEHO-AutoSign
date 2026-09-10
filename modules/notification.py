"""青龙原生 notify.py 的窄适配层。"""

from __future__ import annotations


def send_notification(title: str, content: str) -> None:
    try:
        from notify import send
    except ImportError:
        print("[通知] 当前环境没有青龙原生 notify.py，已跳过推送")
        return

    try:
        send(title, content)
    except Exception as exc:
        print("[通知] 青龙通知发送失败：{}".format(exc))
