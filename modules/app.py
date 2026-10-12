"""ZEEHO 自动签到运行编排。"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable, List, Optional, Sequence, Tuple

from .config import ConfigError, load_settings
from .notification import send_notification
from .zeeho import (
    BLIND_BOX_THRESHOLD,
    STEP_DELETE,
    STEP_LIKE,
    STEP_POST,
    STEP_SHARE,
    DailyTaskResult,
    BlindBoxResult,
    SessionKeepaliveResult,
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


def describe_session_keepalive(result: Optional[SessionKeepaliveResult]) -> str:
    """保活为辅助操作，失败只提示，不阻断签到或每日任务。"""
    if result is None:
        return "ℹ️ 已关闭（runtime.session_keepalive=false）"
    if result.ok:
        return "✅ 会话保活成功"
    if result.auth_expired:
        return "⚠️ 会话保活被拒绝：{}，请重新抓包更新 authorization".format(result.error)
    return "⚠️ 会话保活失败：{}，继续执行签到和任务".format(result.error or "未知原因")


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


def describe_blind_box(
    result: Optional[BlindBoxResult], signin_ok: bool = True
) -> str:
    """盲盒行：未执行、未解锁、领取成功或失败。"""
    if not signin_ok:
        return "ℹ️ 签到未成功，已跳过盲盒领取"
    if result is None:
        return "ℹ️ 已关闭（runtime.blind_box=false）"
    if not result.ok:
        return "❌ 盲盒领取失败：{}".format(result.error or "未知原因")
    if result.skipped:
        return "ℹ️ 盲盒未解锁（{}/{}）".format(result.sign_count, BLIND_BOX_THRESHOLD)
    line = "✅ 盲盒领取成功"
    if result.gained_points:
        line += "：积分+{}".format(result.gained_points)
    if result.prize_name:
        line += "（{}）".format(result.prize_name)
    return line


def describe_daily_result(result: Optional[DailyTaskResult]) -> List[str]:
    """每日任务段落的所有行（未开启返回一行说明）。"""
    if result is None:
        return ["ℹ️ 已关闭（runtime.daily_tasks=false）"]
    if result.error:
        return ["❌ 每日任务未执行：{}".format(result.error)]
    return [describe_task_step(step) for step in result.steps]


def describe_available_score(score: int) -> str:
    """领取/任务结束后重新查到的余额（日志里的 `[余额]` 行）。"""
    return "可用积分：{}".format(score)


def final_score(
    signin: SignInResult, daily: Optional[DailyTaskResult]
) -> Optional[int]:
    """账号最终的可用积分余额。

    优先用每日任务跑完后重新查到的余额（`daily.available_score`）；没有它时
    退回 `signin.available_score`。关闭每日任务但执行盲盒领取时，编排层会把
    领取后的真实余额回填到 signin；重查失败时保留原余额。
    """
    if daily is not None and daily.available_score is not None:
        return daily.available_score
    return signin.available_score


def describe_total_score(scores: Sequence[Optional[int]]) -> str:
    """末尾一行的可用积分汇总（只统计取到余额的账号）。"""
    known = [score for score in scores if score is not None]
    if not known:
        return "可用积分：未取到"
    line = "可用积分：{}".format(sum(known))
    missing = len(scores) - len(known)
    if missing:
        line += "（另有 {} 个账号未取到）".format(missing)
    return line


def account_lines(
    index: int,
    name: str,
    signin: SignInResult,
    daily: Optional[DailyTaskResult],
    blind: Optional[BlindBoxResult] = None,
    keepalive: Optional[SessionKeepaliveResult] = None,
) -> List[str]:
    """单个账号的报告块：标题 + 可选会话段 + 签到段 + 盲盒段 + 任务段。"""
    lines = ["[{}] {}：".format(index, name)]
    if keepalive is not None:
        lines.extend(["会话：", describe_session_keepalive(keepalive)])
    lines.extend(["签到：", describe_signin(signin)])
    lines.extend(["盲盒：", describe_blind_box(blind, signin.ok)])
    lines.append("任务：")
    lines.extend(describe_daily_result(daily))
    return lines


def format_results(
    names: Iterable[str],
    signins: Iterable[SignInResult],
    dailies: Iterable[Optional[DailyTaskResult]],
    blinds: Optional[Iterable[Optional[BlindBoxResult]]] = None,
    keepalives: Optional[Iterable[Optional[SessionKeepaliveResult]]] = None,
) -> Tuple[str, bool]:
    """汇总全部账号结果，返回 (推送文案, 是否全部成功)。"""
    name_list = list(names)
    signin_list = list(signins)
    daily_list = list(dailies)
    blind_list = list(blinds) if blinds is not None else [None] * len(signin_list)
    keepalive_list = (
        list(keepalives) if keepalives is not None else [None] * len(signin_list)
    )
    success_count = 0
    lines: List[str] = []
    for index, (name, signin, daily, blind, keepalive) in enumerate(
        zip(name_list, signin_list, daily_list, blind_list, keepalive_list), 1
    ):
        success_count += int(
            signin.ok and (blind is None or blind.ok) and (daily is None or daily.ok)
        )
        lines.extend(account_lines(index, name, signin, daily, blind, keepalive))
    content = "\n".join(
        [
            "共{}个账号，成功{}个，失败{}个".format(
                len(signin_list), success_count, len(signin_list) - success_count
            )
        ]
        + lines
        + [
            describe_total_score(
                [
                    final_score(signin, daily)
                    for signin, daily in zip(signin_list, daily_list)
                ]
            )
        ]
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
    blinds: List[Optional[BlindBoxResult]] = []
    keepalives: List[Optional[SessionKeepaliveResult]] = []
    for index, account in enumerate(settings.accounts, 1):
        print("\n—— 账号 {}：{} ——".format(index, account.name))
        keepalive = (
            client.keep_session_active(account) if settings.session_keepalive else None
        )
        print("[会话] " + describe_session_keepalive(keepalive))
        signin = client.sign_in(account)
        print("[签到] " + describe_signin(signin))
        blind = (
            client.claim_blind_box(account) if settings.blind_box and signin.ok else None
        )
        print("[盲盒] " + describe_blind_box(blind, signin.ok))
        daily = client.daily_tasks(account) if settings.daily_tasks else None
        for line in describe_daily_result(daily):
            print("[任务] " + line)
        # 任务或盲盒领取之后重查真实余额；超时可能已经开奖，也需要重查。
        if daily is not None or (blind is not None and not blind.skipped):
            score = client.fetch_score(account)
            if daily is not None:
                daily = replace(daily, available_score=score)
            elif score is not None:
                signin = replace(signin, available_score=score)
            if score is not None:
                print("[余额] " + describe_available_score(score))
        names.append(account.name)
        signins.append(signin)
        dailies.append(daily)
        blinds.append(blind)
        keepalives.append(keepalive)

    content, all_ok = format_results(names, signins, dailies, blinds, keepalives)
    print("\n" + content)
    if settings.notify:
        send_notification(TITLE, content)
    else:
        print("ℹ️ ZEEHO_NOTIFY=false，已跳过推送")
    return 0 if all_ok else 1
