import contextlib
import io
import unittest
from unittest import mock

from modules.app import (
    describe_available_score,
    describe_blind_box,
    describe_daily_result,
    describe_signin,
    describe_total_score,
    final_score,
    format_results,
    run,
)
from modules.config import Account, Settings
from modules.zeeho import (
    STEP_DELETE,
    STEP_LIKE,
    STEP_POST,
    STEP_SHARE,
    DailyTaskResult,
    BlindBoxResult,
    SignInResult,
    TaskStep,
)

ACCOUNT = Account("20260101000000001", "Bearer token", "Higanoneko")


def make_settings(**overrides):
    values = dict(
        accounts=[ACCOUNT],
        app_id="app",
        app_secret="secret",
        base_url="https://example.test",
        user_agent="browser-agent",
        zeeho_user_agent="app-agent",
        timeout=15,
        use_proxy=False,
        notify=False,
        blind_box=False,
    )
    values.update(overrides)
    return Settings(**values)


def signin(
    ok=True,
    already=False,
    error="",
    gained=0,
    streak=0,
    score=None,
):
    return SignInResult(ACCOUNT, ok, already, error, "10000", gained, streak, score)


def full_day_task(delete=True, score=None):
    """一天正常跑完的四步任务链（score = 任务跑完后重新查到的余额）。"""
    return DailyTaskResult(
        ACCOUNT,
        (
            TaskStep(STEP_POST, True, points=1),
            TaskStep(STEP_LIKE, True, points=1),
            TaskStep(STEP_SHARE, True, points=1),
            TaskStep(STEP_DELETE, delete),
        ),
        available_score=score,
    )


class DescribeSigninTests(unittest.TestCase):
    def test_fresh_signin_line(self):
        self.assertEqual(
            describe_signin(signin(already=False, gained=1, streak=4)),
            "✅ 签到成功：积分+1（已连签 4 天）",
        )

    def test_already_signed_line(self):
        self.assertEqual(
            describe_signin(signin(already=True, gained=1, streak=4)),
            "ℹ️ 今日已签到：积分+1（已连签 4 天）",
        )

    def test_signin_line_without_known_points(self):
        self.assertEqual(describe_signin(signin(gained=0, streak=0)), "✅ 签到成功")

    def test_signin_failure_line(self):
        self.assertEqual(
            describe_signin(signin(ok=False, error="code=40000，message=登录态失效")),
            "❌ 签到失败：code=40000，message=登录态失效",
        )


class DescribeDailyResultTests(unittest.TestCase):
    def test_success_lines(self):
        lines = describe_daily_result(full_day_task())
        self.assertEqual(
            lines,
            [
                "✅ 发帖成功：积分+1",
                "✅ 喜欢成功：积分+1",
                "✅ 分享成功：积分+1",
                "✅ 已删除任务用帖子",
            ],
        )

    def test_already_done_lines_use_info_icon(self):
        daily = DailyTaskResult(
            ACCOUNT,
            (
                TaskStep(STEP_POST, True, True, 1),
                TaskStep(STEP_LIKE, True, True, 1),
                TaskStep(STEP_SHARE, True, True, 1),
                TaskStep(STEP_DELETE, True, True),
            ),
            skipped=True,
        )
        self.assertEqual(
            describe_daily_result(daily),
            [
                "ℹ️ 发帖已完成：积分+1",
                "ℹ️ 喜欢已完成：积分+1",
                "ℹ️ 分享已完成：积分+1",
                "ℹ️ 无任务帖需要删除",
            ],
        )

    def test_step_notes_are_rendered(self):
        daily = DailyTaskResult(
            ACCOUNT,
            (
                TaskStep(STEP_POST, True, True, 1, "复用今日已有帖"),
                TaskStep(STEP_DELETE, True, False, 0, "清理残留 1 篇"),
            ),
        )
        self.assertEqual(
            describe_daily_result(daily),
            [
                "ℹ️ 发帖已完成：积分+1（复用今日已有帖）",
                "✅ 已删除任务用帖子（清理残留 1 篇）",
            ],
        )

    def test_failure_lines(self):
        daily = DailyTaskResult(
            ACCOUNT,
            (
                TaskStep(STEP_POST, False, detail="code=40000，message=被禁言"),
                TaskStep(STEP_DELETE, False, detail="HTTP 500"),
            ),
        )
        self.assertEqual(
            describe_daily_result(daily),
            ["❌ 发帖失败：code=40000，message=被禁言", "❌ 删除任务帖失败：HTTP 500"],
        )

    def test_preparation_failure_line(self):
        daily = DailyTaskResult(ACCOUNT, error="每日任务-查询我的帖子失败：HTTP 500")
        self.assertEqual(
            describe_daily_result(daily),
            ["❌ 每日任务未执行：每日任务-查询我的帖子失败：HTTP 500"],
        )

    def test_disabled_line(self):
        self.assertEqual(
            describe_daily_result(None),
            ["ℹ️ 已关闭（runtime.daily_tasks=false）"],
        )


class TotalScoreTests(unittest.TestCase):
    def test_sums_known_scores(self):
        self.assertEqual(
            describe_total_score([208, 12]),
            "可用积分：220",
        )

    def test_notes_missing_accounts(self):
        self.assertEqual(
            describe_total_score([208, None]),
            "可用积分：208（另有 1 个账号未取到）",
        )

    def test_unknown_scores(self):
        self.assertEqual(describe_total_score([None]), "可用积分：未取到")

    def test_describe_available_score(self):
        self.assertEqual(describe_available_score(211), "可用积分：211")


class FinalScoreTests(unittest.TestCase):
    def test_prefers_balance_fetched_after_tasks(self):
        # 签到阶段查到 208（早于任务积分），任务后重查 211：应报 211
        self.assertEqual(
            final_score(signin(score=208), full_day_task(score=211)), 211
        )

    def test_falls_back_to_signin_balance(self):
        # 任务后那次查询失败 → 退回签到阶段的余额，而不是报「未取到」
        self.assertEqual(final_score(signin(score=208), full_day_task()), 208)

    def test_falls_back_when_daily_tasks_disabled(self):
        self.assertEqual(final_score(signin(score=208), None), 208)

    def test_none_when_no_balance_at_all(self):
        self.assertIsNone(final_score(signin(ok=False), full_day_task()))
        self.assertIsNone(final_score(signin(ok=False), None))


class FormatResultsTests(unittest.TestCase):
    def test_report_includes_blind_box_reward_before_daily_tasks(self):
        content, all_ok = format_results(
            ["A"], [signin(already=True, gained=1, streak=30, score=208)],
            [full_day_task(score=221)],
            [BlindBoxResult(True, sign_count=30, gained_points=10, prize_name="10积分")],
        )
        self.assertTrue(all_ok)
        self.assertEqual(content.splitlines(), [
            "共1个账号，成功1个，失败0个",
            "[1] A：", "签到：", "ℹ️ 今日已签到：积分+1（已连签 30 天）",
            "盲盒：", "✅ 盲盒领取成功：积分+10（10积分）",
            "任务：", "✅ 发帖成功：积分+1", "✅ 喜欢成功：积分+1",
            "✅ 分享成功：积分+1", "✅ 已删除任务用帖子", "可用积分：221",
        ])

    def test_report_matches_requested_layout(self):
        content, all_ok = format_results(
            ["Higanoneko"],
            [signin(gained=1, streak=4, score=208)],
            [full_day_task(score=211)],
        )

        self.assertTrue(all_ok)
        self.assertEqual(
            content,
            "\n".join(
                [
                    "共1个账号，成功1个，失败0个",
                    "[1] Higanoneko：",
                    "签到：",
                    "✅ 签到成功：积分+1（已连签 4 天）",
                    "盲盒：",
                    "ℹ️ 已关闭（runtime.blind_box=false）",
                    "任务：",
                    "✅ 发帖成功：积分+1",
                    "✅ 喜欢成功：积分+1",
                    "✅ 分享成功：积分+1",
                    "✅ 已删除任务用帖子",
                    "可用积分：211",
                ]
            ),
        )

    def test_failed_account_is_counted_and_keeps_its_block(self):
        content, all_ok = format_results(
            ["A", "B"],
            [signin(gained=1, score=10), signin(ok=False, error="HTTP 401 invalid_token")],
            [full_day_task(score=13), None],
        )

        self.assertFalse(all_ok)
        lines = content.splitlines()
        self.assertEqual(lines[0], "共2个账号，成功1个，失败1个")
        self.assertIn("[1] A：", lines)
        self.assertIn("[2] B：", lines)
        self.assertIn("❌ 签到失败：HTTP 401 invalid_token", lines)
        self.assertIn("ℹ️ 已关闭（runtime.daily_tasks=false）", lines)
        self.assertEqual(lines[-1], "可用积分：13（另有 1 个账号未取到）")

    def test_task_failure_marks_account_failed(self):
        daily = full_day_task(delete=False, score=30)
        content, all_ok = format_results(["A"], [signin(gained=1)], [daily])

        self.assertFalse(all_ok)
        self.assertIn("共1个账号，成功0个，失败1个", content)
        self.assertEqual(content.splitlines()[-1], "可用积分：30")


class RunTests(unittest.TestCase):
    def _run(self, settings, client):
        """跑一遍 run()，返回 (退出码, 控制台输出, 用的客户端替身)。"""
        out = io.StringIO()
        with mock.patch("modules.app.load_settings", return_value=settings), mock.patch(
            "modules.app.ZeehoClient", return_value=client
        ), mock.patch("modules.app.send_notification"), contextlib.redirect_stdout(out):
            code = run()
        return code, out.getvalue(), client

    def test_balance_is_fetched_after_daily_tasks(self):
        # 任务链跑完后必须再查一次余额：本次任务赚到的 3 分要算进报告合计
        client = mock.Mock()
        client.sign_in.return_value = signin(gained=1, score=208)
        client.daily_tasks.return_value = full_day_task()
        client.fetch_score.return_value = 211

        code, output, client = self._run(make_settings(), client)

        client.fetch_score.assert_called_once_with(ACCOUNT)
        self.assertEqual(code, 0)
        self.assertIn("[余额] 可用积分：211", output)
        self.assertEqual(output.splitlines()[-2], "可用积分：211")

    def test_balance_failure_falls_back_to_signin_score(self):
        client = mock.Mock()
        client.sign_in.return_value = signin(gained=1, score=208)
        client.daily_tasks.return_value = full_day_task()
        client.fetch_score.return_value = None

        code, output, _ = self._run(make_settings(), client)

        self.assertEqual(code, 0)
        self.assertNotIn("[余额]", output)
        self.assertEqual(output.splitlines()[-2], "可用积分：208")

    def test_daily_tasks_disabled_skips_extra_balance_query(self):
        client = mock.Mock()
        client.sign_in.return_value = signin(already=True, gained=1, score=208)

        code, output, client = self._run(make_settings(daily_tasks=False), client)

        client.daily_tasks.assert_not_called()
        client.fetch_score.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn("ℹ️ 已关闭（runtime.daily_tasks=false）", output)
        self.assertEqual(output.splitlines()[-2], "可用积分：208")

    def test_blind_box_runs_after_signin_before_tasks_and_balance(self):
        client = mock.Mock()
        client.sign_in.return_value = signin(already=True, score=208)
        client.claim_blind_box.return_value = BlindBoxResult(True, gained_points=10)
        client.daily_tasks.return_value = full_day_task()
        client.fetch_score.return_value = 221

        code, output, _ = self._run(make_settings(blind_box=True), client)

        self.assertEqual(code, 0)
        self.assertEqual(client.mock_calls, [
            mock.call.sign_in(ACCOUNT), mock.call.claim_blind_box(ACCOUNT),
            mock.call.daily_tasks(ACCOUNT), mock.call.fetch_score(ACCOUNT),
        ])
        self.assertIn("✅ 盲盒领取成功：积分+10", output)
        self.assertEqual(output.splitlines()[-2], "可用积分：221")

    def test_blind_box_refreshes_balance_without_daily_tasks(self):
        client = mock.Mock()
        client.sign_in.return_value = signin(score=208)
        client.claim_blind_box.return_value = BlindBoxResult(True, gained_points=10)
        client.fetch_score.return_value = 218
        code, output, _ = self._run(
            make_settings(blind_box=True, daily_tasks=False), client
        )
        self.assertEqual(code, 0)
        client.daily_tasks.assert_not_called()
        client.fetch_score.assert_called_once_with(ACCOUNT)
        self.assertEqual(output.splitlines()[-2], "可用积分：218")

    def test_locked_blind_box_does_not_refresh_balance_without_tasks(self):
        client = mock.Mock()
        client.sign_in.return_value = signin(score=208)
        client.claim_blind_box.return_value = BlindBoxResult(True, True, 29)
        code, output, _ = self._run(
            make_settings(blind_box=True, daily_tasks=False), client
        )
        self.assertEqual(code, 0)
        client.fetch_score.assert_not_called()
        self.assertIn("盲盒未解锁（29/30）", output)

    def test_blind_box_failure_keeps_daily_tasks_running(self):
        client = mock.Mock()
        client.sign_in.return_value = signin(score=208)
        client.claim_blind_box.return_value = BlindBoxResult(False, error="HTTP 401")
        client.daily_tasks.return_value = full_day_task()
        client.fetch_score.return_value = 211
        code, output, _ = self._run(make_settings(blind_box=True), client)
        self.assertEqual(code, 1)
        client.daily_tasks.assert_called_once_with(ACCOUNT)
        self.assertIn("❌ 盲盒领取失败：HTTP 401", output)
        self.assertEqual(output.splitlines()[-2], "可用积分：211")

    def test_failed_signin_skips_blind_box(self):
        client = mock.Mock()
        client.sign_in.return_value = signin(ok=False, error="HTTP 401")
        code, output, _ = self._run(
            make_settings(blind_box=True, daily_tasks=False), client
        )
        self.assertEqual(code, 1)
        client.claim_blind_box.assert_not_called()
        self.assertIn("签到未成功，已跳过盲盒领取", output)

    def test_disabled_blind_box_never_calls_claim(self):
        client = mock.Mock()
        client.sign_in.return_value = signin(score=208)
        self._run(make_settings(blind_box=False, daily_tasks=False), client)
        client.claim_blind_box.assert_not_called()

    def test_blind_box_balance_failure_preserves_known_score(self):
        client = mock.Mock()
        client.sign_in.return_value = signin(score=208)
        client.claim_blind_box.return_value = BlindBoxResult(True, gained_points=10)
        client.fetch_score.return_value = None
        code, output, _ = self._run(
            make_settings(blind_box=True, daily_tasks=False), client
        )
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines()[-2], "可用积分：208")


class DescribeBlindBoxTests(unittest.TestCase):
    def test_reward_and_prize(self):
        self.assertEqual(
            describe_blind_box(BlindBoxResult(True, gained_points=10, prize_name="10积分")),
            "✅ 盲盒领取成功：积分+10（10积分）",
        )

    def test_non_point_prize(self):
        self.assertEqual(
            describe_blind_box(BlindBoxResult(True, prize_name="补签卡")),
            "✅ 盲盒领取成功（补签卡）",
        )

    def test_blind_failure_is_in_report_and_marks_account_failed(self):
        content, all_ok = format_results(
            ["A"], [signin(score=10)], [None],
            [BlindBoxResult(False, error="code=40000")],
        )
        self.assertFalse(all_ok)
        self.assertIn("❌ 盲盒领取失败：code=40000", content)
        self.assertEqual(content.splitlines()[0], "共1个账号，成功0个，失败1个")


if __name__ == "__main__":
    unittest.main()
