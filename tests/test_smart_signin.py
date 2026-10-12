import hashlib
import json
import unittest
from dataclasses import replace
from datetime import date as RealDate
from unittest import mock

import requests

from modules.config import Account, Settings
from modules.zeeho import SCORE_ENDPOINT, SMART_SIGNIN_PATH, ZeehoClient, smart_signin_state


TODAY = "2026-09-15"
ACCOUNT = Account("test-user", "Bearer test-token", "test")


class FakeDate(RealDate):
    @classmethod
    def today(cls):
        return cls(2026, 9, 15)


def response(data=None, code="10000", status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps({"code": code, "data": data}).encode()
    return result


def state(last_time, count):
    return response({"lastTime": last_time, "continueDays": count})


def settings():
    return Settings(
        accounts=[ACCOUNT], app_id="h5-app", app_secret="h5-secret",
        tapi_app_id="native-app", tapi_app_secret="native-secret",
        base_url="https://example.test", user_agent="browser-agent",
        zeeho_user_agent="app-agent", timeout=15, use_proxy=False,
        notify=False, signin_mode="smart",
    )


class SmartSigninTests(unittest.TestCase):
    def run_signin(self, session, config=None):
        with mock.patch("modules.zeeho.date", FakeDate):
            return ZeehoClient(
                config or settings(), session=session, clock_ms=lambda: 123
            ).sign_in(ACCOUNT)

    def test_precheck_post_and_confirmation_use_native_key_and_smart_query(self):
        session = mock.Mock()
        session.get.side_effect = [
            state("2026-09-14T08:00:00", 8),
            state(TODAY + "T08:00:00", 9),
            response({"score": 100}),
        ]
        session.post.return_value = response({"signInStatus": 0, "integralScore": 3})
        result = self.run_signin(session)
        self.assertTrue(result.ok)
        self.assertFalse(result.already_signed)
        self.assertEqual((result.gained_points, result.continue_days, result.available_score), (3, 9, 100))
        self.assertEqual([call[0] for call in session.method_calls], ["get", "post", "get", "get"])
        for call in [session.get.call_args_list[0], session.post.call_args, session.get.call_args_list[1]]:
            self.assertEqual(call.args[0], "https://example.test" + SMART_SIGNIN_PATH)
            self.assertEqual(call.kwargs["params"], {"server_name": "SMART"})
            headers = call.kwargs["headers"]
            param = headers["Cfmoto-X-Param"]
            self.assertTrue(param.startswith("appId=native-app&"))
            self.assertTrue(param.endswith("timestamp=123"))
            expected = hashlib.md5(hashlib.sha1(
                ("server_name=SMART" + param + "native-secret").encode()
            ).hexdigest().encode()).hexdigest()
            self.assertEqual(headers["Cfmoto-X-Sign"], expected)
            self.assertEqual(headers["Authorization"], ACCOUNT.authorization)
            self.assertEqual(headers["user_id"], ACCOUNT.user_id)
            self.assertEqual(headers["User-Agent"], "browser-agent")
            self.assertNotIn("Cookie", headers)
        self.assertEqual(session.post.call_args.kwargs["data"], b"")
        self.assertEqual(session.post.call_args.kwargs["headers"]["Content-Length"], "0")
        self.assertEqual(session.get.call_args_list[2].args[0], SCORE_ENDPOINT.format(user_id=ACCOUNT.user_id))
        session.request.assert_not_called()

    def test_already_signed_skips_post_without_inventing_points(self):
        session = mock.Mock()
        session.get.side_effect = [state(TODAY, 30), response({"score": 200})]
        result = self.run_signin(session)
        self.assertTrue(result.ok)
        self.assertTrue(result.already_signed)
        self.assertEqual(result.gained_points, 0)
        self.assertEqual(result.continue_days, 30)
        session.post.assert_not_called()

    def test_first_signin_and_string_business_code(self):
        session = mock.Mock()
        session.get.side_effect = [state(None, 0), state(TODAY, "1"), response({"score": 0}, code=10000)]
        session.post.return_value = response({"signInStatus": "0"}, code=10000)
        result = self.run_signin(session)
        self.assertTrue(result.ok)
        self.assertEqual(result.continue_days, 1)
        self.assertEqual(result.available_score, 0)

    def test_precheck_rejection_or_malformed_state_never_posts(self):
        bad_responses = [
            response({}, code="30125", status=430), response({}, code="40000"),
            response({}), state("not-a-date", 1), state("2026-09-16", 1),
        ]
        malformed_json = response({})
        malformed_json._content = b'<html>bad response</html>'
        bad_responses.append(malformed_json)
        for failed in bad_responses:
            with self.subTest(payload=failed.text):
                session = mock.Mock()
                session.get.return_value = failed
                result = self.run_signin(session)
                self.assertFalse(result.ok)
                self.assertTrue(result.error)
                session.post.assert_not_called()
                self.assertEqual(session.get.call_count, 1)

    def test_post_failure_does_not_retry_or_switch_to_h5(self):
        for failed in [requests.Timeout(), response({}, code="40000"), response({}, code="30125", status=430)]:
            with self.subTest(failed=type(failed).__name__):
                session = mock.Mock()
                session.get.return_value = state("2026-09-14", 8)
                if isinstance(failed, Exception):
                    session.post.side_effect = failed
                else:
                    session.post.return_value = failed
                result = self.run_signin(session)
                self.assertFalse(result.ok)
                session.post.assert_called_once()
                self.assertEqual(session.get.call_count, 1)
                session.request.assert_not_called()

    def test_success_and_repeated_operation_both_require_confirmation(self):
        for code in ["10000", "repeatedly_operation"]:
            for confirmed in [True, False]:
                with self.subTest(code=code, confirmed=confirmed):
                    session = mock.Mock()
                    session.get.side_effect = [
                        state("2026-09-14", 8),
                        state(TODAY if confirmed else "2026-09-14", 9),
                        response({"score": 100}),
                    ]
                    session.post.return_value = response({"integralScore": 2}, code=code)
                    result = self.run_signin(session)
                    self.assertEqual(result.ok, confirmed)
                    self.assertEqual(result.already_signed, confirmed and code == "repeatedly_operation")
                    self.assertEqual(result.gained_points, 2)
                    session.post.assert_called_once()
                    self.assertEqual(session.get.call_count, 3 if confirmed else 2)

    def test_confirmation_failure_preserves_response_points(self):
        session = mock.Mock()
        session.get.side_effect = [state("2026-09-14", 8), response({}, status=500)]
        session.post.return_value = response({"integralScore": 1})
        result = self.run_signin(session)
        self.assertFalse(result.ok)
        self.assertEqual(result.gained_points, 1)
        self.assertIn("HTTP 500", result.error)
        session.post.assert_called_once()

    def test_server_already_signed_status_is_preserved_after_confirmation(self):
        session = mock.Mock()
        session.get.side_effect = [state("2026-09-14", 8), state(TODAY, 9), response({"score": 100})]
        session.post.return_value = response({"signInStatus": 1})
        self.assertTrue(self.run_signin(session).already_signed)

    def test_missing_native_credentials_stops_before_network(self):
        session = mock.Mock()
        result = self.run_signin(session, replace(settings(), tapi_app_id="", tapi_app_secret=""))
        self.assertFalse(result.ok)
        self.assertEqual(session.method_calls, [])

    def test_balance_uses_native_credentials_and_injected_clock(self):
        session = mock.Mock()
        session.get.return_value = response({"score": 100})
        client = ZeehoClient(settings(), session=session, clock_ms=lambda: 456)
        self.assertEqual(client.fetch_score(ACCOUNT), 100)
        param = session.get.call_args.kwargs["headers"]["Cfmoto-X-Param"]
        self.assertTrue(param.startswith("appId=native-app&"))
        self.assertTrue(param.endswith("timestamp=456"))

    def test_balance_failure_does_not_change_confirmed_signin(self):
        session = mock.Mock()
        session.get.side_effect = [state(TODAY, 5), response({}, code="invalid_token", status=401)]
        result = self.run_signin(session)
        self.assertTrue(result.ok)
        self.assertEqual(result.continue_days, 5)
        self.assertIsNone(result.available_score)
        session.post.assert_not_called()


class SmartStateTests(unittest.TestCase):
    def test_invalid_state_is_not_treated_as_unsigned(self):
        for data in [
            None, [], {}, {"continueDays": 0}, {"lastTime": TODAY},
            {"lastTime": TODAY, "continueDays": True},
            {"lastTime": TODAY, "continueDays": -1},
            {"lastTime": TODAY, "continueDays": 1.5},
            {"lastTime": TODAY, "continueDays": "bad"},
            {"lastTime": [], "continueDays": 0},
            {"lastTime": None, "continueDays": 5},
            {"lastTime": "2026-02-30", "continueDays": 5},
            {"lastTime": "2026-9-15", "continueDays": 5},
        ]:
            with self.subTest(data=data):
                self.assertIsNone(smart_signin_state({"data": data}, TODAY))


if __name__ == "__main__":
    unittest.main()
