"""ZEEHO 自动签到运行编排。"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

from .config import ConfigError, load_settings
from .notification import send_notification
from .zeeho import (
    STEP_DELETE,
    STEP_LIKE,
    STEP_POST,
    STEP_SHARE,
    DailyTaskResult,
    SignInResult,
    TaskStep,
    ZeehoClient,
)

TITLE = "ZEEHO 自动签到"

# 每日任务步骤的展示名（除删帖外都按「<名称>成功：积分+X」渲染）
_STEP_LABELS = {
    STEP_POST: "发帖",
    STEP_LIKE: "喜欢",
    STEP_SHARE: "分享",
}


def describe_signin(result: SignInResult) -> str:
    """签到行：成功 / 今日已签到 / 失败。"""
    if not result.ok:
        return "❌ 签到失败：{}".format(result.error or "未知原因")
    line = "{} {}".format(
        "ℹ️" if result.already_signed else "✅",
        "今日已签到" if result.already_signed else "签到成功",
    )
    if result.gained_points:
        line += "：积分+{}".format(result.gained_points)
    if result.continue_days:
        line += "（已连签 {} 天）".format(result.continue_days)
    return line


def describe_task_step(step: TaskStep) -> str:
    """每日任务里的一行：成功 / 已完成（本次跳过）/ 失败。"""
    if not step.ok:
        if step.key == STEP_DELETE:
            return "❌ 删除任务帖失败：{}".format(step.detail or "未知原因")
        return "❌ {}失败：{}".format(
            _STEP_LABELS.get(step.key, step.key), step.detail or "未知原因"
        )

    if step.key == STEP_DELETE:
        line = "ℹ️ 无任务帖需要删除" if step.skipped else "✅ 已删除任务用帖子"
    else:
        label = _STEP_LABELS.get(step.key, step.key)
        line = "ℹ️ {}已完成".format(label) if step.skipped else "✅ {}成功".format(label)
    if step.points:
        line += "：积分+{}".format(step.points)
    if step.detail:
        line += "（{}）".format(step.detail)
    return line


def describe_daily_result(result: Optional[DailyTaskResult]) -> List[str]:
    """每日任务段落的所有行（未开启返回一行说明）。"""
    if result is None:
        return ["ℹ️ 已关闭（runtime.daily_tasks=false）"]
    if result.error:
        return ["❌ 每日任务未执行：{}".format(result.error)]
    return [describe_task_step(step) for step in result.steps]


def describe_total_score(signins: Sequence[SignInResult]) -> str:
    """末尾一行的可用积分汇总（只统计成功取到余额的账号）。"""
    scores = [
        result.available_score
        for result in signins
        if result.available_score is not None
    ]
    if not scores:
        return "可用积分：未取到"
    line = "可用积分：{}".format(sum(scores))
    missing = len(signins) - len(scores)
    if missing:
        line += "（另有 {} 个账号未取到）".format(missing)
    return line


def account_lines(
    index: int,
    name: str,
    signin: SignInResult,
    daily: Optional[DailyTaskResult],
) -> List[str]:
    """单个账号的报告块：标题 + 签到段 + 任务段。"""
    lines = ["[{}] {}：".format(index, name), "签到：", describe_signin(signin)]
    lines.append("任务：")
    lines.extend(describe_daily_result(daily))
    return lines


def format_results(
    names: Iterable[str],
    signins: Iterable[SignInResult],
    dailies: Iterable[Optional[DailyTaskResult]],
) -> Tuple[str, bool]:
    """汇总全部账号结果，返回 (推送文案, 是否全部成功)。"""
    name_list = list(names)
    signin_list = list(signins)
    daily_list = list(dailies)
    success_count = 0
    lines: List[str] = []
    for index, (name, signin, daily) in enumerate(
        zip(name_list, signin_list, daily_list), 1
    ):
        success_count += int(signin.ok and (daily is None or daily.ok))
        lines.extend(account_lines(index, name, signin, daily))
    content = "\n".join(
        [
            "共{}个账号，成功{}个，失败{}个".format(
                len(signin_list), success_count, len(signin_list) - success_count
            )
        ]
        + lines
        + [describe_total_score(signin_list)]
    )
    return content, success_count == len(signin_list)


def run() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print("❌ 配置错误：{}".format(exc))
        return 2

    print("🔔 {}，共发现 {} 个账号".format(TITLE, len(settings.accounts)))
    client = ZeehoClient(settings, log=print)
    names: List[str] = []
    signins: List[SignInResult] = []
    dailies: List[Optional[DailyTaskResult]] = []
    for index, account in enumerate(settings.accounts, 1):
        print("\n—— 账号 {}：{} ——".format(index, account.name))
        signin = client.sign_in(account)
        print("[签到] " + describe_signin(signin))
        daily = client.daily_tasks(account) if settings.daily_tasks else None
        for line in describe_daily_result(daily):
            print("[任务] " + line)
        names.append(account.name)
        signins.append(signin)
        dailies.append(daily)

    content, all_ok = format_results(names, signins, dailies)
    print("\n" + content)
    if settings.notify:
        send_notification(TITLE, content)
    else:
        print("ℹ️ ZEEHO_NOTIFY=false，已跳过推送")
    return 0 if all_ok else 1
