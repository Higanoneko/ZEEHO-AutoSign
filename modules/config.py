"""从青龙配置目录读取并校验 ZEEHO 配置。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, List, Mapping, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover - 由缺少依赖的运行环境触发
    yaml = None


CONFIG_FILE_NAME = "zeeho_config.yaml"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIRECTORIES = (
    (Path("/ql/data/config"), Path("/ql/config"), PROJECT_ROOT)
    if sys.platform.startswith("linux")
    else (PROJECT_ROOT,)
)

DEFAULT_APP_ID = ""
DEFAULT_APP_SECRET = ""
DEFAULT_BASE_URL = "https://h5.zeehoev.com"
DEFAULT_USER_AGENT = "ZEEHO/5.0 (iPhone; iOS 17.0; Scale/3.00)"

CONFIG_TEMPLATE = """# ZEEHO 自动签到配置文件
# 青龙面板中请在「配置文件」页面编辑 zeeho_config.yaml。

app:
  app_id: ""
  app_secret: ""
  # 原生 App 密钥对，供 App 域接口与 SMART 签到使用：
  # h5 模式下可空（App 域回退 H5 密钥）；smart 模式必须成对填写。
  tapi_app_id: ""
  tapi_app_secret: ""
  base_url: "https://h5.zeehoev.com"
  user_agent: ""
  zeeho_user_agent: ""

runtime:
  # 签到方案：h5（默认）或 smart（需填写原生 App 密钥对）
  signin_mode: h5
  timeout_seconds: 15
  use_proxy: false
  notify: true
  # 每次运行先模拟 App 进入前台，保持当前登录会话
  session_keepalive: true
  # 签到盲盒：服务端 signCount 达到 30 时自动领取
  blind_box: true
  # 每日任务（发帖→喜欢→分享→删帖，前 3 步各 +1 积分，删帖为清理）
  daily_tasks: true
  # 自动发布的帖子正文；留空默认按当天日期生成，形如「9月9日打卡」
  daily_post_content: ""

accounts:
  - name: "账号1"
    user_id: ""
    authorization: ""

  # 更多账号请复制上面的账号块：
  # - name: "账号2"
  #   user_id: ""
  #   authorization: ""
"""


class ConfigError(ValueError):
    """配置文件缺失、格式错误或内容不完整。"""


@dataclass(frozen=True)
class Account:
    user_id: str
    authorization: str
    name: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], index: int) -> "Account":
        user_id = str(value.get("user_id") or value.get("userId") or "").strip()
        raw_token = str(
            value.get("authorization") or value.get("token") or ""
        ).strip()
        if not user_id:
            raise ConfigError("账号{}缺少 user_id".format(index))
        if not raw_token:
            raise ConfigError("账号{}缺少 authorization".format(index))

        authorization = raw_token
        if not raw_token.lower().startswith("bearer "):
            authorization = "Bearer " + raw_token

        return cls(
            user_id=user_id,
            authorization=authorization,
            name=str(
                value.get("name")
                or value.get("user_name")
                or value.get("userName")
                or "账号{}".format(index)
            ).strip(),
        )


@dataclass(frozen=True)
class Settings:
    accounts: List[Account]
    app_id: str
    app_secret: str
    base_url: str
    user_agent: str
    zeeho_user_agent: str
    timeout: float
    use_proxy: bool
    notify: bool
    config_path: Path = PROJECT_ROOT / CONFIG_FILE_NAME
    tapi_app_id: str = ""
    tapi_app_secret: str = ""
    daily_tasks: bool = True
    daily_post_content: str = ""
    blind_box: bool = True
    session_keepalive: bool = True
    signin_mode: str = "h5"


def get_config_path() -> Path:
    """返回已存在的配置文件，或首个可用青龙配置目录中的目标路径。"""
    for directory in CONFIG_DIRECTORIES:
        candidate = directory / CONFIG_FILE_NAME
        if candidate.is_file():
            return candidate
    for directory in CONFIG_DIRECTORIES:
        if directory.is_dir():
            return directory / CONFIG_FILE_NAME
    return PROJECT_ROOT / CONFIG_FILE_NAME


def create_config_template(path: Path) -> None:
    """首次运行时创建可直接在青龙面板编辑的模板。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    except OSError as exc:
        raise ConfigError("无法创建配置模板 {}：{}".format(path, exc)) from exc


def _config_bool(value: Any, name: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "开启", "启用"}:
        return True
    if normalized in {"0", "false", "no", "off", "关闭", "禁用"}:
        return False
    raise ConfigError("{} 必须为 true/false".format(name))


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("{} 必须是 YAML 对象".format(name))
    return value


def _decode_binary_value(
    value: str, field_name: str, expected_length: int
) -> Tuple[str, bool]:
    """先验 16 位二进制空格串，再提取每组前 8 位 ASCII。"""
    secret = str(value or "").strip()
    tokens = secret.split()
    looks_binary = len(tokens) > 1 or (
        len(tokens) == 1
        and len(tokens[0]) == 16
        and set(tokens[0]) <= {"0", "1"}
    )
    if not looks_binary:
        return secret, False
    if any(set(token) - {"0", "1"} for token in tokens):
        raise ConfigError("{} 二进制格式只能包含 0、1 和空格".format(field_name))
    if len(tokens) != expected_length or any(len(token) != 16 for token in tokens):
        raise ConfigError(
            "{} 二进制格式应包含 {} 组 16 位数据".format(
                field_name, expected_length
            )
        )
    if any(token[8:] != "00000000" for token in tokens):
        raise ConfigError("{} 每组二进制的后 8 位应为 0".format(field_name))

    decoded = "".join(chr(int(token[:8], 2)) for token in tokens)
    if len(decoded) != expected_length or not decoded.isascii() or not decoded.isprintable():
        raise ConfigError("{} 解码结果不是有效 ASCII 字符串".format(field_name))
    return decoded, True


def decode_app_id(value: str) -> Tuple[str, bool]:
    """识别并解码 8 组二进制格式的 app id。"""
    decoded, changed = _decode_binary_value(value, "app.app_id", 8)
    if changed and not decoded.isalnum():
        raise ConfigError("app.app_id 解码结果不是 8 位字母数字字符串")
    return decoded, changed


def decode_app_secret(value: str) -> Tuple[str, bool]:
    """识别并解码 40 组二进制格式的 app secret。"""
    decoded, changed = _decode_binary_value(value, "app.app_secret", 40)
    if not changed:
        return decoded, False
    if len(decoded) != 40 or any(char not in "0123456789abcdefABCDEF" for char in decoded):
        raise ConfigError("app.app_secret 解码结果不是 40 位十六进制字符串")
    return decoded.lower(), True


def extract_h5_credentials(source: str) -> Tuple[str, str]:
    """从 H5 JS 的生产配置提取密钥；只解析文本，不执行下载的 JavaScript。"""
    # 兼容字符串表中的 \x22 / \x20 和 JSON 字符串的转义引号。
    normalized = re.sub(
        r"\\x([0-9a-fA-F]{2})", lambda match: chr(int(match.group(1), 16)), source
    ).replace(r'\"', '"')
    matches = re.findall(
        r'["\']VUE_APP_PRODUCTION_CONFIG["\']\s*:\s*(\{[^{}]*\})', normalized
    )
    if not matches:
        raise ConfigError("JS 中未找到 VUE_APP_PRODUCTION_CONFIG")
    candidates = set()
    for value in matches:
        try:
            config = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise ConfigError("JS 中的生产配置不是有效 JSON") from exc
        app_id, _ = decode_app_id(config.get("i1") or config.get("appId") or "")
        app_secret, _ = decode_app_secret(config.get("s1") or config.get("appSecret") or "")
        if not re.fullmatch(r"[A-Za-z0-9]{8}", app_id) or not re.fullmatch(
            r"[0-9a-fA-F]{40}", app_secret
        ):
            raise ConfigError("生产配置缺少有效的 i1/s1 或 appId/appSecret")
        candidates.add((app_id, app_secret))
    if len(candidates) != 1:
        raise ConfigError("JS 包含多组不同的生产密钥，请核对页面实际使用的配置")
    return candidates.pop()


def _replace_app_fields(
    source: str, replacements: Mapping[str, str]
) -> str:
    """只替换 app 段里的指定字段，其余配置文本逐字节保持。"""
    replaced = {key: False for key in replacements}
    lines = source.splitlines(keepends=True)
    in_app_section = False

    for index, line in enumerate(lines):
        top_level = re.match(r"^([A-Za-z_][\w-]*)\s*:", line)
        if top_level:
            in_app_section = top_level.group(1) == "app"
            continue
        if not in_app_section:
            continue
        for field, value in replacements.items():
            if replaced[field]:
                continue
            match = re.match(
                r"^(\s+" + re.escape(field) + r"\s*:\s*).*(\r?\n)?$", line
            )
            if match:
                newline = match.group(2) or ""
                lines[index] = '{}"{}"{}'.format(match.group(1), value, newline)
                replaced[field] = True
                break

    missing = [field for field, was_replaced in replaced.items() if not was_replaced]
    if missing:
        raise ConfigError("配置文件 app 段缺少字段：{}".format("、".join(missing)))
    return "".join(lines)


def _write_decoded_app_fields(
    path: Path, source: str, replacements: Mapping[str, str]
) -> None:
    """定点替换 app 字段并原子写回，避免改动账号配置。"""
    updated = _replace_app_fields(source, replacements)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=path.name + ".",
            suffix=".tmp",
            dir=str(path.parent),
            delete=False,
        ) as temporary:
            temporary.write(updated)
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    except OSError as exc:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise ConfigError("无法写回已解码的 app 配置到 {}：{}".format(path, exc)) from exc


def load_settings(config_path: Optional[Path] = None) -> Settings:
    """读取完整配置；调用者只需处理 ConfigError。"""
    if yaml is None:
        raise ConfigError("缺少 PyYAML，请在青龙依赖管理中安装 pyyaml")

    path = Path(config_path) if config_path is not None else get_config_path()
    if not path.exists():
        create_config_template(path)
        raise ConfigError("首次运行已创建配置模板：{}，请填写后重新运行".format(path))

    try:
        source = path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(source)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError("无法读取配置文件 {}：{}".format(path, exc)) from exc
    if not isinstance(parsed, dict):
        raise ConfigError("配置文件顶层必须是 YAML 对象")

    app = _mapping(parsed.get("app"), "app")
    runtime = _mapping(parsed.get("runtime"), "runtime")
    account_values = parsed.get("accounts")
    if not isinstance(account_values, list) or not account_values:
        raise ConfigError("accounts 必须是非空账号列表")
    if not all(isinstance(value, dict) for value in account_values):
        raise ConfigError("accounts 中的每个账号都必须是 YAML 对象")
    accounts = [
        Account.from_mapping(value, index)
        for index, value in enumerate(account_values, 1)
    ]

    app_id, app_id_was_decoded = decode_app_id(
        str(app.get("app_id") or DEFAULT_APP_ID)
    )
    app_secret, secret_was_decoded = decode_app_secret(
        str(app.get("app_secret") or DEFAULT_APP_SECRET)
    )
    # 原生 App 域专用密钥对：可空，为空回退 H5 密钥；两者必须成对出现
    raw_tapi_app_id = str(app.get("tapi_app_id") or "").strip()
    raw_tapi_app_secret = str(app.get("tapi_app_secret") or "").strip()
    if bool(raw_tapi_app_id) != bool(raw_tapi_app_secret):
        raise ConfigError("app.tapi_app_id 与 app.tapi_app_secret 需同时填写")
    tapi_app_id, tapi_id_was_decoded = decode_app_id(raw_tapi_app_id)
    tapi_app_secret, tapi_secret_was_decoded = decode_app_secret(
        raw_tapi_app_secret
    )
    signin_mode = str(runtime.get("signin_mode", "h5")).strip().lower()
    if signin_mode not in {"h5", "smart"}:
        raise ConfigError("runtime.signin_mode 必须是 h5 或 smart")
    if signin_mode == "smart" and not (tapi_app_id and tapi_app_secret):
        raise ConfigError("SMART 签到需填写 app.tapi_app_id 与 app.tapi_app_secret")
    base_url = str(app.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
    user_agent = str(app.get("user_agent") or "").strip()
    zeeho_user_agent = str(app.get("zeeho_user_agent") or "").strip()
    if not app_id or not app_secret or not base_url:
        raise ConfigError("app.app_id、app.app_secret、app.base_url 不能为空")
    if not user_agent or not zeeho_user_agent:
        raise ConfigError(
            "app.user_agent 和 app.zeeho_user_agent 均为必填请求头配置"
        )

    try:
        timeout = float(runtime.get("timeout_seconds", 15))
    except (TypeError, ValueError) as exc:
        raise ConfigError("runtime.timeout_seconds 必须是数字") from exc
    if timeout <= 0:
        raise ConfigError("runtime.timeout_seconds 必须大于 0")

    decoded: "dict[str, str]" = {}
    if app_id_was_decoded:
        decoded["app_id"] = app_id
    if secret_was_decoded:
        decoded["app_secret"] = app_secret
    if tapi_id_was_decoded:
        decoded["tapi_app_id"] = tapi_app_id
    if tapi_secret_was_decoded:
        decoded["tapi_app_secret"] = tapi_app_secret
    if decoded:
        _write_decoded_app_fields(path, source, decoded)
        print(
            "[配置] 已自动解码 {} 并写回配置文件：{}".format(
                "、".join("app." + field for field in decoded), path
            )
        )

    return Settings(
        accounts=accounts,
        app_id=app_id,
        app_secret=app_secret,
        base_url=base_url,
        user_agent=user_agent,
        zeeho_user_agent=zeeho_user_agent,
        timeout=timeout,
        use_proxy=_config_bool(runtime.get("use_proxy"), "runtime.use_proxy", False),
        notify=_config_bool(runtime.get("notify"), "runtime.notify", True),
        config_path=path,
        tapi_app_id=tapi_app_id,
        tapi_app_secret=tapi_app_secret,
        daily_tasks=_config_bool(
            runtime.get("daily_tasks"), "runtime.daily_tasks", True
        ),
        daily_post_content=str(runtime.get("daily_post_content") or "").strip(),
        blind_box=_config_bool(runtime.get("blind_box"), "runtime.blind_box", True),
        session_keepalive=_config_bool(
            runtime.get("session_keepalive"), "runtime.session_keepalive", True
        ),
        signin_mode=signin_mode,
    )
