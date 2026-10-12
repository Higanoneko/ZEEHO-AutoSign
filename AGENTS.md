# AGENTS.md

面向在本仓库改代码的 agent。README.md 是给用户看的完整文档；本文件只缓存
「代码里看不出来、改错会踩坑」的架构与约定，避免与 README 重复。

## 项目是什么

青龙面板每日自动签到「极核 ZEEHO」：签到走 H5 接口
（`h5.zeehoev.com` `/cfmotoservermine/signin`），每个请求带应用签名头 `Cfmoto-X-*`。
Python 3，依赖仅 `requests` + `PyYAML`。真实账号凭据在 `zeeho_config.yaml`
（已被 `.gitignore` 排除）：任何提交不得带入真实 token / user_id；抓包文件（含 Cookie、
设备标识）也在排除之列，不要为了举例把它们贴进文档或测试。

## 分层与依赖方向

`modules/` 内部单向依赖：`config ← zeeho ← app`；入口 `zeeho_auto_sign.py` 只调 `app.run()`。

| 文件 | 职责 | 备注 |
|---|---|---|
| `modules/config.py` | 配置读取/校验/原子写回 | 无外部请求；`Settings`/`Account` 是 frozen 值对象 |
| `modules/zeeho.py` | 签名与请求 | H5 签到 + App 域(tapi)每日任务都在这里；`ZeehoClient` 是薄的注入式 IO 适配器；纯函数都在模块顶层 |
| `modules/app.py` | 编排：逐账号 `keep_session_active` → `sign_in` → `claim_blind_box` → `daily_tasks` → `fetch_score`（领取/任务后重查余额）→ 汇总（连签/可用积分合计）→ 通知 | **全部展示文案都在这里**：`describe_session_keepalive` / `describe_signin` / `describe_blind_box` / `describe_task_step` / `describe_daily_result` / `describe_available_score` / `describe_total_score` / `final_score` / `account_lines` |
| `modules/notification.py` | 青龙原生 `notify.py` 的窄适配 | 本地缺失时打印跳过，不要加推送依赖 |

结果对象只描述事实、不带文案，渲染一律在 `app.py`：

- `SignInResult`：`ok / already_signed / error / code / gained_points / continue_days /
  available_score`；失败时只有 `error`（原因），没有可直接打印的整句。
- `DailyTaskResult`：`steps`（`TaskStep` 元组）+ `skipped` + `error` + `available_score`，
  `ok` 是 property（`error` 为空且每一步都 ok）。卡在准备阶段时 `steps` 为空、原因在
  `error`。`available_score` 由 `app.run()` 在整条任务链跑完后用
  `client.fetch_score()` 回填（`dataclasses.replace`），表示**任务积分已入账**的真实余额；
  None = 没开每日任务或那次查询失败。展示端一律用 `app.final_score(signin, daily)`：
  优先它、退回落，不要在别处再推算余额。
- 关闭每日任务时，领取盲盒后的余额由 `app.run()` 回填 `SignInResult.available_score`；
  查询失败保留签到阶段的旧余额，仍统一通过 `final_score` 展示。
- `TaskStep`：`key / ok / skipped / points / detail`，`key` 取 `STEP_POST` / `STEP_LIKE` /
  `STEP_SHARE` / `STEP_DELETE`；新增步骤时在 `app._STEP_LABELS` 补展示名。
- 失败原因统一经 `zeeho.stage_error_detail(error, stage)` 剥掉重复的阶段前缀，再交给
  app 层拼成「❌ 发帖失败：code=…」。

配置定位顺序见 `config.py` 的 `CONFIG_DIRECTORIES`：青龙新版 `/ql/data/config`、
旧版 `/ql/config`、本地项目根；找不到则生成模板并报错提示填写。

## 开发主旨：纯函数核心 + 注入式 IO

把逻辑先写成「输入 → 输出」的纯函数：不读全局、不写文件、不发请求、不取当前时间，
一切依赖从参数进。副作用收口在两侧：

- `ZeehoClient` 通过构造注入 `session` / `clock_ms` / `log`（测试可全部替身，线上默认见
  `__init__`）；请求收口在 `keep_session_active` / `sign_in` / `claim_blind_box` /
  `daily_tasks` 生命周期 + 收尾的只读 `fetch_score`（余额）里，`sign_in` 与 `daily_tasks`
  都遵循「预检 → 动作 → 复核」的
  幂等模式（签到看 `signStatue`；每日任务看服务端进度+自己的帖子）。
- `config.py` 允许的副作用只有「解码后原子写回配置文件」与提示打印，其余只读。

约定：

- 时间一律经 `clock_ms()` / `date.today()`（可注入 / 可 patch），不要散落 `time.time()`。
- 新增统计或分支优先写成模块级纯函数 + 对应单测，而不是往类里堆状态。
- 不引入模块级可变状态；消息文案不要在别处二次拼接。
- 日志永不打印 Authorization / Bearer / token / JSESSIONID 原文
  （`zeeho._response_excerpt` 负责脱敏，改动响应输出时保持这层）。
- 逻辑测试零网络：`session` 用 `Mock` 按 URL 分发响应；固定日期用
  `mock.patch("modules.zeeho.date", FakeDate)`（见 `tests/test_zeeho.py`）。

## 签名与接口速查（动网络代码必读）

- `sign = MD5(SHA1hex(data + param + app_secret))`，其中
  `param = appId=<id>&nonce=<uuid>&timestamp=<毫秒>`，
  `data` = 请求的查询串（按 key 排序）+ 请求体（如 `{}`）；
  三个头：`Cfmoto-X-Param` / `Cfmoto-X-Sign` / `Cfmoto-X-Sign-Type=0`。
- 每个请求现算 nonce / timestamp（动态时间戳）。服务端对 timestamp 有新鲜度窗口，
  **不要用抓包回放验证**：旧时间戳 → `HTTP 430 code=30123`；密钥不匹配 → `430/30121`；
  nonce 空或畸形 → `430/30000`。
- `app_id`/`app_secret` 是前端 JS 内置的应用凭据，请求头里只有签名结果、没有密钥。
  JS 内含 `VUE_APP_TEST/RELEASE/PRODUCTION_CONFIG` 三块，只认 **PRODUCTION**；
  配置支持直接粘贴 8/40 组 16 位二进制串，由脚本自动解码并写回
  （完整步骤见 README「🔑 教程」第 4 节）。
- 业务 code：`10000` 成功；`repeatedly_operation` 已签/操作过快（按成功处理）；
  `40000` 登录态或请求异常；`HTTP 401 invalid_token` = authorization 失效，需重新抓包。
- **鉴权只靠 `authorization`（bearer），代码里没有 Cookie 这一层**：抓包请求头里的
  `Cookie` 与鉴权无关，`Account` 不存该字段、`_headers()` 也不发该头。置空 Cookie 后签到
  与每日任务实测全通，Loon 版同样从不读 Cookie 头。所以刷新登录态只需换 `authorization`
  一个字段；抓包教程里不必再让用户复制那串 Cookie。
- `signStatue`：`3`/`5` = 已签（今天的 `integralScore` 即本次得分）；`4` = 过去未签；`2` = 未来占位。
- 可用积分来自 App 域 `tapi.zeehoev.com/v1.0/mine/cfmotoservermine/setting/{user_id}` 的
  `data.score`（`zeeho.SCORE_ENDPOINT`），与 H5 同一对密钥即可；只读，失败只跳过展示、不阻断签到。
  **余额只按事实取，不做推算**：任务行里的「积分+X」来自规则表 `integralNumber`，与余额无关。
  `sign_in` 里那次查询早于盲盒和每日任务，单看它会少掉本次奖励；所以每个账号在任务链
  跑完后由 `app.run()` 再查一次，报告末尾那行是这些最终余额的合计
  （`app.final_score` + `app.describe_total_score`，取不到时退回落、都没有则标「未取到」）。
- 连签天数以 App 域 `GET mine/cfmotoservermine/signin/count` 为准：`data.count` = 连续签到天数、
  `data.signStatus == 1` = 今天已签到（`signin_count_state` 解析，与 App 签到页同源）。
  `_streak` 只在它不可用时才退回 H5 明细推算，因此跨月补查请求只发生在兜底路径；
  `signStatus` 还用作签到后复核的兜底（明细复核失败但 signStatus 由 0 变 1 ⇒ 判定签到成功）。
- 签到 `POST /cfmotoservermine/signin` 的响应 `data.integralScore` 就是本次得分（抓包实测），
  `zeeho.signin_payload_points` 把它当兜底：明细复核失败时也能报告积分。
- `Zeeho-User-Agent` 中间的 UUID 每次请求都变，但服务端不校验，配置里写死即可。

## 会话保活约定（动 `keep_session_active` 必读）

- 保活是辅助操作：失败只进入日志/通知，不阻断后续任务，也不计入账号成功统计。
  `SessionKeepaliveResult.auth_expired` 只根据 HTTP 401/403 设置，先判断 HTTP 再解析 JSON；
  业务 `40000`、超时或令牌标称有效期均不能用来判定失效。
- 空体 POST 的签名数据为空串；仅 HTTP 200 + `10000` 成功，`repeatedly_operation`
  不当作成功。本次不重试，不回显响应体或异常原文；接口及配置说明见 README「会话保活」。

## 盲盒领取约定（动 `claim_blind_box` 必读）

- 资格只看 H5 `signin/info` 的 `data.signCount`，它与 App `signin/count` 的
  连签天数 `data.count` 不是同一个字段；缺失进度时报告失败，不能用连签天数替代。
- 领取接口虽为 GET，但会开奖：超时或失败时本次运行不重试；
  `repeatedly_operation` 可能是操作过快，不能像签到那样直接认作成功。
  下次运行重新读服务端进度；真实服务器是否立即重置进度仍需实测。

## 每日任务速查（动 `daily_tasks` / 社区接口必读）

- 走原生 App 域 `tapi.zeehoev.com/v1.0`，鉴权只用 `authorization: bearer …`（无需
  Cookie / user_id / H5 那堆头）；签名同 H5 规则，`data` 对 GET/DELETE 是排序查询串、
  对 POST 是**实际发送的原始 JSON body**。PUT/DELETE 空请求体带 `Content-Length: 0`。
- 四步链：`POST social/.../commonArticle`（发帖，响应只有 `data:true`，**不回帖 ID**）→
  重新 `GET social/.../community/mineArticleInfo?userId=` 定位新帖（复核）→
  `POST socialCommu/likeFavoriteInfo`（body `{"kindFlag":"0","postId":<id>}`）→
  `PUT article/share/<id>` + `GET mine/.../integral/adjustByShare`（后者失败仅提示）→
  `DELETE commonArticle/deleteArticle?articleId=<id>&postType=1`（清理，postType 取条目 `types`）。
- **绝不操作普通用户帖**：只识别「本人帖子列表里正文匹配 `^\d{1,2}月\d{1,2}日打卡$` 且
  `createDate` 前缀 == 今天」的条目；识别/清理相关纯函数在 `zeeho.py` 顶层
  （`classify_task_articles` 等），改匹配规则必须同步改单测。
- 幂等：三项任务今天是否已完成看 `GET mine/.../integral/getByUserId` 的
  `ps0003_01_002_VOList[]` 规则行（code 1023 发帖 / 1026 点赞 / 1024 分享）：
  `createDate` 属于今天 = 今天已领过该任务积分（App「积分任务」面板同源，三项通用），
  没有该字段时退回 `remarks` 里的 `(1/1)`（点赞/分享的每日上限进度）；解析在
  `daily_task_rules`，返回按步骤键索引的 `TaskRule(done_today, points)`，接口异常按未完成处理。
  发帖前先看 1023：App 端今天已发过帖就不再重复发。点赞前先看条目 `userLikeStatus`
  （已赞直接跳过，避免对同一帖子重复 POST 可能变成取消）。
- 抓包里这些社区请求签名用的是 App 内置密钥（`appId=S7qPWPU1`），与 H5 的
  `Sw5F9uJi` 不同；脚本默认复用 H5 密钥（与积分余额接口同域同规则已验证），需要时可配
  `app.tapi_app_id/tapi_app_secret` 覆盖（`Settings._app_credentials`）。
- 发帖正文由 `runtime.daily_post_content` 决定，留空按当天日期生成（`task_post_content`），
  改动默认正文模板 = 同时改 `_TASK_STYLE_RE` 与相关测试，别让两处失配。

## 测试与验证

- 跑全部测试：`python -m unittest discover -s tests -v`
  （Windows 下若 TEMP 目录不可写，先把 `$env:TEMP` 指到工作区内的目录再跑）。
- 报告/通知文案的用例在 `tests/test_app.py`（纯渲染，零网络；`RunTests` 用替身
  `ZeehoClient` 覆盖编排顺序——余额必须在 `daily_tasks` 之后查）；改 `app.py` 的文案必须
  同步改它，`format_results` 的整段输出在那里按行断言。
- 改请求头/字段前，先对照仓库根 `tapi.zeehoev.com_*.har` 里真实请求的结构；
  抓包教程、字段来源、常见错误对照表都以 `README.md` 为准，不要另造一套说法。
