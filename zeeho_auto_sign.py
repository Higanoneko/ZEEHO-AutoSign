#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 青龙面板任务配置
# new Env('ZEEHO 自动签到')
# cron 0 8 * * * gamemale_daily_ql.py
"""
ZEEHO 自动签到的青龙任务入口。
"""

import sys

from modules.app import run


def _configure_output() -> None:
    """让青龙日志和 Windows 本地重定向都稳定输出 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _configure_output()
    raise SystemExit(run())
