import tempfile
import unittest
from pathlib import Path

import yaml

from modules.config import ConfigError, CONFIG_TEMPLATE, extract_h5_credentials, load_settings


class ExtractCredentialsTests(unittest.TestCase):
    app_id = "TestApp1"
    secret = "0123456789abcdef" * 2 + "01234567"

    def source(self, fields=("i1", "s1")):
        import json
        encode = lambda value: " ".join("{:08b}00000000".format(ord(char)) for char in value)
        return json.dumps({
            "VUE_APP_TEST_CONFIG": {fields[0]: encode("WrongId1"), fields[1]: encode("f" * 40)},
            "VUE_APP_PRODUCTION_CONFIG": {fields[0]: encode(self.app_id), fields[1]: encode(self.secret)},
        })

    def test_current_and_legacy_keys_select_only_production(self):
        for fields in [("i1", "s1"), ("appId", "appSecret")]:
            with self.subTest(fields=fields):
                self.assertEqual(extract_h5_credentials(self.source(fields)), (self.app_id, self.secret))

    def test_hex_escaped_and_json_string_literals(self):
        import json
        source = self.source()
        for value in [source.replace('"', r'\x22').replace(' ', r'\x20'), json.dumps(source)]:
            with self.subTest(value=value):
                self.assertEqual(extract_h5_credentials(value), (self.app_id, self.secret))

    def test_plain_credentials_and_identical_duplicates(self):
        source = '{"VUE_APP_PRODUCTION_CONFIG":{"appId":"TestApp1","appSecret":"' + self.secret + '"}}'
        self.assertEqual(extract_h5_credentials(source + source), (self.app_id, self.secret))

    def test_missing_invalid_and_conflicting_production_keys_fail_closed(self):
        for source in [
            '{"VUE_APP_TEST_CONFIG":{}}',
            '{"VUE_APP_PRODUCTION_CONFIG":{}}',
            '{"VUE_APP_PRODUCTION_CONFIG":{"i1":"bad","s1":"bad"}}',
            self.source() + self.source().replace("0101010000000000", "0101100000000000"),
            '{"VUE_APP_PRODUCTION_CONFIG":{"i1": invalid}}',
        ]:
            with self.subTest(source=source):
                with self.assertRaises(ConfigError):
                    extract_h5_credentials(source)


class ConfigTests(unittest.TestCase):
    def test_signin_mode_validation_and_native_credentials_requirement(self):
        content = """
app:
  app_id: test-app
  app_secret: test-secret
  tapi_app_id: {app_id}
  tapi_app_secret: {secret}
  user_agent: browser-agent
  zeeho_user_agent: app-agent
runtime:
  signin_mode: {mode}
accounts:
  - user_id: "1"
    authorization: test-token
"""
        for mode, native, expected in [
            ("h5", False, "h5"), ("smart", True, "smart"),
            ("SMART", True, "smart"), ("smart", False, None),
            ("invalid", True, None), ("false", True, None),
        ]:
            with self.subTest(mode=mode, native=native), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "zeeho_config.yaml"
                path.write_text(content.format(
                    app_id="native-app" if native else "''",
                    secret="native-secret" if native else "''", mode=mode,
                ), encoding="utf-8")
                if expected is None:
                    with self.assertRaises(ConfigError):
                        load_settings(path)
                else:
                    self.assertEqual(load_settings(path).signin_mode, expected)

    def test_session_keepalive_switch(self):
        content = """
app:
  app_id: test-app
  app_secret: test-secret
  user_agent: browser-agent
  zeeho_user_agent: app-agent
runtime:
  session_keepalive: {switch}
accounts:
  - user_id: "1"
    authorization: test-token
"""
        for switch, expected in [("true", True), ("false", False), ("'off'", False), ("'yes'", True), ("invalid", None)]:
            with self.subTest(switch=switch), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "zeeho_config.yaml"
                path.write_text(content.format(switch=switch), encoding="utf-8")
                if expected is None:
                    with self.assertRaisesRegex(ConfigError, "runtime.session_keepalive"):
                        load_settings(path)
                else:
                    self.assertEqual(load_settings(path).session_keepalive, expected)

    def test_blind_box_switch_parses_values_and_rejects_invalid_input(self):
        content = """
app:
  app_id: test-app
  app_secret: test-secret
  user_agent: browser-agent
  zeeho_user_agent: app-agent
runtime:
  blind_box: {switch}
accounts:
  - user_id: "1"
    authorization: test-token
"""
        for switch, expected in [("true", True), ("false", False), ("'off'", False), ("'yes'", True), ("invalid", None)]:
            with self.subTest(switch=switch), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "zeeho_config.yaml"
                path.write_text(content.format(switch=switch), encoding="utf-8")
                if expected is None:
                    with self.assertRaisesRegex(ConfigError, "runtime.blind_box"):
                        load_settings(path)
                else:
                    self.assertEqual(load_settings(path).blind_box, expected)

    def test_loads_yaml_accounts_and_runtime_settings(self):
        content = """
app:
  app_id: test-app
  app_secret: test-secret
  user_agent: browser-agent
  zeeho_user_agent: app-agent
runtime:
  timeout_seconds: 9
  use_proxy: true
  notify: false
accounts:
  - name: A
    user_id: "1"
    authorization: Bearer abc
  - userId: "2"
    token: def
    userName: B
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zeeho_config.yaml"
            path.write_text(content, encoding="utf-8")
            settings = load_settings(path)

        self.assertEqual([item.user_id for item in settings.accounts], ["1", "2"])
        self.assertEqual(settings.accounts[1].authorization, "Bearer def")
        self.assertEqual(settings.timeout, 9)
        self.assertTrue(settings.use_proxy)
        self.assertFalse(settings.notify)
        self.assertTrue(settings.session_keepalive)
        self.assertEqual(settings.signin_mode, "h5")

    def test_creates_template_when_config_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zeeho_config.yaml"
            with self.assertRaisesRegex(ConfigError, "首次运行已创建"):
                load_settings(path)
            self.assertEqual(path.read_text(encoding="utf-8"), CONFIG_TEMPLATE)

    def test_rejects_unfilled_account(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zeeho_config.yaml"
            path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path)

    def test_decodes_binary_app_values_and_writes_them_back(self):
        expected_id = "Sw5F9uJi"
        expected = "4b3e188875610b84462baf60f1257bd14eb9bb86"
        encoded_id = " ".join(
            "{:08b}00000000".format(ord(char)) for char in expected_id
        )
        encoded = " ".join("{:08b}00000000".format(ord(char)) for char in expected)
        content = """
app:
  app_id: "{}"
  app_secret: "{}"
  user_agent: browser-agent
  zeeho_user_agent: app-agent
accounts:
  - user_id: "1"
    authorization: token
""".format(encoded_id, encoded)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zeeho_config.yaml"
            path.write_text(content, encoding="utf-8")
            settings = load_settings(path)
            written = yaml.safe_load(path.read_text(encoding="utf-8"))

        self.assertEqual(settings.app_id, expected_id)
        self.assertEqual(settings.app_secret, expected)
        self.assertEqual(written["app"]["app_id"], expected_id)
        self.assertEqual(written["app"]["app_secret"], expected)

    def test_decode_writeback_preserves_account_text_exactly(self):
        expected_id = "Sw5F9uJi"
        expected_secret = "4b3e188875610b84462baf60f1257bd14eb9bb86"
        encoded_id = " ".join(
            "{:08b}00000000".format(ord(char)) for char in expected_id
        )
        encoded_secret = " ".join(
            "{:08b}00000000".format(ord(char)) for char in expected_secret
        )
        content = """# keep this comment
app:
  app_id: "{}"
  app_secret: "{}"
  user_agent: "browser-agent"
  zeeho_user_agent: "app-agent"
accounts:
  - name: "account"
    user_id: "1"
    authorization: "Bearer token"
    user_agent: "Mozilla/5.0 (Linux; Android 17) Mobile Safari/537.36"
""".format(encoded_id, encoded_secret)
        expected_text = content.replace(encoded_id, expected_id).replace(
            encoded_secret, expected_secret
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zeeho_config.yaml"
            path.write_text(content, encoding="utf-8")
            load_settings(path)
            written_text = path.read_text(encoding="utf-8")

        self.assertEqual(written_text, expected_text)

    def test_rejects_invalid_binary_app_id_before_runtime(self):
        invalid_id = " ".join(["0100000100000000"] * 7)
        content = """
app:
  app_id: "{}"
  app_secret: plain-secret
accounts:
  - user_id: "1"
    authorization: token
""".format(invalid_id)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zeeho_config.yaml"
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "app.app_id"):
                load_settings(path)

    def test_rejects_binary_app_id_with_invalid_character(self):
        invalid_id = " ".join(["0100000100000000"] * 7 + ["0100000X00000000"])
        content = """
app:
  app_id: "{}"
  app_secret: plain-secret
accounts:
  - user_id: "1"
    authorization: token
""".format(invalid_id)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zeeho_config.yaml"
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "只能包含"):
                load_settings(path)


    def test_loads_daily_task_settings_and_tapi_keypair(self):
        content = """
app:
  app_id: test-app
  app_secret: test-secret
  tapi_app_id: S7qPWPU1
  tapi_app_secret: 46870a8fb4478eb19f23b0c6bb7d8a2b8a1f7e3d
  user_agent: browser-agent
  zeeho_user_agent: app-agent
runtime:
  daily_tasks: false
  blind_box: false
  daily_post_content: "骑行打卡"
accounts:
  - user_id: "1"
    authorization: token
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zeeho_config.yaml"
            path.write_text(content, encoding="utf-8")
            settings = load_settings(path)

        self.assertFalse(settings.daily_tasks)
        self.assertFalse(settings.blind_box)
        self.assertEqual(settings.daily_post_content, "骑行打卡")
        self.assertEqual(settings.tapi_app_id, "S7qPWPU1")
        self.assertEqual(settings.tapi_app_secret, "46870a8fb4478eb19f23b0c6bb7d8a2b8a1f7e3d")

    def test_daily_task_defaults_when_fields_absent(self):
        content = """
app:
  app_id: test-app
  app_secret: test-secret
  user_agent: browser-agent
  zeeho_user_agent: app-agent
accounts:
  - user_id: "1"
    authorization: token
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zeeho_config.yaml"
            path.write_text(content, encoding="utf-8")
            settings = load_settings(path)

        self.assertTrue(settings.daily_tasks)
        self.assertTrue(settings.blind_box)
        self.assertEqual(settings.daily_post_content, "")
        self.assertEqual(settings.tapi_app_id, "")
        self.assertEqual(settings.tapi_app_secret, "")

    def test_tapi_keypair_requires_both_fields(self):
        content = """
app:
  app_id: test-app
  app_secret: test-secret
  tapi_app_id: S7qPWPU1
  user_agent: browser-agent
  zeeho_user_agent: app-agent
accounts:
  - user_id: "1"
    authorization: token
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zeeho_config.yaml"
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "tapi_app_id"):
                load_settings(path)

    def test_decodes_binary_tapi_values_and_writes_them_back(self):
        expected_id = "S7qPWPU1"
        expected_secret = "46870a8fb4478eb19f23b0c6bb7d8a2b8a1f7e3d"
        encoded_id = " ".join(
            "{:08b}00000000".format(ord(char)) for char in expected_id
        )
        encoded_secret = " ".join(
            "{:08b}00000000".format(ord(char)) for char in expected_secret
        )
        content = """
app:
  app_id: test-app
  app_secret: test-secret
  tapi_app_id: "{}"
  tapi_app_secret: "{}"
  user_agent: browser-agent
  zeeho_user_agent: app-agent
accounts:
  - user_id: "1"
    authorization: token
""".format(encoded_id, encoded_secret)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zeeho_config.yaml"
            path.write_text(content, encoding="utf-8")
            settings = load_settings(path)
            written = yaml.safe_load(path.read_text(encoding="utf-8"))

        self.assertEqual(settings.tapi_app_id, expected_id)
        self.assertEqual(settings.tapi_app_secret, expected_secret)
        self.assertEqual(written["app"]["tapi_app_id"], expected_id)
        self.assertEqual(written["app"]["tapi_app_secret"], expected_secret)


if __name__ == "__main__":
    unittest.main()
