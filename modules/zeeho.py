"""ZEEHO 签到请求模块。"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

import requests

from .config import Account, Settings

# signStatue ∈ {3,5} 视为当天已签到（5 常见于补签等场景）
_SIGNED_STATUES = frozenset({"3", "5"})

# 可用积分余额来自原生 App 端用户信息接口；签名凭据与 H5 同一套
SCORE_ENDPOINT = "https://tapi.zeehoev.com/v1.0/mine/cfmotoservermine/setting/{user_id}"
# App 端签到状态（连签天数 + 今日是否已签），与 App 签到页显示的是同一个数字
TAPI_SIGNIN_COUNT_PATH = "/v1.0/mine/cfmotoservermine/signin/count"
BLIND_BOX_PATH = "/cfmotoservermine/signin/supplementPrize"
BLIND_BOX_THRESHOLD = 30
SMART_SIGNIN_PATH = "/cfmotoservermine/signin"
SMART_SIGNING_DATA = "server_name=SMART"

# ---- 每日任务（发帖→喜欢→分享→删帖）：原生 App 域 tapi 接口 ----
TAPI_BASE = "https://tapi.zeehoev.com"
TAPI_KEEPALIVE_PATH = "/v1.0/app/cfmotoserverapp/app/scoreguiding/openApplication"
TAPI_MY_ARTICLES_PATH = (
    "/v1.0/social/cfmotoserversocial/community/mineArticleInfo"
)
TAPI_CREATE_ARTICLE_PATH = "/v1.0/social/cfmotoserversocial/commonArticle"
TAPI_LIKE_PATH = "/v1.0/social/cfmotoserversocial/socialCommu/likeFavoriteInfo"
TAPI_SHARE_PATH = "/v1.0/social/cfmotoserversocial/article/share/{article_id}"
TAPI_DELETE_ARTICLE_PATH = (
    "/v1.0/social/cfmotoserversocial/commonArticle/deleteArticle"
)
TAPI_INTEGRAL_PATH = "/v1.0/mine/cfmotoservermine/integral/getByUserId"
TAPI_ADJUST_SHARE_PATH = "/v1.0/mine/cfmotoservermine/integral/adjustByShare"

# getByUserId 规则表里的积分 code：点赞 1026 / 分享 1024 / 每日首次发帖 1023
TASK_LIKE_CODE = "1026"
TASK_SHARE_CODE = "1024"
TASK_POST_CODE = "1023"
# 每日任务链的步骤键：每一步都有独立结果，展示文案由 modules/app.py 渲染
STEP_POST = "post"
STEP_LIKE = "like"
STEP_SHARE = "share"
STEP_DELETE = "delete"
# 步骤 → getByUserId 规则表 code（删帖是清理动作，不产生积分）
TASK_STEP_CODES = {
    STEP_POST: TASK_POST_CODE,
    STEP_LIKE: TASK_LIKE_CODE,
    STEP_SHARE: TASK_SHARE_CODE,
}
# 规则表查不到对应任务时的单次奖励兜底（抓包实测三项均为 +1）
TASK_DEFAULT_POINTS = 1
# 帖子类型：1 = 普通贴（删帖接口的 postType 参数同值）
TASK_POST_TYPE = "1"
# 自动打卡帖正文的识别形状（只有长这样的本人帖子才会被清理/复用，
# 普通用户帖即便内容相近也不会被误删）
_TASK_STYLE_RE = re.compile(r"^\d{1,2}月\d{1,2}日打卡$")


@dataclass(frozen=True)
class SessionKeepaliveResult:
    """会话保活结果；只有服务器返回 401/403 时标记登录态被拒绝。"""

    ok: bool
    error: str = ""
    auth_expired: bool = False


@dataclass(frozen=True)
class SignInResult:
    """单个账号的签到结果；error 只在失败时给出原因，文案由 app 层渲染。"""

    account: Account
    ok: bool
    already_signed: bool
    error: str = ""
    code: str = ""
    gained_points: int = 0
    continue_days: int = 0
    available_score: Optional[int] = None


@dataclass(frozen=True)
class BlindBoxResult:
    """签到盲盒结果；sign_count 是 H5 奖励进度，独立于 App 连签天数。"""

    ok: bool
    skipped: bool = False
    sign_count: Optional[int] = None
    gained_points: int = 0
    prize_name: str = ""
    error: str = ""


@dataclass(frozen=True)
class TaskRule:
    """getByUserId 规则表里的一条每日任务规则。"""

    code: str
    points: int
    done_today: bool


@dataclass(frozen=True)
class SigninCountState:
    """App 端签到状态（signin/count）：连续签到天数与今天是否已签到。"""

    streak: int
    signed_today: bool


@dataclass(frozen=True)
class TaskStep:
    """每日任务链里的一步。

    skipped 表示「今天已经完成过、本次没有重复执行」；points 是这一步对应的
    单次积分奖励（规则表的 integralNumber），失败步骤为 0；detail 是失败原因或
    补充说明（如复用今日已有帖、清理了几篇残留）。
    """

    key: str
    ok: bool
    skipped: bool = False
    points: int = 0
    detail: str = ""


@dataclass(frozen=True)
class DailyTaskResult:
    """每日任务链结果：steps 为空表示卡在准备阶段（见 error）。

    available_score 是任务链跑完后重新查到的可用积分余额（由编排层在整条链结束
    后回填，`ZeehoClient.fetch_score`）；None 表示没查（未开每日任务）或查询失败。
    展示时优先用它，因为它已经包含本次任务赚到的积分。
    """

    account: Account
    steps: Tuple[TaskStep, ...] = ()
    skipped: bool = False
    error: str = ""
    available_score: Optional[int] = None

    @property
    def ok(self) -> bool:
        return not self.error and all(step.ok for step in self.steps)


def create_signature(
    app_id: str,
    app_secret: str,
    data: str = "",
    timestamp_ms: Optional[int] = None,
    nonce: Optional[str] = None,
) -> Dict[str, str]:
    """生成 ZEEHO H5 请求签名头；可注入时间和 nonce 便于验证。"""
    nonce_value = nonce or str(uuid.uuid4())
    timestamp_value = str(timestamp_ms or int(time.time() * 1000))
    param = "appId={}&nonce={}&timestamp={}".format(
        app_id, nonce_value, timestamp_value
    )
    sha1_value = hashlib.sha1(
        (data + param + app_secret).encode("utf-8")
    ).hexdigest()
    signature = hashlib.md5(sha1_value.encode("utf-8")).hexdigest()
    return {
        "Cfmoto-X-Param": param,
        "Cfmoto-X-Sign": signature,
        "Cfmoto-X-Sign-Type": "0",
    }


def _is_signed_entry(entry: Any) -> bool:
    """单条签到记录是否已签到。"""
    return isinstance(entry, dict) and str(entry.get("signStatue")) in _SIGNED_STATUES


def _previous_month(month: str) -> str:
    """返回上一个自然月，格式与入参一致（零填充到两位）。"""
    year, number = (int(part) for part in month.split("-"))
    if number == 1:
        year, number = year - 1, 12
    else:
        number -= 1
    return "{}-{:02d}".format(year, number)


def count_streak(entries: list, today: str, month: str) -> Tuple[int, bool]:
    """统计截止今天的连续签到天数（不含跨月延伸）。

    返回 (连续天数, 是否已数到月初、需要跨月补查上月)。
    entries 为某个月按日期升序的签到明细（nowSignDetailVos）。
    今天未签到或找不到今天的记录时返回 0。
    """
    today_index = next(
        (
            index
            for index, entry in enumerate(entries)
            if isinstance(entry, dict) and str(entry.get("createDate")) == today
        ),
        None,
    )
    if today_index is None or not _is_signed_entry(entries[today_index]):
        return 0, False

    streak = 0
    for index in range(today_index, -1, -1):
        if not _is_signed_entry(entries[index]):
            break
        streak += 1

    first_date = (
        str(entries[0].get("createDate"))
        if entries and isinstance(entries[0], dict)
        else ""
    )
    covers_month_start = streak == today_index + 1 and first_date == "{}-01".format(
        month
    )
    return streak, covers_month_start


def count_previous_month_tail(entries: list) -> int:
    """统计上一个月末尾的连续已签到天数（从月末往回数，遇断签停止）。"""
    count = 0
    for entry in reversed(entries):
        if _is_signed_entry(entry):
            count += 1
        else:
            break
    return count


def task_post_content(configured: str, today: date) -> str:
    """自动打卡帖正文：配置为空时按当天日期生成（形如「9月9日打卡」）。"""
    text = (configured or "").strip()
    return text or "{}月{}日打卡".format(today.month, today.day)


def build_publish_body(content: str) -> str:
    """发帖请求体（与抓包里 commonArticle 的字段结构一致，无图）。"""
    payload = {
        "activityId": "",
        "activitypostid": "",
        "addrPosition": "",
        "files": "[]",
        "location": "",
        "postSubInfo": None,
        "postcontent": content,
        "sendTos": "",
        "topicid": "",
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_like_body(post_id: str) -> str:
    """点赞请求体；kindFlag 0 = 点赞（1 = 收藏）。"""
    numeric = int(post_id) if str(post_id or "").isdigit() else post_id
    return json.dumps({"kindFlag": "0", "postId": numeric}, separators=(",", ":"))


def _entry_created_on(entry: Mapping[str, Any], date_prefix: str) -> bool:
    """mineArticleInfo 条目的 createDate（如 2026-09-09 12:03:05）是否属于指定日期。"""
    raw = entry.get("createDate")
    return str(raw or "")[:10] == date_prefix


def _is_task_style(entry: Mapping[str, Any]) -> bool:
    """条目正文是否为「打卡帖」形状（脚本可识别/清理的本人帖子）。"""
    body = str(entry.get("tcontent") or entry.get("postContent") or "")
    return bool(_TASK_STYLE_RE.match(body))


def article_id_of(entry: Optional[Mapping[str, Any]]) -> str:
    """帖子 ID（mineArticleInfo 用 tuuid，帖子详情用 uuid）。"""
    if not isinstance(entry, Mapping):
        return ""
    return str(entry.get("tuuid") or entry.get("uuid") or "").strip()


def article_post_type(entry: Optional[Mapping[str, Any]]) -> str:
    """删帖接口的 postType：取条目 types 字段，缺省按 1（普通贴）。"""
    if not isinstance(entry, Mapping):
        return TASK_POST_TYPE
    value = str(entry.get("types") or "").strip()
    return value or TASK_POST_TYPE


def is_article_liked(entry: Optional[Mapping[str, Any]]) -> bool:
    """帖子是否已被当前账号点赞（mineArticleInfo 的 userLikeStatus）。"""
    if not isinstance(entry, Mapping):
        return False
    return entry.get("userLikeStatus") in (1, "1")


def classify_task_articles(
    entries: list, today: date, content: str
) -> Tuple[Optional[Mapping[str, Any]], list]:
    """把「我的帖子」拆成 今天的打卡帖 与 可清理的旧打卡帖。

    只识别正文形如打卡帖的本人条目（绝不匹配普通用户帖）；同一天重复出现的
    打卡帖只保留第一条作为今日帖，其余归入待清理。
    """
    date_prefix = today.isoformat()
    today_post: Optional[Mapping[str, Any]] = None
    stale: list = []
    for entry in entries:
        if not isinstance(entry, Mapping) or not _is_task_style(entry):
            continue
        is_today = str(entry.get("tcontent") or "") == content and _entry_created_on(
            entry, date_prefix
        )
        if is_today and today_post is None:
            today_post = entry
        else:
            stale.append(entry)
    return today_post, stale


def _as_int(value: Any, default: int = 0) -> int:
    """把接口里可能是字符串的数字（如 integralScore "1"）转成 int。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def stage_error_detail(error: str, stage: str) -> str:
    """去掉错误文本里重复的阶段前缀，返回可直接拼在「XX失败：」后面的原因。

    `_tapi_request` / `_request_json` 返回的错误形如「<阶段>失败：code=…」或
    「<阶段>请求异常：ConnectionError…」；渲染层已经写了「❌ 发帖失败：」，
    因此这里把阶段名与「失败：」标签剥掉，只保留真正的异常原因。
    """
    text = str(error or "")
    for label in ("失败：", "请求异常："):
        prefix = stage + label
        if text.startswith(prefix):
            return text[len(prefix):] if label == "失败：" else text[len(stage):]
    return text


def daily_task_rules(
    payload: Optional[Mapping[str, Any]], today: date
) -> Dict[str, TaskRule]:
    """解析 getByUserId 规则表：每个每日任务今天是否已完成、单次奖励多少积分。

    完成判定与 App「积分任务」面板同源（抓包实测）：
    - 规则行的 `createDate` 属于今天 ⇒ 今天已经领到过该任务的积分（三个任务都适用，
      面板上也正是靠它显示「已完成」）；
    - 响应里没有 `createDate` 时退回备注文本里的 `(1/1)`（点赞/分享的每日上限进度）。
    接口异常或字段缺失时按「未完成」处理：宁可重复执行，也不漏做当天任务。
    """
    rules = {
        step: TaskRule(code, TASK_DEFAULT_POINTS, False)
        for step, code in TASK_STEP_CODES.items()
    }
    data = payload.get("data") if isinstance(payload, Mapping) else None
    rows = data.get("ps0003_01_002_VOList") if isinstance(data, Mapping) else None
    if not isinstance(rows, list):
        return rules

    date_prefix = today.isoformat()
    by_code = {rule.code: step for step, rule in rules.items()}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        step = by_code.get(str(row.get("code") or ""))
        if step is None:
            continue
        done_today = str(row.get("createDate") or "")[:10] == date_prefix or (
            "(1/1)" in str(row.get("remarks") or "")
        )
        rules[step] = TaskRule(
            rules[step].code,
            _as_int(row.get("integralNumber"), TASK_DEFAULT_POINTS)
            or TASK_DEFAULT_POINTS,
            done_today,
        )
    return rules


def signin_count_state(
    payload: Optional[Mapping[str, Any]],
) -> Optional[SigninCountState]:
    """解析 App 端 `signin/count`：`count` = 连续签到天数，`signStatus` = 1 表示今天已签到。

    抓包里该接口返回 `{"count":4,"signStatus":1}`，与 App 签到页显示的连签数字一致，
    因此连签以它为准（H5 明细推算只作为兜底）。响应缺字段或不是成功响应时返回 None。
    """
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping) or data.get("count") is None:
        return None
    return SigninCountState(
        streak=_as_int(data.get("count")),
        signed_today=_as_int(data.get("signStatus")) == 1,
    )


def signin_payload_points(payload: Optional[Mapping[str, Any]]) -> int:
    """签到接口响应里直接带出的本次积分（data.integralScore，抓包实测有值）。

    有了它，即使随后的签到明细复核请求失败，也能把本次得分报告出来。
    """
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        return 0
    return _as_int(data.get("integralScore"))


def blind_box_sign_count(payload: Optional[Mapping[str, Any]]) -> Optional[int]:
    """解析 H5 盲盒进度；缺失或畸形时返回 None，不能用连签天数代替。"""
    data = payload.get("data") if isinstance(payload, Mapping) else None
    raw = data.get("signCount") if isinstance(data, Mapping) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def blind_box_reward(payload: Mapping[str, Any]) -> Tuple[int, str]:
    """读取实际奖励；兼容参考实现的 integral / integralScore 两种字段。"""
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return 0, ""
    points = _as_int(data.get("integral") or data.get("integralScore"))
    return points, str(data.get("prizesName") or "")


def smart_signin_state(
    payload: Optional[Mapping[str, Any]], today: str
) -> Optional[SigninCountState]:
    """SMART 状态来自 lastTime / continueDays；缺失或畸形时不授权签到动作。"""
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping) or "lastTime" not in data:
        return None
    raw_count = data.get("continueDays")
    if isinstance(raw_count, bool) or not isinstance(raw_count, (int, str)):
        return None
    try:
        count = int(raw_count)
    except ValueError:
        return None
    if count < 0:
        return None
    last_time = data.get("lastTime")
    if last_time in (None, ""):
        return SigninCountState(count, False) if count == 0 else None
    if not isinstance(last_time, str):
        return None
    last_day = last_time[:10]
    try:
        if date.fromisoformat(last_day).isoformat() != last_day:
            return None
    except ValueError:
        return None
    if last_day > today:
        return None
    return SigninCountState(count, last_day == today)


def session_keepalive_result(status: int, payload: Any) -> SessionKeepaliveResult:
    """按实际响应判定保活结果；不按令牌的标称有效期推断失效。"""
    if status != 200:
        return SessionKeepaliveResult(
            False, error="HTTP {}".format(status), auth_expired=status in {401, 403}
        )
    if not isinstance(payload, Mapping):
        return SessionKeepaliveResult(False, error="响应 JSON 顶层不是对象")
    code = str(payload.get("code") or "")
    if code != "10000":
        # 不回显响应体/服务端 message，避免登录态出现在日志和通知中。
        return SessionKeepaliveResult(False, error="code={}".format(code or "<空>"))
    return SessionKeepaliveResult(True)


class ZeehoClient:
    """一个账号一次签到所需的完整请求 interface。"""

    def __init__(
        self,
        settings: Settings,
        session: Optional[requests.Session] = None,
        clock_ms: Optional[Callable[[], int]] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.session.trust_env = settings.use_proxy
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.log = log or (lambda _message: None)

    def _headers(
        self, account: Account, signing_data: str = "", empty_body: bool = False
    ) -> Dict[str, str]:
        now = self.clock_ms()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN",
            "Authorization": account.authorization,
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": self.settings.base_url,
            "Referer": "{}/activity/signin?hideNavigationBar=true&timeStamp={}".format(
                self.settings.base_url, now
            ),
            "User-Agent": self.settings.user_agent,
            "Zeeho-User-Agent": self.settings.zeeho_user_agent,
            "X-Requested-With": "com.cfmoto",
            "interfaceversion": "2",
            "user_id": account.user_id,
        }
        if empty_body:
            headers["Content-Length"] = "0"
        headers.update(
            create_signature(
                self.settings.app_id,
                self.settings.app_secret,
                data=signing_data,
                timestamp_ms=now,
            )
        )
        return headers

    @staticmethod
    def _response_excerpt(response: requests.Response) -> str:
        """截取并脱敏响应，防止诊断日志意外回显登录态。"""
        excerpt = " ".join(response.text.split())[:500]
        excerpt = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._-]+", "Bearer <REDACTED>", excerpt)
        excerpt = re.sub(
            r"(?i)(JSESSIONID[=:])[^;\s\"']+", r"\1<REDACTED>", excerpt
        )
        return excerpt

    def _request_json(
        self,
        stage: str,
        method: str,
        path: str,
        headers: Mapping[str, str],
        params: Optional[Mapping[str, str]] = None,
        data: Optional[bytes] = None,
    ) -> Tuple[Optional[requests.Response], Optional[Mapping[str, Any]], str]:
        query_hint = ""
        if params:
            query_hint = "?" + "&".join(
                "{}={}".format(key, params[key]) for key in sorted(params)
            )
        self.log("[请求] {} | {} {}{}".format(stage, method, path, query_hint))
        try:
            request = self.session.get if method == "GET" else self.session.post
            kwargs: Dict[str, Any] = {
                "headers": dict(headers),
                "timeout": self.settings.timeout,
            }
            if params:
                kwargs["params"] = dict(params)
            if method != "GET":
                kwargs["data"] = data if data is not None else b""
            response = request(self.settings.base_url + path, **kwargs)
        except requests.RequestException as exc:
            detail = "{}请求异常：{}: {}".format(stage, type(exc).__name__, exc)
            self.log("[错误] " + detail)
            return None, None, detail

        _, payload, error = self._parse_json(stage, response)
        return response, payload, error

    @staticmethod
    def _month_entries(payload: Optional[Mapping[str, Any]]) -> list:
        """取某月签到明细列表（nowSignDetailVos），异常时返回空列表。"""
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            return []
        entries = data.get("nowSignDetailVos")
        return entries if isinstance(entries, list) else []

    @staticmethod
    def _entry_on(entries: list, target_date: str) -> Optional[Mapping[str, Any]]:
        """在明细列表里找指定日期的那一条。"""
        for entry in entries:
            if isinstance(entry, Mapping) and str(entry.get("createDate")) == target_date:
                return entry
        return None

    def fetch_score(self, account: Account) -> Optional[int]:
        """查询账号可用积分余额（App 端用户信息接口，优先原生 App 密钥）。

        只读接口，失败不影响签到结果，返回 None 表示未取到。
        """
        headers = self._tapi_headers(account, json_body=True)
        headers.update({"interfaceversion": "2", "user_id": account.user_id})
        try:
            response = self.session.get(
                SCORE_ENDPOINT.format(user_id=account.user_id),
                headers=headers,
                timeout=self.settings.timeout,
            )
        except requests.RequestException as exc:
            self.log("[提示] 可用积分查询请求异常：{}".format(type(exc).__name__))
            return None
        if response.status_code != 200:
            self.log("[提示] 可用积分查询失败：HTTP {}".format(response.status_code))
            return None
        try:
            payload = response.json()
        except ValueError:
            self.log("[提示] 可用积分查询响应不是有效 JSON")
            return None
        if not isinstance(payload, Mapping) or str(payload.get("code") or "") != "10000":
            self.log("[提示] 可用积分查询未返回成功")
            return None
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return None
        raw = data.get("score")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def _parse_json(
        self, stage: str, response: requests.Response
    ) -> Tuple[Optional[Mapping[str, Any]], Optional[Mapping[str, Any]], str]:
        """解析 JSON 响应并记录日志，返回 (payload, payload, error)。

        业务 code 非 10000 时只记日志不改返回值（HTTP >= 400 才视为传输错误），
        由调用方按接口语义判断 code。
        """
        try:
            parsed = response.json()
        except (ValueError, TypeError):
            excerpt = self._response_excerpt(response)
            detail = "{}响应不是有效 JSON（HTTP {}）".format(
                stage, response.status_code
            )
            if excerpt:
                detail += "；响应：{}".format(excerpt)
            self.log("[错误] " + detail)
            return None, None, detail
        if not isinstance(parsed, dict):
            detail = "{}响应 JSON 顶层不是对象（HTTP {}）".format(
                stage, response.status_code
            )
            self.log("[错误] " + detail)
            return None, None, detail

        payload: Mapping[str, Any] = parsed
        code = str(payload.get("code") or "")
        message = str(payload.get("message") or payload.get("msg") or "")
        self.log(
            "[响应] {} | HTTP {} | code={} | message={}".format(
                stage, response.status_code, code or "<空>", message or "<空>"
            )
        )
        if code and code not in {"10000", "repeatedly_operation"}:
            excerpt = self._response_excerpt(response)
            if excerpt:
                self.log("[响应正文] {} | {}".format(stage, excerpt))
        if response.status_code >= 400:
            excerpt = self._response_excerpt(response)
            detail = "{}失败：HTTP {}，code={}，message={}".format(
                stage,
                response.status_code,
                code or "<空>",
                message or "<空>",
            )
            if excerpt:
                detail += "；响应：{}".format(excerpt)
            self.log("[错误] " + detail)
            return payload, payload, detail
        return payload, payload, ""

    def _app_credentials(self) -> Tuple[str, str]:
        """App 域签名密钥对：配置了 tapi 专用密钥就用它，否则回退 H5 密钥。"""
        if self.settings.tapi_app_id and self.settings.tapi_app_secret:
            return self.settings.tapi_app_id, self.settings.tapi_app_secret
        return self.settings.app_id, self.settings.app_secret

    def _tapi_headers(
        self,
        account: Account,
        signing_data: str = "",
        json_body: bool = False,
        empty_body: bool = False,
    ) -> Dict[str, str]:
        """App 域请求头：仅带鉴权 + 签名，与可用积分查询同一套最小头部。"""
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN",
            "Authorization": account.authorization,
        }
        if json_body:
            headers["Content-Type"] = "application/json; charset=UTF-8"
        if empty_body:
            headers["Content-Length"] = "0"
        app_id, app_secret = self._app_credentials()
        headers.update(
            create_signature(
                app_id, app_secret, data=signing_data, timestamp_ms=self.clock_ms()
            )
        )
        return headers

    def _tapi_request(
        self,
        stage: str,
        method: str,
        url: str,
        account: Account,
        params: Optional[Mapping[str, str]] = None,
        body: Optional[str] = None,
        empty_body: bool = False,
    ) -> Tuple[Optional[Mapping[str, Any]], str]:
        """发送一条 App 域签名请求并校验业务 code（10000 / repeatedly_operation 视为成功）。

        返回 (payload, error)；error 为空表示该步成功。签名 data = 排序后的查询串 + 请求体，
        与 H5 接口同一套规则，时间戳/随机数每次现算。
        """
        signing_data = ""
        if params:
            signing_data = "&".join(
                "{}={}".format(key, params[key]) for key in sorted(params)
            )
        if body is not None:
            signing_data += body
        headers = self._tapi_headers(
            account,
            signing_data=signing_data,
            json_body=body is not None,
            empty_body=empty_body,
        )
        query_hint = ""
        if params:
            query_hint = "?" + "&".join(
                "{}={}".format(key, params[key]) for key in sorted(params)
            )
        self.log("[请求] {} | {} {}{}".format(stage, method, url, query_hint))
        try:
            kwargs: Dict[str, Any] = {
                "headers": dict(headers),
                "timeout": self.settings.timeout,
            }
            if params:
                kwargs["params"] = dict(params)
            kwargs["data"] = body.encode("utf-8") if body is not None else b""
            response = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            detail = "{}请求异常：{}: {}".format(stage, type(exc).__name__, exc)
            self.log("[错误] " + detail)
            return None, detail

        _, payload, error = self._parse_json(stage, response)
        if error:
            return payload, error
        code = str((payload or {}).get("code") or "")
        message = str(
            (payload or {}).get("message") or (payload or {}).get("msg") or ""
        )
        if code not in {"10000", "repeatedly_operation"}:
            detail = "{}失败：code={}，message={}".format(
                stage, code or "<空>", message or "<空>"
            )
            return payload, detail
        return payload, ""

    def keep_session_active(self, account: Account) -> SessionKeepaliveResult:
        """模拟 App 前台保活，发送一次空体 POST；失败交给下次任务再尝试。"""
        headers = self._tapi_headers(account, json_body=True, empty_body=True)
        headers.update({"Accept": "*/*", "User-Agent": "okhttp/4.9.2"})
        try:
            response = self.session.post(
                TAPI_BASE + TAPI_KEEPALIVE_PATH,
                headers=headers,
                data=b"",
                timeout=self.settings.timeout,
            )
        except requests.RequestException as exc:
            return SessionKeepaliveResult(False, error=type(exc).__name__)
        # HTTP 状态优先于 JSON 解析，包括返回 HTML/空体的 401/403。
        if response.status_code != 200:
            return session_keepalive_result(response.status_code, None)
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return SessionKeepaliveResult(False, error="响应不是有效 JSON")
        return session_keepalive_result(response.status_code, payload)

    def _my_articles(self, account: Account, stage: str) -> Tuple[Optional[list], str]:
        """查询自己的帖子列表（mineArticleInfo）。失败返回 (None, error)。"""
        payload, error = self._tapi_request(
            stage,
            "GET",
            TAPI_BASE + TAPI_MY_ARTICLES_PATH,
            account,
            params={"userId": account.user_id},
        )
        if error:
            return None, error
        data = payload.get("data")
        if not isinstance(data, list):
            return None, "{}失败：data 不是帖子列表".format(stage)
        return data, ""

    def daily_tasks(self, account: Account) -> DailyTaskResult:
        """执行每日任务链：发帖 → 喜欢 → 分享 → 删帖（前 3 步各 +1 积分）。

        只操作脚本自己发布的「打卡帖」（正文/日期双重识别），不触碰用户的真实
        帖子；每一步都会留下独立结果（是否执行、积分多少、失败原因），当天任务
        已全部完成时整链跳过；发帖后崩溃残留的打卡帖会在下次运行自动补做并清理。
        """
        today = date.today()
        content = task_post_content(self.settings.daily_post_content, today)

        articles, error = self._my_articles(account, "每日任务-查询我的帖子")
        if error:
            return DailyTaskResult(account, error=error)
        today_post, stale = classify_task_articles(articles, today, content)

        # 今日各任务进度（只读；失败一律按未完成处理，绝不漏跑当天任务）
        progress_payload, progress_error = self._tapi_request(
            "每日任务-查询进度",
            "GET",
            TAPI_BASE + TAPI_INTEGRAL_PATH,
            account,
            params={"userId": account.user_id},
        )
        if progress_error:
            self.log("[提示] " + progress_error + "，本次按未完成执行")
            progress_payload = None
        rules = daily_task_rules(progress_payload, today)

        steps: List[TaskStep] = []

        # 清理历史残留打卡帖（崩溃遗留，或已过期的旧打卡帖）
        cleaned = 0
        cleanup_errors: List[str] = []
        for entry in stale:
            post_id = article_id_of(entry)
            if not post_id:
                continue
            _, delete_error = self._tapi_request(
                "每日任务-清理残留帖",
                "DELETE",
                TAPI_BASE + TAPI_DELETE_ARTICLE_PATH,
                account,
                params={"articleId": post_id, "postType": article_post_type(entry)},
                empty_body=True,
            )
            if delete_error:
                cleanup_errors.append(
                    stage_error_detail(delete_error, "每日任务-清理残留帖")
                )
            else:
                cleaned += 1

        # 发帖：今日已有打卡帖（上一轮没跑完）或 App 端今天已经发过帖，都不重复发
        post_rule = rules[STEP_POST]
        if today_post is not None:
            steps.append(
                TaskStep(STEP_POST, True, True, post_rule.points, "复用今日已有帖")
            )
        elif post_rule.done_today:
            steps.append(
                TaskStep(STEP_POST, True, True, post_rule.points, "App 端今日已发帖")
            )
        else:
            _, publish_error = self._tapi_request(
                "每日任务-发帖",
                "POST",
                TAPI_BASE + TAPI_CREATE_ARTICLE_PATH,
                account,
                body=build_publish_body(content),
            )
            if publish_error:
                steps.append(
                    TaskStep(
                        STEP_POST,
                        False,
                        detail=stage_error_detail(publish_error, "每日任务-发帖"),
                    )
                )
                return DailyTaskResult(account, tuple(steps))
            # 复核：发帖接口不回帖 ID，需重新拉列表定位今天的打卡帖
            confirmed, confirm_error = self._my_articles(
                account, "每日任务-发帖复核"
            )
            if confirm_error:
                steps.append(
                    TaskStep(
                        STEP_POST,
                        False,
                        detail="发帖成功但{}（残留帖会在下次运行自动清理）".format(
                            stage_error_detail(confirm_error, "每日任务-发帖复核")
                        ),
                    )
                )
                return DailyTaskResult(account, tuple(steps))
            today_post, _ = classify_task_articles(confirmed, today, content)
            if today_post is None:
                steps.append(
                    TaskStep(
                        STEP_POST,
                        False,
                        detail="发帖成功但复核未找到今日打卡帖（残留帖会在下次运行自动清理）",
                    )
                )
                return DailyTaskResult(account, tuple(steps))
            steps.append(TaskStep(STEP_POST, True, points=post_rule.points))

        post_id = article_id_of(today_post)

        # 喜欢（当天进度已完成、或帖子已被赞都直接跳过：重复点赞会变成取消）
        like_rule = rules[STEP_LIKE]
        if like_rule.done_today:
            steps.append(TaskStep(STEP_LIKE, True, True, like_rule.points))
        elif is_article_liked(today_post):
            steps.append(
                TaskStep(STEP_LIKE, True, True, like_rule.points, "帖子已赞")
            )
        else:
            _, like_error = self._tapi_request(
                "每日任务-点赞",
                "POST",
                TAPI_BASE + TAPI_LIKE_PATH,
                account,
                body=build_like_body(post_id),
            )
            if like_error:
                steps.append(
                    TaskStep(
                        STEP_LIKE,
                        False,
                        detail=stage_error_detail(like_error, "每日任务-点赞"),
                    )
                )
            else:
                steps.append(TaskStep(STEP_LIKE, True, points=like_rule.points))

        # 分享（当天分享过就跳过；分享后同步一次积分调整，失败仅提示）
        share_rule = rules[STEP_SHARE]
        if share_rule.done_today:
            steps.append(TaskStep(STEP_SHARE, True, True, share_rule.points))
        else:
            _, share_error = self._tapi_request(
                "每日任务-分享",
                "PUT",
                TAPI_BASE + TAPI_SHARE_PATH.format(article_id=post_id),
                account,
                empty_body=True,
            )
            if share_error:
                steps.append(
                    TaskStep(
                        STEP_SHARE,
                        False,
                        detail=stage_error_detail(share_error, "每日任务-分享"),
                    )
                )
            else:
                steps.append(TaskStep(STEP_SHARE, True, points=share_rule.points))
                _, adjust_error = self._tapi_request(
                    "每日任务-分享积分调整",
                    "GET",
                    TAPI_BASE + TAPI_ADJUST_SHARE_PATH,
                    account,
                )
                if adjust_error:
                    self.log("[提示] " + adjust_error)

        # 删帖：清理本次的打卡帖，方便账号正常使用（并汇总残留清理结果）
        delete_errors: List[str] = []
        deleted_today = False
        if today_post is not None:
            _, delete_error = self._tapi_request(
                "每日任务-删帖",
                "DELETE",
                TAPI_BASE + TAPI_DELETE_ARTICLE_PATH,
                account,
                params={
                    "articleId": post_id,
                    "postType": article_post_type(today_post),
                },
                empty_body=True,
            )
            if delete_error:
                delete_errors.append(stage_error_detail(delete_error, "每日任务-删帖"))
            else:
                deleted_today = True
        delete_errors.extend(cleanup_errors)
        notes = list(delete_errors)
        if cleaned:
            notes.append("清理残留 {} 篇".format(cleaned))
        steps.append(
            TaskStep(
                STEP_DELETE,
                ok=not delete_errors,
                skipped=not deleted_today,
                detail="；".join(notes),
            )
        )

        return DailyTaskResult(
            account, tuple(steps), all(step.skipped for step in steps)
        )

    def _signin_count(
        self, account: Account, stage: str
    ) -> Optional[SigninCountState]:
        """查询 App 端连签天数与今日签到状态（只读；失败返回 None，不影响签到）。"""
        payload, error = self._tapi_request(
            stage, "GET", TAPI_BASE + TAPI_SIGNIN_COUNT_PATH, account
        )
        if error:
            self.log("[提示] " + error + "，连签天数改用签到明细推算")
            return None
        state = signin_count_state(payload)
        if state is None:
            self.log(
                "[提示] {}未返回 count，连签天数改用签到明细推算".format(stage)
            )
        return state

    def _streak(
        self,
        account: Account,
        month: str,
        today: str,
        entries: list,
        app_state: Optional[SigninCountState] = None,
    ) -> int:
        """连签天数：优先用 App 端 `signin/count`（与 App 显示一致），否则用 H5 明细推算。

        H5 明细推算遇到「已数到本月 1 号」时会补查上一个月继续累计。
        """
        if app_state is not None:
            return app_state.streak

        streak, crosses_month = count_streak(entries, today, month)
        if not crosses_month:
            return streak
        previous = _previous_month(month)
        payload, error = self._sign_info(account, previous, "跨月连签查询")
        if error:
            return streak
        return streak + count_previous_month_tail(self._month_entries(payload))

    def _signed_stats(
        self,
        account: Account,
        month: str,
        today: str,
        entries: list,
        app_state: Optional[SigninCountState] = None,
    ) -> Tuple[int, int, Optional[int]]:
        """账号今天已签到的前提下，汇总本次积分/连签天数/可用积分。"""
        today_entry = self._entry_on(entries, today)
        gained = int(today_entry.get("integralScore") or 0) if today_entry else 0
        streak = self._streak(account, month, today, entries, app_state)
        score = self.fetch_score(account)
        return gained, streak, score

    def _sign_info(
        self, account: Account, month: str, stage: str
    ) -> Tuple[Optional[Mapping[str, Any]], str]:
        query = "month={}".format(month)
        _, payload, error = self._request_json(
            stage,
            "GET",
            "/cfmotoservermine/signin/info",
            self._headers(account, signing_data=query),
            params={"month": month},
        )
        if error:
            return payload, error
        if payload is None or str(payload.get("code") or "") != "10000":
            code = str((payload or {}).get("code") or "<空>")
            message = str(
                (payload or {}).get("message") or (payload or {}).get("msg") or "<空>"
            )
            return payload, "{}失败：code={}，message={}".format(stage, code, message)
        return payload, ""

    def claim_blind_box(self, account: Account) -> BlindBoxResult:
        """查询服务端奖励进度后领取一次；失败不重试，避免重复开奖。"""
        today = date.today().isoformat()
        payload, error = self._sign_info(account, today[:7], "盲盒资格查询")
        if error:
            return BlindBoxResult(
                False, error=stage_error_detail(error, "盲盒资格查询")
            )
        sign_count = blind_box_sign_count(payload)
        if sign_count is None:
            return BlindBoxResult(False, error="签到信息缺少有效的 signCount")
        if sign_count < BLIND_BOX_THRESHOLD:
            return BlindBoxResult(True, skipped=True, sign_count=sign_count)

        _, reward, error = self._request_json(
            "盲盒领取",
            "GET",
            BLIND_BOX_PATH,
            self._headers(account, signing_data="supplementDate=" + today),
            params={"supplementDate": today},
        )
        if error:
            return BlindBoxResult(
                False,
                sign_count=sign_count,
                error=stage_error_detail(error, "盲盒领取"),
            )
        code = str((reward or {}).get("code") or "")
        if code != "10000":
            message = str(
                (reward or {}).get("message") or (reward or {}).get("msg") or "<空>"
            )
            return BlindBoxResult(
                False,
                sign_count=sign_count,
                error="code={}，message={}".format(code or "<空>", message),
            )
        points, name = blind_box_reward(reward)
        return BlindBoxResult(
            True,
            sign_count=sign_count,
            gained_points=points,
            prize_name=name,
        )

    def sign_in(self, account: Account) -> SignInResult:
        if self.settings.signin_mode == "smart":
            return self._sign_in_smart(account)
        today = date.today().isoformat()
        month = today[:7]

        # 预检之一：App 端签到状态（连签天数 + 今天是否已签；只读，失败只降级、不阻断）
        app_state = self._signin_count(account, "App 签到状态查询")

        before, error = self._sign_info(account, month, "签到前查询")
        if error:
            return SignInResult(
                account,
                False,
                False,
                "状态查询失败：{}".format(
                    stage_error_detail(error, "签到前查询")
                ),
            )
        before_entries = self._month_entries(before)
        before_entry = self._entry_on(before_entries, today)
        if _is_signed_entry(before_entry):
            gained, streak, score = self._signed_stats(
                account, month, today, before_entries, app_state
            )
            return SignInResult(
                account,
                True,
                True,
                code="10000",
                gained_points=gained,
                continue_days=streak,
                available_score=score,
            )

        _, payload, error = self._request_json(
            "执行签到",
            "POST",
            "/cfmotoservermine/signin",
            self._headers(account, empty_body=True),
            data=b"",
        )
        if error:
            return SignInResult(
                account,
                False,
                False,
                "执行签到失败：{}".format(stage_error_detail(error, "执行签到")),
            )

        code = str(payload.get("code") or "")
        message = str(payload.get("message") or payload.get("msg") or "")
        # 签到接口响应里直接带出本次积分（抓包实测 data.integralScore），
        # 明细复核请求失败时也能把它报告出来
        posted_points = signin_payload_points(payload)
        if code == "repeatedly_operation":
            # 可能操作过快或另一处已签：补一次确认，带出今日积分与连签信息
            gained, streak, score = posted_points, 0, None
            confirm_payload, confirm_error = self._sign_info(
                account, month, "重复签到确认"
            )
            if not confirm_error:
                confirm_entries = self._month_entries(confirm_payload)
                if _is_signed_entry(self._entry_on(confirm_entries, today)):
                    gained, streak, score = self._signed_stats(
                        account, month, today, confirm_entries, app_state
                    )
                    gained = gained or posted_points
            if not streak and app_state is not None:
                streak = app_state.streak
            if score is None:
                score = self.fetch_score(account)
            return SignInResult(
                account,
                True,
                True,
                code=code,
                gained_points=gained,
                continue_days=streak,
                available_score=score,
            )
        if code != "10000":
            return SignInResult(
                account,
                False,
                False,
                "签到接口返回 code={}，message={}".format(
                    code or "<空>", message or "<空>"
                ),
                code,
            )

        after, error = self._sign_info(account, month, "签到后确认")
        if error:
            # 明细复核失败时用 App 端 signStatus 兜底确认（本次签到前它还是未签状态）
            fallback = self._signin_count(account, "App 签到复核")
            if (
                fallback is not None
                and fallback.signed_today
                and (app_state is None or not app_state.signed_today)
            ):
                return SignInResult(
                    account,
                    True,
                    False,
                    code=code,
                    gained_points=posted_points,
                    continue_days=fallback.streak,
                    available_score=self.fetch_score(account),
                )
            return SignInResult(
                account,
                False,
                False,
                "签到接口返回成功{}，但{}".format(
                    "（本次积分 +{}）".format(posted_points) if posted_points else "",
                    error,
                ),
                code,
                gained_points=posted_points,
            )
        after_entries = self._month_entries(after)
        if not _is_signed_entry(self._entry_on(after_entries, today)):
            return SignInResult(
                account,
                False,
                False,
                "签到接口返回成功，但签到后记录未确认今日已签到",
                code,
                gained_points=posted_points,
            )
        gained, streak, score = self._signed_stats(
            account, month, today, after_entries, app_state
        )
        return SignInResult(
            account,
            True,
            False,
            code=code,
            gained_points=gained or posted_points,
            continue_days=streak,
            available_score=score,
        )

    def _smart_headers(self, account: Account, empty_body: bool = False) -> Dict[str, str]:
        """SMART 仍走 H5 域，但使用原生 App 密钥并签入 server_name 查询串。"""
        headers = self._tapi_headers(
            account, signing_data=SMART_SIGNING_DATA, empty_body=empty_body
        )
        headers.update({"User-Agent": self.settings.user_agent, "user_id": account.user_id})
        return headers

    def _smart_state(
        self, account: Account, today: str, stage: str
    ) -> Tuple[Optional[SigninCountState], str]:
        _, payload, error = self._request_json(
            stage, "GET", SMART_SIGNIN_PATH, self._smart_headers(account),
            params={"server_name": "SMART"},
        )
        if error:
            return None, stage_error_detail(error, stage)
        code = str((payload or {}).get("code") or "")
        if code != "10000":
            return None, "code={}".format(code or "<空>")
        state = smart_signin_state(payload, today)
        if state is None:
            return None, "签到状态缺少有效的 lastTime / continueDays"
        return state, ""

    def _sign_in_smart(self, account: Account) -> SignInResult:
        """SMART：只读预检 → 最多一次空体 POST → 只读复核；不切换后端重试。"""
        if not (self.settings.tapi_app_id and self.settings.tapi_app_secret):
            return SignInResult(account, False, False, "SMART 签到缺少原生 App 密钥对")
        today = date.today().isoformat()
        before, error = self._smart_state(account, today, "SMART 签到前查询")
        if error:
            return SignInResult(account, False, False, "状态查询失败：" + error)
        if before.signed_today:
            return SignInResult(
                account, True, True, code="10000", continue_days=before.streak,
                available_score=self.fetch_score(account),
            )
        _, payload, error = self._request_json(
            "SMART 执行签到", "POST", SMART_SIGNIN_PATH,
            self._smart_headers(account, empty_body=True),
            params={"server_name": "SMART"}, data=b"",
        )
        if error:
            return SignInResult(
                account, False, False,
                "执行签到失败：" + stage_error_detail(error, "SMART 执行签到"),
            )
        code = str((payload or {}).get("code") or "")
        points = signin_payload_points(payload)
        if code not in {"10000", "repeatedly_operation"}:
            return SignInResult(
                account, False, False,
                "签到接口返回 code=" + (code or "<空>"), code,
            )
        after, error = self._smart_state(account, today, "SMART 签到后确认")
        if error or not after.signed_today:
            return SignInResult(
                account, False, False,
                "签到后未确认今日已签到：" + (error or "lastTime 不是今天"),
                code=code, gained_points=points,
            )
        data = payload.get("data")
        already = code == "repeatedly_operation" or (
            isinstance(data, Mapping) and str(data.get("signInStatus")) == "1"
        )
        return SignInResult(
            account, True, already, code=code, gained_points=points,
            continue_days=after.streak, available_score=self.fetch_score(account),
        )
