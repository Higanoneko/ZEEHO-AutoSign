import hashlib
import json
import unittest
from datetime import date as RealDate
from unittest import mock
from unittest.mock import Mock

import requests

from modules.config import Account, Settings
from modules.zeeho import (
    BLIND_BOX_PATH,
    blind_box_reward,
    blind_box_sign_count,
    STEP_DELETE,
    STEP_LIKE,
    STEP_POST,
    STEP_SHARE,
    TAPI_SIGNIN_COUNT_PATH,
    ZeehoClient,
    TAPI_ADJUST_SHARE_PATH,
    TAPI_CREATE_ARTICLE_PATH,
    TAPI_DELETE_ARTICLE_PATH,
    TAPI_INTEGRAL_PATH,
    TAPI_LIKE_PATH,
    TAPI_MY_ARTICLES_PATH,
    TAPI_SHARE_PATH,
    SigninCountState,
    build_like_body,
    build_publish_body,
    classify_task_articles,
    count_previous_month_tail,
    count_streak,
    create_signature,
    daily_task_rules,
    signin_count_state,
    signin_payload_points,
    stage_error_detail,
    task_post_content,
)

FIXED_TODAY = RealDate(2026, 9, 15)
FIXED_MONTH = "2026-09"


class FakeDate(RealDate):
    """把 date.today() 固定到月中，保证连签用例与跨月逻辑都可确定。"""

    @classmethod
    def today(cls):
        return FIXED_TODAY


def make_settings():
    return Settings(
        accounts=[],
        app_id="test-app",
        app_secret="test-secret",
        base_url="https://example.test",
        user_agent="browser-agent",
        zeeho_user_agent="app-agent",
        timeout=15,
        use_proxy=False,
        notify=False,
    )


def response(status, payload):
    value = requests.Response()
    value.status_code = status
    value.url = "https://example.test/test"
    value.headers["Content-Type"] = "application/json"
    value._content = json.dumps(payload).encode("utf-8")
    return value


def signed_entry(day, score=1, status="3"):
    return {
        "createDate": "2026-09-{:02d}".format(day),
        "signStatue": status,
        "integralScore": score,
    }


def count_response(count, signed_today):
    """App 端 signin/count 的响应（count = 连签天数，signStatus = 1 表示今天已签）。"""
    return response(
        200,
        {
            "code": "10000",
            "message": "操作成功",
            "data": {"count": count, "signStatus": 1 if signed_today else 0},
        },
    )


class BlindBoxTests(unittest.TestCase):
    account = Account("1", "Bearer test-token", "test")

    def claim(self, session):
        with mock.patch("modules.zeeho.date", FakeDate):
            return ZeehoClient(
                make_settings(), session=session, clock_ms=lambda: 123
            ).claim_blind_box(self.account)

    def test_sign_count_is_independent_of_streak(self):
        for raw, expected in [(0, 0), (29, 29), (30, 30), ("60", 60)]:
            with self.subTest(raw=raw):
                self.assertEqual(blind_box_sign_count({"data": {"signCount": raw}}), expected)
        for raw in [None, True, -1, "bad", 30.5, [], {}]:
            with self.subTest(raw=raw):
                self.assertIsNone(blind_box_sign_count({"data": {"signCount": raw}}))
        self.assertIsNone(blind_box_sign_count({"data": {"count": 60}}))
        self.assertIsNone(blind_box_sign_count(None))

    def test_reward_field_variants_and_non_point_prize(self):
        for data, expected in [
            ({"integral": "10", "prizesName": "10积分"}, (10, "10积分")),
            ({"integralScore": 20}, (20, "")),
            ({"integral": 0, "integralScore": 5}, (5, "")),
            ({"prizesName": "补签卡"}, (0, "补签卡")),
            ({"integral": "bad"}, (0, "")),
        ]:
            with self.subTest(data=data):
                self.assertEqual(blind_box_reward({"data": data}), expected)

    def test_unlocked_claim_uses_signed_get_with_current_date(self):
        session = Mock()
        session.get.side_effect = [
            response(200, {"code": "10000", "data": {"signCount": 30}}),
            response(200, {"code": 10000, "data": {"integral": 10, "prizesName": "10积分"}}),
        ]
        result = self.claim(session)
        self.assertTrue(result.ok)
        self.assertFalse(result.skipped)
        self.assertEqual((result.sign_count, result.gained_points, result.prize_name), (30, 10, "10积分"))
        sent = session.get.call_args
        self.assertEqual(sent.args, ("https://example.test" + BLIND_BOX_PATH,))
        self.assertEqual(sent.kwargs["params"], {"supplementDate": "2026-09-15"})
        self.assertNotIn("data", sent.kwargs)
        self.assertEqual(sent.kwargs["timeout"], 15)
        headers = sent.kwargs["headers"]
        self.assertEqual(headers["Authorization"], self.account.authorization)
        self.assertNotIn("Cookie", headers)
        raw = "supplementDate=2026-09-15" + headers["Cfmoto-X-Param"] + "test-secret"
        expected = hashlib.md5(hashlib.sha1(raw.encode()).hexdigest().encode()).hexdigest()
        self.assertEqual(headers["Cfmoto-X-Sign"], expected)
        self.assertIn("timestamp=123", headers["Cfmoto-X-Param"])
        self.assertEqual(session.get.call_count, 2)

    def test_locked_progress_never_calls_claim_endpoint(self):
        for count in [0, 29]:
            with self.subTest(count=count):
                session = Mock()
                session.get.return_value = response(200, {"code": "10000", "data": {"signCount": count}})
                result = self.claim(session)
                self.assertTrue(result.ok)
                self.assertTrue(result.skipped)
                session.get.assert_called_once()
                self.assertTrue(session.get.call_args.args[0].endswith("/signin/info"))

    def test_missing_progress_fails_without_claiming(self):
        session = Mock()
        session.get.return_value = response(200, {"code": "10000", "data": {"count": 60}})
        result = self.claim(session)
        self.assertFalse(result.ok)
        self.assertIn("signCount", result.error)
        session.get.assert_called_once()

    def test_precheck_failure_never_claims(self):
        for reply in [
            response(401, {"code": "invalid_token"}),
            response(200, {"code": "40000", "message": "未登录"}),
            response(200, []),
        ]:
            with self.subTest(reply=reply):
                session = Mock()
                session.get.return_value = reply
                result = self.claim(session)
                self.assertFalse(result.ok)
                self.assertTrue(result.error)
                session.get.assert_called_once()

    def test_claim_failure_or_timeout_does_not_retry_or_award_points(self):
        for reply in [
            response(200, {"code": "40000", "message": "无盲盒"}),
            response(200, {"code": "repeatedly_operation", "message": "操作过快"}),
            response(430, {"code": "30121", "message": "签名被拒"}),
            response(200, []),
            requests.Timeout("timeout"),
        ]:
            with self.subTest(reply=reply):
                session = Mock()
                session.get.side_effect = [
                    response(200, {"code": "10000", "data": {"signCount": 30}}), reply
                ]
                result = self.claim(session)
                self.assertFalse(result.ok)
                self.assertEqual(result.gained_points, 0)
                self.assertTrue(result.error)
                self.assertEqual(session.get.call_count, 2)

    def test_repeated_run_rechecks_server_progress(self):
        session = Mock()
        session.get.side_effect = [
            response(200, {"code": "10000", "data": {"signCount": 30}}),
            response(200, {"code": "10000", "data": {"integralScore": 10}}),
            response(200, {"code": "10000", "data": {"signCount": 0}}),
        ]
        self.assertEqual(self.claim(session).gained_points, 10)
        self.assertTrue(self.claim(session).skipped)
        self.assertEqual(session.get.call_count, 3)


class SignatureTests(unittest.TestCase):
    def test_signature_matches_reference_algorithm(self):
        result = create_signature(
            "app", "secret", timestamp_ms=123, nonce="fixed-nonce"
        )
        raw = "appId=app&nonce=fixed-nonce&timestamp=123secret"
        expected = hashlib.md5(hashlib.sha1(raw.encode()).hexdigest().encode()).hexdigest()

        self.assertEqual(result["Cfmoto-X-Sign"], expected)
        self.assertEqual(
            result["Cfmoto-X-Param"], "appId=app&nonce=fixed-nonce&timestamp=123"
        )

    def test_http_430_keeps_server_error_message(self):
        raw = requests.Response()
        raw.status_code = 430
        raw.url = "https://example.test/cfmotoservermine/signin"
        raw.headers["Content-Type"] = "application/json"
        raw._content = b'{"code":"430","message":"permit error"}'
        session = Mock()
        session.get.return_value = raw
        session.request.return_value = count_response(0, False)

        result = ZeehoClient(make_settings(), session=session).sign_in(
            Account("1", "Bearer token", "account")
        )

        self.assertFalse(result.ok)
        self.assertIn("permit error", result.error)
        sent_headers = session.get.call_args.kwargs["headers"]
        self.assertEqual(sent_headers["User-Agent"], "browser-agent")
        self.assertEqual(sent_headers["Zeeho-User-Agent"], "app-agent")
        self.assertEqual(session.get.call_count, 1)

    def test_count_streak_stops_at_first_missed_day(self):
        entries = [
            {"createDate": "2026-09-{:02d}".format(d), "signStatue": 3}
            for d in range(1, 16)
        ]
        entries[9]["signStatue"] = 4  # 9月10日断签

        streak, crosses = count_streak(entries, "2026-09-15", FIXED_MONTH)

        self.assertEqual(streak, 5)  # 9-11..9-15
        self.assertFalse(crosses)

    def test_count_streak_flags_month_start_crossing(self):
        entries = [signed_entry(d) for d in range(1, 16)]

        streak, crosses = count_streak(entries, "2026-09-15", FIXED_MONTH)

        self.assertEqual(streak, 15)
        self.assertTrue(crosses)

    def test_count_streak_returns_zero_when_today_unsigned(self):
        entries = [signed_entry(d) for d in range(1, 15)]
        entries.append({"createDate": "2026-09-15", "signStatue": 4})

        streak, crosses = count_streak(entries, "2026-09-15", FIXED_MONTH)

        self.assertEqual(streak, 0)
        self.assertFalse(crosses)

    def test_previous_month_tail_counts_from_month_end(self):
        entries = [signed_entry(d) for d in range(1, 16)]
        tail = count_previous_month_tail(entries)
        self.assertEqual(tail, 15)

        entries[13]["signStatue"] = 4  # 9-14 断签
        self.assertEqual(count_previous_month_tail(entries), 1)

    def test_sign_in_uses_precheck_empty_post_and_confirmation(self):
        precheck = response(
            200, {"code": "10000", "message": "操作成功", "data": {"nowSignDetailVos": []}}
        )
        signed = response(
            200, {"code": "10000", "message": "操作成功", "data": {}}
        )
        confirmation = response(
            200,
            {
                "code": "10000",
                "message": "操作成功",
                "data": {
                    "nowSignDetailVos": [
                        {"createDate": FIXED_TODAY.isoformat(), "signStatue": 3, "integralScore": 5}
                    ]
                },
            },
        )
        score = response(
            200,
            {"code": "10000", "message": "操作成功", "data": {"score": 204}},
        )
        session = Mock()
        session.get.side_effect = [precheck, confirmation, score]
        session.post.return_value = signed
        session.request.return_value = count_response(7, False)

        with mock.patch("modules.zeeho.date", FakeDate):
            result = ZeehoClient(make_settings(), session=session).sign_in(
                Account("1", "Bearer token", "account")
            )

        self.assertTrue(result.ok)
        self.assertFalse(result.already_signed)
        self.assertEqual(result.error, "")
        self.assertEqual(result.gained_points, 5)
        self.assertEqual(result.continue_days, 7)  # 连签取自 App 端 signin/count
        self.assertEqual(result.available_score, 204)
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(
            session.request.call_args.args[:2],
            ("GET", "https://tapi.zeehoev.com" + TAPI_SIGNIN_COUNT_PATH),
        )
        sent_post = session.post.call_args
        self.assertEqual(sent_post.kwargs["data"], b"")
        self.assertEqual(sent_post.kwargs["headers"]["Content-Length"], "0")

    def test_sign_in_falls_back_to_points_from_signin_response(self):
        # 签到接口响应自带 data.integralScore；明细复核里没有积分时也用得上
        precheck = ok_payload({"nowSignDetailVos": []})
        signed = ok_payload({"signStatue": 2, "integralScore": 3})
        confirmation = ok_payload(
            {"nowSignDetailVos": [{"createDate": FIXED_TODAY.isoformat(), "signStatue": 3}]}
        )
        score = ok_payload({"score": 12})
        session = Mock()
        session.get.side_effect = [precheck, confirmation, score]
        session.post.return_value = signed
        session.request.return_value = count_response(4, False)

        with mock.patch("modules.zeeho.date", FakeDate):
            result = ZeehoClient(make_settings(), session=session).sign_in(
                Account("1", "Bearer token", "account")
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.gained_points, 3)

    def test_sign_in_reports_points_when_confirmation_fails(self):
        precheck = ok_payload({"nowSignDetailVos": []})
        signed = ok_payload({"integralScore": "1"})
        broken = response(500, {"code": "40000", "message": "网络开小差"})
        session = Mock()
        session.get.side_effect = [precheck, broken]
        session.post.return_value = signed
        # 签到前后的 App 端状态查询都失败：无法兜底确认，应如实报失败
        session.request.return_value = response(500, {"code": "40000", "message": "坏"})

        with mock.patch("modules.zeeho.date", FakeDate):
            result = ZeehoClient(make_settings(), session=session).sign_in(
                Account("1", "Bearer token", "account")
            )

        self.assertFalse(result.ok)
        self.assertIn("本次积分 +1", result.error)
        self.assertIn("网络开小差", result.error)

    def test_sign_in_falls_back_to_app_status_when_confirmation_fails(self):
        # 明细复核挂了，但 App 端 signStatus 从「未签」变成「已签」：算签到成功
        precheck = ok_payload({"nowSignDetailVos": []})
        signed = ok_payload({"integralScore": "1"})
        broken = response(500, {"code": "40000", "message": "网络开小差"})
        score = ok_payload({"score": 66})
        session = Mock()
        session.get.side_effect = [precheck, broken, score]
        session.post.return_value = signed
        session.request.side_effect = [count_response(3, False), count_response(4, True)]

        with mock.patch("modules.zeeho.date", FakeDate):
            result = ZeehoClient(make_settings(), session=session).sign_in(
                Account("1", "Bearer token", "account")
            )

        self.assertTrue(result.ok)
        self.assertFalse(result.already_signed)
        self.assertEqual(result.gained_points, 1)
        self.assertEqual(result.continue_days, 4)
        self.assertEqual(result.available_score, 66)

    def test_sign_in_already_signed_reports_stats_without_post(self):
        # 9-07 起每天签到到今天，今天已签：应跳过 POST 并给出统计
        entries = [signed_entry(day) for day in range(7, 16)]
        precheck = response(
            200,
            {"code": "10000", "message": "操作成功", "data": {"nowSignDetailVos": entries}},
        )
        score = response(
            200,
            {"code": "10000", "message": "操作成功", "data": {"score": 314}},
        )
        session = Mock()
        session.get.side_effect = [precheck, score]
        session.request.return_value = count_response(12, True)

        with mock.patch("modules.zeeho.date", FakeDate):
            result = ZeehoClient(make_settings(), session=session).sign_in(
                Account("1", "Bearer token", "account")
            )

        self.assertTrue(result.ok)
        self.assertTrue(result.already_signed)
        self.assertEqual(result.gained_points, 1)
        self.assertEqual(result.continue_days, 12)  # App 端数字优先于 H5 明细推算
        self.assertEqual(result.available_score, 314)
        self.assertEqual(session.get.call_count, 2)  # 预检 + 积分余额，不再扫明细算连签
        session.post.assert_not_called()

    def test_sign_in_streak_falls_back_to_month_scan_without_app_count(self):
        # App 端 signin/count 不可用时退回 H5 明细推算（含跨月补查）
        prev_entries = []
        for day in range(20, 32):
            prev_entries.append(
                {
                    "createDate": "2026-08-{:02d}".format(day),
                    "signStatue": 3 if day >= 25 else 4,
                    "integralScore": 1,
                }
            )
        cur_entries = [signed_entry(day) for day in range(1, 16)]
        prev_info = response(
            200,
            {"code": "10000", "message": "操作成功", "data": {"nowSignDetailVos": prev_entries}},
        )
        cur_info = response(
            200,
            {"code": "10000", "message": "操作成功", "data": {"nowSignDetailVos": cur_entries}},
        )
        score = response(
            200,
            {"code": "10000", "message": "操作成功", "data": {"score": 99}},
        )
        session = Mock()
        session.get.side_effect = [cur_info, prev_info, score]
        session.request.return_value = response(
            500, {"code": "40000", "message": "App 端不可用"}
        )

        with mock.patch("modules.zeeho.date", FakeDate):
            result = ZeehoClient(make_settings(), session=session).sign_in(
                Account("1", "Bearer token", "account")
            )

        self.assertTrue(result.already_signed)
        self.assertEqual(result.continue_days, 22)  # 8-25..9-15
        self.assertEqual(session.get.call_count, 3)


def ok_payload(data):
    return response(200, {"code": "10000", "message": "操作成功", "data": data})


def task_article(post_id, content, created, like_status=0):
    """mineArticleInfo 里的打卡帖条目（字段对齐真实抓包）。"""
    return {
        "tuuid": post_id,
        "tcontent": content,
        "types": "1",
        "createDate": created,
        "userLikeStatus": like_status,
    }


def progress_row(code, points=1, done=False, remarks=""):
    """getByUserId 规则表里的一行（字段对齐真实抓包）。"""
    return {
        "code": code,
        "integralNumber": points,
        "remarks": remarks,
        "createDate": (
            "{}T08:13:01.931+0800".format(FIXED_TODAY.isoformat()) if done else None
        ),
    }


def progress_rows(post_done=False, like_done=False, share_done=False):
    """三项每日任务的规则表：createDate 属于今天即「今天已完成」。"""

    def remarks(prefix, done):
        return "{},每日上限1次  ({}/1)".format(prefix, 1 if done else 0)

    return [
        progress_row("1023", 1, post_done, "每日首次发帖可获得1积分"),
        progress_row(
            "1026", 1, like_done, remarks("每次点赞内容可获得1积分", like_done)
        ),
        progress_row(
            "1024", 1, share_done, remarks("每次分享内容可获得1积分", share_done)
        ),
    ]


def step_of(result, key):
    return next(step for step in result.steps if step.key == key)


def queue_session(script):
    """Mock session：请求按脚本顺序出队，并记录每次调用的 method/url/kwargs。"""
    session = Mock()
    calls = []

    def dispatch(method, url, **kwargs):
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        return script.pop(0)

    session.request.side_effect = dispatch
    return session, calls, script


class DailyTaskPureFunctionTests(unittest.TestCase):
    def test_task_post_content_defaults_to_date_based_text(self):
        content = task_post_content("", RealDate(2026, 9, 15))
        self.assertEqual(content, "9月15日打卡")

    def test_task_post_content_honors_configured_text(self):
        content = task_post_content("  打卡啦  ", RealDate(2026, 9, 15))
        self.assertEqual(content, "打卡啦")

    def test_publish_body_matches_capture_shape(self):
        body = build_publish_body("9月15日打卡")
        parsed = json.loads(body)
        self.assertEqual(parsed["postcontent"], "9月15日打卡")
        self.assertEqual(parsed["files"], "[]")
        self.assertNotIn(" ", body)  # 紧凑 JSON，与抓包一致

    def test_like_body_uses_numeric_post_id(self):
        body = json.loads(build_like_body("20260915000000001"))
        self.assertEqual(body, {"kindFlag": "0", "postId": 20260915000000001})

    def test_classify_ignores_normal_user_posts(self):
        entries = [
            task_article("real1", "今天天气真好啊", "2026-09-15 08:00:00"),
            task_article("real2", "分享骑行日常", "2026-09-14 08:00:00"),
        ]
        today_post, stale = classify_task_articles(
            entries, RealDate(2026, 9, 15), "9月15日打卡"
        )
        self.assertIsNone(today_post)
        self.assertEqual(stale, [])

    def test_classify_splits_today_and_stale(self):
        entries = [
            task_article("old1", "9月14日打卡", "2026-09-14 12:00:00"),
            task_article("today1", "9月15日打卡", "2026-09-15 12:00:00"),
            task_article("dup1", "9月15日打卡", "2026-09-15 13:00:00"),
        ]
        today_post, stale = classify_task_articles(
            entries, RealDate(2026, 9, 15), "9月15日打卡"
        )
        self.assertEqual(article_id(today_post), "today1")
        self.assertEqual([article_id(item) for item in stale], ["old1", "dup1"])

    def test_daily_task_rules_read_today_create_date(self):
        payload = {"data": {"ps0003_01_002_VOList": progress_rows(True, True, True)}}

        rules = daily_task_rules(payload, FIXED_TODAY)

        for step in (STEP_POST, STEP_LIKE, STEP_SHARE):
            self.assertTrue(rules[step].done_today)
            self.assertEqual(rules[step].points, 1)

    def test_daily_task_rules_fall_back_to_remarks_progress(self):
        rows = [
            progress_row("1023", 1, False, "每日首次发帖可获得1积分"),
            progress_row("1026", 1, False, "每日上限1次  (1/1)"),
            progress_row("1024", 1, False, "每日上限1次  (0/1)"),
        ]

        rules = daily_task_rules({"data": {"ps0003_01_002_VOList": rows}}, FIXED_TODAY)

        self.assertFalse(rules[STEP_POST].done_today)
        self.assertTrue(rules[STEP_LIKE].done_today)
        self.assertFalse(rules[STEP_SHARE].done_today)

    def test_daily_task_rules_ignore_other_days_and_custom_points(self):
        rows = [
            {
                "code": "1023",
                "integralNumber": "2",
                "remarks": "每日首次发帖可获得1积分",
                "createDate": "2026-09-14T08:13:01.931+0800",
            }
        ]

        rules = daily_task_rules({"data": {"ps0003_01_002_VOList": rows}}, FIXED_TODAY)

        self.assertFalse(rules[STEP_POST].done_today)
        self.assertEqual(rules[STEP_POST].points, 2)
        self.assertEqual(rules[STEP_LIKE].points, 1)  # 规则行缺失时按默认 1 分

    def test_daily_task_rules_handle_malformed_payload(self):
        for payload in (None, {}, {"data": None}, {"data": {"ps0003_01_002_VOList": {}}}):
            rules = daily_task_rules(payload, FIXED_TODAY)
            self.assertEqual(len(rules), 3)
            for rule in rules.values():
                self.assertFalse(rule.done_today)
                self.assertEqual(rule.points, 1)

    def test_stage_error_detail_strips_repeated_stage(self):
        self.assertEqual(
            stage_error_detail("每日任务-发帖失败：code=40000，message=被禁言", "每日任务-发帖"),
            "code=40000，message=被禁言",
        )
        self.assertEqual(
            stage_error_detail("执行签到请求异常：ConnectionError: boom", "执行签到"),
            "请求异常：ConnectionError: boom",
        )
        self.assertEqual(
            stage_error_detail("别的错误", "每日任务-发帖"), "别的错误"
        )

    def test_signin_payload_points_reads_integral_score(self):
        self.assertEqual(signin_payload_points({"data": {"integralScore": "3"}}), 3)
        self.assertEqual(signin_payload_points({"data": {"integralScore": None}}), 0)
        self.assertEqual(signin_payload_points(None), 0)

    def test_signin_count_state_reads_streak_and_today_flag(self):
        state = signin_count_state({"data": {"count": 4, "signStatus": 1}})
        self.assertEqual(state, SigninCountState(streak=4, signed_today=True))

        state = signin_count_state({"data": {"count": "0", "signStatus": 0}})
        self.assertEqual(state, SigninCountState(streak=0, signed_today=False))

    def test_signin_count_state_handles_missing_fields(self):
        self.assertIsNone(signin_count_state(None))
        self.assertIsNone(signin_count_state({"data": None}))
        self.assertIsNone(signin_count_state({"data": {"signStatus": 1}}))


def article_id(entry):
    return entry["tuuid"] if entry else None


class DailyTaskTests(unittest.TestCase):
    def setUp(self):
        self.account = Account("20260825003041623", "Bearer token", "account")
        self.today = "2026-09-15"
        self.content = "9月15日打卡"
        self.post_id = "20260915000000001"

    def test_full_chain_publish_like_share_delete(self):
        # 与抓包一致的调用顺序：查帖 → 查进度 → 发帖 → 复核查帖 → 点赞 → 分享 → 调整 → 删帖
        script = [
            ok_payload([]),  # 查询我的帖子：无帖
            ok_payload({"ps0003_01_002_VOList": progress_rows(False, False, False)}),
            ok_payload(True),  # 发帖
            ok_payload(  # 复核：今天的新打卡帖出现在列表首位
                [
                    task_article(
                        self.post_id, self.content, "{} 12:03:05".format(self.today)
                    )
                ]
            ),
            ok_payload(True),  # 点赞
            ok_payload(1),  # 分享
            ok_payload(True),  # 积分调整
            ok_payload(True),  # 删帖
        ]
        session, calls, script = queue_session(script)

        with mock.patch("modules.zeeho.date", FakeDate):
            result = ZeehoClient(make_settings(), session=session).daily_tasks(
                self.account
            )

        self.assertTrue(result.ok)
        self.assertFalse(result.skipped)
        self.assertEqual(
            [step.key for step in result.steps],
            [STEP_POST, STEP_LIKE, STEP_SHARE, STEP_DELETE],
        )
        for step in result.steps:
            self.assertTrue(step.ok)
            self.assertFalse(step.skipped)
        self.assertEqual(step_of(result, STEP_POST).points, 1)
        self.assertEqual(step_of(result, STEP_LIKE).points, 1)
        self.assertEqual(step_of(result, STEP_SHARE).points, 1)
        self.assertEqual(step_of(result, STEP_DELETE).points, 0)
        self.assertEqual(script, [])  # 所有脚本响应都被消费

        self.assertEqual(calls[0]["url"], "https://tapi.zeehoev.com" + TAPI_MY_ARTICLES_PATH)
        self.assertEqual(
            calls[0]["kwargs"]["params"], {"userId": self.account.user_id}
        )
        self.assertEqual(
            calls[1]["url"], "https://tapi.zeehoev.com" + TAPI_INTEGRAL_PATH
        )
        publish = calls[2]
        self.assertEqual(publish["method"], "POST")
        self.assertEqual(
            publish["url"], "https://tapi.zeehoev.com" + TAPI_CREATE_ARTICLE_PATH
        )
        self.assertEqual(
            publish["kwargs"]["data"].decode("utf-8"), build_publish_body(self.content)
        )
        like = calls[4]
        self.assertEqual(like["method"], "POST")
        self.assertEqual(like["url"], "https://tapi.zeehoev.com" + TAPI_LIKE_PATH)
        self.assertEqual(
            like["kwargs"]["data"].decode("utf-8"),
            build_like_body(self.post_id),
        )
        share = calls[5]
        self.assertEqual(share["method"], "PUT")
        self.assertEqual(
            share["url"],
            "https://tapi.zeehoev.com"
            + TAPI_SHARE_PATH.format(article_id=self.post_id),
        )
        self.assertEqual(
            calls[6]["url"], "https://tapi.zeehoev.com" + TAPI_ADJUST_SHARE_PATH
        )
        delete = calls[7]
        self.assertEqual(delete["method"], "DELETE")
        self.assertEqual(
            delete["url"], "https://tapi.zeehoev.com" + TAPI_DELETE_ARTICLE_PATH
        )
        self.assertEqual(
            delete["kwargs"]["params"], {"articleId": self.post_id, "postType": "1"}
        )
        self.assertEqual(delete["kwargs"]["data"], b"")

    def test_already_done_skips_whole_chain(self):
        # 三项任务今天都已完成且没有今日打卡帖（上一轮含删帖已跑完）
        script = [
            ok_payload([]),
            ok_payload({"ps0003_01_002_VOList": progress_rows(True, True, True)}),
        ]
        session, calls, script = queue_session(script)

        with mock.patch("modules.zeeho.date", FakeDate):
            result = ZeehoClient(make_settings(), session=session).daily_tasks(
                self.account
            )

        self.assertTrue(result.ok)
        self.assertTrue(result.skipped)
        self.assertTrue(all(step.skipped for step in result.steps))
        self.assertTrue(all(step.ok for step in result.steps))
        self.assertEqual(len(calls), 2)
        self.assertEqual(script, [])

    def test_app_side_post_today_skips_publishing(self):
        # App 端今天已经发过帖（规则行 createDate = 今天）且没有脚本打卡帖：不重复发帖
        script = [
            ok_payload([]),
            ok_payload({"ps0003_01_002_VOList": progress_rows(True, False, True)}),
            ok_payload(True),  # 点赞
        ]
        session, calls, script = queue_session(script)

        with mock.patch("modules.zeeho.date", FakeDate):
            result = ZeehoClient(make_settings(), session=session).daily_tasks(
                self.account
            )

        self.assertTrue(result.ok)
        self.assertFalse(result.skipped)
        post_step = step_of(result, STEP_POST)
        self.assertTrue(post_step.skipped)
        self.assertEqual(post_step.detail, "App 端今日已发帖")
        self.assertEqual(
            [call["url"] for call in calls],
            [
                "https://tapi.zeehoev.com" + TAPI_MY_ARTICLES_PATH,
                "https://tapi.zeehoev.com" + TAPI_INTEGRAL_PATH,
                "https://tapi.zeehoev.com" + TAPI_LIKE_PATH,
            ],
        )
        self.assertEqual(script, [])

    def test_crash_leftover_is_finished_then_deleted(self):
        # 上一轮发帖后崩溃：今天打卡帖仍在且已被赞/已分享 → 复用并补删
        script = [
            ok_payload(
                [
                    task_article(
                        self.post_id,
                        self.content,
                        "{} 12:03:05".format(self.today),
                        like_status=1,
                    )
                ]
            ),
            ok_payload({"ps0003_01_002_VOList": progress_rows(True, True, True)}),
            ok_payload(True),  # 删帖
        ]
        session, calls, script = queue_session(script)

        with mock.patch("modules.zeeho.date", FakeDate):
            result = ZeehoClient(make_settings(), session=session).daily_tasks(
                self.account
            )

        self.assertTrue(result.ok)
        self.assertFalse(result.skipped)
        self.assertEqual(step_of(result, STEP_POST).detail, "复用今日已有帖")
        self.assertTrue(step_of(result, STEP_LIKE).skipped)
        self.assertTrue(step_of(result, STEP_SHARE).skipped)
        self.assertTrue(step_of(result, STEP_DELETE).ok)
        self.assertFalse(step_of(result, STEP_DELETE).skipped)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[2]["method"], "DELETE")

    def test_stale_posts_are_cleaned_before_publish(self):
        # 昨天的崩溃残留打卡帖先被删，再走完整链
        script = [
            ok_payload([task_article("old-1", "9月14日打卡", "2026-09-14 09:00:00")]),
            ok_payload({"ps0003_01_002_VOList": progress_rows(False, False, False)}),
            ok_payload(True),  # 删残留
            ok_payload(True),  # 发帖
            ok_payload(
                [task_article(self.post_id, self.content, "{} 12:03:05".format(self.today))]
            ),
            ok_payload(True),  # 点赞
            ok_payload(1),  # 分享
            ok_payload(True),  # 调整
            ok_payload(True),  # 删帖
        ]
        session, calls, script = queue_session(script)

        with mock.patch("modules.zeeho.date", FakeDate):
            result = ZeehoClient(make_settings(), session=session).daily_tasks(
                self.account
            )

        self.assertTrue(result.ok)
        self.assertEqual(step_of(result, STEP_DELETE).detail, "清理残留 1 篇")
        cleanup = calls[2]
        self.assertEqual(cleanup["method"], "DELETE")
        self.assertEqual(
            cleanup["kwargs"]["params"], {"articleId": "old-1", "postType": "1"}
        )
        self.assertEqual(script, [])

    def test_delete_failure_reports_failure(self):
        script = [
            ok_payload([]),
            ok_payload({"ps0003_01_002_VOList": progress_rows(False, False, False)}),
            ok_payload(True),  # 发帖
            ok_payload(
                [task_article(self.post_id, self.content, "{} 12:03:05".format(self.today))]
            ),
            ok_payload(True),  # 点赞
            ok_payload(1),  # 分享
            ok_payload(True),  # 调整
            response(
                200,
                {"code": "40000", "message": "帖子不存在或无权删除", "data": None},
            ),  # 删帖业务失败
        ]
        session, calls, script = queue_session(script)

        with mock.patch("modules.zeeho.date", FakeDate):
            result = ZeehoClient(make_settings(), session=session).daily_tasks(
                self.account
            )

        self.assertFalse(result.ok)
        delete_step = step_of(result, STEP_DELETE)
        self.assertFalse(delete_step.ok)
        self.assertIn("帖子不存在或无权删除", delete_step.detail)
        self.assertNotIn("每日任务-删帖", delete_step.detail)

    def test_list_query_failure_stops_before_publishing(self):
        session, calls, script = queue_session(
            [response(500, {"code": "10000", "message": "", "data": None})]
        )

        with mock.patch("modules.zeeho.date", FakeDate):
            result = ZeehoClient(make_settings(), session=session).daily_tasks(
                self.account
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.steps, ())
        self.assertIn("查询我的帖子", result.error)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
