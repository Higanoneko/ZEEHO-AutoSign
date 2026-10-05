# ZEEHO Auto Sign

面向青龙面板的 ZEEHO（极核）自动签到脚本。项目主目录只有一个 Python 任务入口
`zeeho_auto_sign.py`，账号配置、请求签名、运行编排和青龙通知均放在独立模块中。

签到走的是 H5 接口（`https://h5.zeehoev.com/cfmotoservermine/signin`），与 Loon 脚本
用的是同一套接口和签名机制，抓包填写配置后，脚本就能在电脑或青龙上独立运行。
手机端登录续期的说明见下文。签到完成后会自动查询并领取已解锁的签到盲盒，
再执行 App 社区的「每日任务」
（发帖 → 喜欢 → 分享 → 删帖，前 3 步各 +1 积分，删帖做清理，详见下文）。

## 青龙部署

订阅拉取本仓库后添加定时任务：

```text
task zeeho_auto_sign.py
```

例如每天 08:10 执行的 cron 为 `10 8 * * *`。

脚本依赖：

```text
requests
pyyaml
```

可以在青龙“依赖管理”的 Python3 类型中安装，或执行：

```bash
pip3 install -r requirements.txt
```

## 配置文件

脚本不使用环境变量保存账号。首次运行会自动创建 `zeeho_config.yaml`：

1. 新版青龙优先创建在 `/ql/data/config/zeeho_config.yaml`。
2. 旧版青龙使用 `/ql/config/zeeho_config.yaml`。
3. 本地运行时创建在项目主目录。

首次运行后，在青龙面板左侧“配置文件”中打开 `zeeho_config.yaml`，按下一节教程填写并
重新运行。完整示例见 `zeeho_config.example.yaml`。

```yaml
app:
  app_id: ""
  app_secret: ""
  # 原生 App 域（tapi.zeehoev.com 社区/每日任务）专用密钥对，可空（留空回退用 H5 密钥）
  tapi_app_id: ""
  tapi_app_secret: ""
  base_url: "https://h5.zeehoev.com"
  user_agent: ""
  zeeho_user_agent: ""

runtime:
  timeout_seconds: 15
  use_proxy: false
  notify: true
  # 签到盲盒：服务端奖励进度达到 30 时自动领取
  blind_box: true
  # 每日任务：发帖→喜欢→分享→删帖（前 3 步各 +1 积分，删帖为清理）
  daily_tasks: true
  # 自动发布的帖子正文，留空默认按当天日期生成（形如「9月9日打卡」）
  daily_post_content: ""

accounts:
  - name: "账号1"
    user_id: ""
    authorization: ""
    cookie: ""
```

`runtime.notify` 控制是否调用青龙通知。脚本直接导入与任务入口同目录、由青龙提供的
原生 `notify.py`，项目自身不包含该文件；本地没有 `notify.py` 时会自动跳过推送。

## 🔑 教程：如何抓到并填写各项配置

### 1. 准备抓包

在电脑上打开 Reqable 并开启 HTTPS 抓包（手机安装并信任 Reqable 根证书、走电脑代理，
或直接用 Reqable 的移动端抓包；能让 H5 请求以明文出现在抓包工具里即可）。Reqable 抓包
结果可整体导出为 `.har`，本文档配套的 `tapi.zeehoev.com_*.har` 即一次完整样例。

### 2. 找到签到接口的请求

手机打开极核 App → 进入“签到”页（H5 页面 `h5.zeehoev.com/activity/signin`）→ 触发
一次签到/状态刷新。在抓包列表中过滤域名 `h5.zeehoev.com`，找到这些请求（路径含
`cfmotoservermine/signin`）：

- `GET /cfmotoservermine/signin/info?month=YYYY-M` —— 查询当月签到明细
- `POST /cfmotoservermine/signin` —— 执行签到
- `POST /cfmotoservermine/signInSupplement/count` —— 补签卡数量（辅助请求，无需填写）

以 **`signin/info`** 这个 GET 请求为准，下面要抄的值全在它的请求头里。
**不要抄 `tapi.zeehoev.com`（原生 App 接口）的请求头**——域名不同，虽然签名密钥可能
通用，但按 H5 前端实际请求来填最稳。

### 3. 从请求头复制“账号相关”字段

展开该请求 → “请求头”，按下表逐行复制到 `accounts` 段：

| 配置字段 | 抓包请求头 | 说明 |
|---|---|---|
| `accounts[].user_id` | `user_id` | 用户 ID，例如 `20260825003041623` |
| `accounts[].authorization` | `Authorization` | 整行复制，`Bearer` 前缀保留（脚本会自动补前缀，缺失也不影响） |
| `accounts[].name` | —— | 仅日志显示用，随意命名 |

> **不需要 Cookie**：实际鉴权只靠 `Authorization`（bearer token），请求头里的 `Cookie`
> 与鉴权无关，脚本已不再读取该字段（配置里写了也会被忽略）。这一结论有实测支撑：置空
> Cookie 后签到与每日任务全部通过；Loon 版脚本同样全程不带 Cookie（它的 `getCookie()`
> 其实只抓 `Authorization` / `User-Agent` 和响应体里的 `id`/`nickName`，从不读 Cookie 头）。

根据实际使用反馈，手机端持续登录、使用 App 时会续期，现有 `authorization` 可继续沿用。
脚本使用配置中的令牌，不执行登录或续期操作。若服务端明确返回 `401 invalid_token`，
只需重新抓包刷新该字段，其他配置不用动；`code=40000` 也可能是其他请求异常。

### 4. 获取 `app.app_id` / `app.app_secret`（签名密钥）

这两项**不会出现在任何请求头里**（请求头只有签名结果），是 H5 前端 JS 内置的“应用级”
常量，服务端按 `appId` 找到对应密钥来校验每次请求的签名。它们对所有账号通用，但
**前端改版后可能轮换**（例如旧版 H5 用 `S7qPWPU1`，新版本 2.93.5 已换成独立密钥），
若突然报“签名被拒”通常是它们过期了，需要重新提取。

提取步骤：

1. 在抓包里找到 H5 页面入口的 JS：通常是 `signin` 页面 HTML 里引用的
   `https://oss-cfmoto.zeehoev.com/codes/zeehoev-h5/static/<版本>/js/app.<hash>.js`
   （也可直接看页面源码里的静态资源地址）。
2. 在 JS 中搜索关键字 `VUE_APP_PRODUCTION_CONFIG`，会看到一段 JSON，形如：

   ```text
   "appId":"0111000000000000 0110110000000000 ..."(共 8 组 16 位二进制)
   "appSecret":"0011010000000000 0110001000000000 ..."(共 40 组 16 位二进制)
   ```

   > 每组 16 位二进制的前 8 位是该字符的 ASCII 码、后 8 位恒为 0。`appId` 解码后是
   > 8 位字母数字（当前为 `Sw5F9uJi`），`appSecret` 解码后是 40 位十六进制字符串。

   ⚠️ **同一份 JS 里其实有三块配置：`VUE_APP_TEST_CONFIG`（测试环境
   `h5-test.zeehoev.com`）、`VUE_APP_RELEASE_CONFIG`（预发 `h5-pre.zeehoev.com`）、
   `VUE_APP_PRODUCTION_CONFIG`（线上 `h5.zeehoev.com`）**。三块的二进制串长得一样，
   但解码结果不同——必须取 **PRODUCTION** 那一块。以 2.93.5 版为例：TEST 块解码为
   `plBK5SB9` / `4b3e1888…`，PRODUCTION 块才是 `Sw5F9uJi` / `46870a8f…`。
   若误取了 TEST/RELEASE 块，二进制能正常解码、校验也能通过，但线上网关不认这对密钥，
   表现为“转换成功了还是 HTTP 430 / 30121”（旧配置里出现 `4b3e1888…` 这类密钥，
   多半就是当时取到了 TEST 块）。
3. 把两串**二进制空格串原样粘贴**到 `zeeho_config.yaml`：

   ```yaml
   app:
     app_id: "0111000000000000 0110110000000000 ..."   # 8 组
     app_secret: "0011010000000000 0110001000000000 ..."  # 40 组
   ```

   脚本会在下次运行时自动校验（每组 16 位、组数正确、后 8 位为 0）并解码，然后把
   解码后的普通字符串原子写回配置文件，后续运行直接用普通字符串。

   > 粘贴时**只贴引号内的二进制串本身**，不要把外层双引号或整段 JSON 一起贴进来，
   > 否则 YAML/格式校验会直接报错。若不确定是否贴对，可以先解码后的明文核对：运行后
   > 日志出现「已自动解码 … 并写回配置文件」即成功。

   如果已经手动解码过，也可以直接填明文：`app_id` 填 8 位字母数字（如
   `Sw5F9uJi`），`app_secret` 填 40 位十六进制。脚本只替换这两个字段对应的行，不会
   改动 User-Agent 等其它配置行。

4. 顺手核对：抓包里任意 `h5.zeehoev.com` 请求的 `Cfmoto-X-Param` 请求头形如
   `appId=Sw5F9uJi&nonce=…&timestamp=…`，其中的 `appId` 应与 JS 里解出来的一致——这是
   一个无需打开 JS 就能确认 app_id 的捷径（但拿不到 secret，secret 仍要从 JS 提取）。

### 5. `app.user_agent` 与 `app.zeeho_user_agent`

从**同一个请求头**里复制（App 级必填，缺失时脚本会在签到前停止）：

| 配置字段 | 抓包请求头 |
|---|---|
| `app.user_agent` | `User-Agent`（整行，含 `Zeeho/3.0.3 …` 尾巴的 WebView UA） |
| `app.zeeho_user_agent` | `Zeeho-User-Agent`（形如 `MOBILE|Android|17|ZEEHO_APP|3.0.3|…`） |

> 提示：真实流量里 `Zeeho-User-Agent` 中间的 UUID 段每次请求都会变（前端现生成的
> 请求 ID）。实测服务端不校验该 UUID，配置文件里写死一个即可；若哪天遇到风控，可把
> 它替换成抓包里看到的任意新值。

### 6. 多账号

`app` 段（签名密钥、UA）所有账号共用一份；要加第二个账号只需复制一个 `accounts` 块，
为它抓包获取独立的 `user_id` / `authorization`。

## 签名与动态时间戳（为什么不会像“回放”那样 430）

签名算法（`modules/zeeho.py` 的 `create_signature`）：

```text
sign = MD5( SHA1hex( data + "appId=<id>&nonce=<uuid>&timestamp=<毫秒>" + app_secret ) )
```

- `data` = 请求的查询串 + 请求体（GET 无查询时为空串；多个参数按 key 排序）。
- 三个签名头：`Cfmoto-X-Param` / `Cfmoto-X-Sign` / `Cfmoto-X-Sign-Type: 0`。

脚本**每个请求都会现算**：`nonce` 用 `uuid4()` 新生成，`timestamp` 取当前毫秒时间，
因此每次运行都与 Loon 一样“新鲜”，不受服务端时间窗限制。

> 这也是为什么**不要用抓包工具的“回放”来验证签名接口**：回放会把抓包那一刻写死的
> 时间戳原样重发，只要超过服务端允许的时间窗（实测约几十分钟内就会失效）就被网关拒
> 绝，返回 `430 {"code":"30123"}`。Loon 可用、脚本可用，都是因为它们每次现算签名。

## 签到流程与状态确认

每个账号依次执行（`modules/zeeho.py` 的 `sign_in`）：

0. **App 端状态查询**：`GET tapi.zeehoev.com/v1.0/mine/cfmotoservermine/signin/count`
   （只读）→ `data.count` = **连续签到天数**、`data.signStatus = 1` = **今天已签到**，
   与 App 签到页显示的是同一个数字。连签天数以它为准，接口不可用时才退回下面的 H5
   明细推算；`signStatus` 还用作签到后复核的兜底（见第 3 步）。
1. **签到前查询**：`GET /cfmotoservermine/signin/info?month=YYYY-M`，在
   `data.nowSignDetailVos` 里找 `createDate == 今天` 的记录。
   - 若 `signStatue` 为已签到状态，直接返回“今日已签到”并读取当日积分，**不再发签到
     请求**（幂等，可放心每天定时跑）。
   - 月份写成 `2026-9` 还是 `2026-09` 服务端都接受，签名只覆盖你实际发送的查询串，
     两者自行保持一致即可。
2. **执行签到**：未签到时 `POST /cfmotoservermine/signin`（空请求体，
   `Content-Length: 0`，签名按上面的算法现算）。
   - 业务 `code=10000` 表示成功；`repeatedly_operation` 表示今天已签过（或操作过快），
     同样按成功处理。
   - 该响应里已经带出 `data.integralScore`（本次积分），脚本用它作为本次得分的兜底值。
3. **签到后确认**：再次 `GET /signin/info`，确认当天记录确实变为已签到状态，并读取
   当天 `integralScore` 作为本次积分回报；若该复核请求失败，会再用一次
   `signin/count` 兜底——`signStatus` 从「未签」变成「已签」同样视为签到成功。两者都
   拿不到结论时才报告异常，绝不假成功。

### 通知与日志格式

签到、盲盒与每日任务跑完后，每个账号输出一个报告块，最后再汇总一行可用积分（日志里的
`[签到]` / `[盲盒]` / `[任务]` / `[余额]` 行与通知内容一致）：

```text
共1个账号，成功1个，失败0个
[1] Higanoneko：
签到：
✅ 签到成功：积分+1（已连签 4 天）
盲盒：
ℹ️ 盲盒未解锁（4/30）
任务：
✅ 发帖成功：积分+1
✅ 喜欢成功：积分+1
✅ 分享成功：积分+1
✅ 已删除任务用帖子
可用积分：208
```

- **签到行**：`✅ 签到成功`（本次真的签了）/ `ℹ️ 今日已签到`（幂等跳过，不再发签到请求）/
  `❌ 签到失败：<原因>`。
  - **积分 +1**：签到后复核当天的 `integralScore`；若复核请求失败，退回签到接口响应里
    直接带出的 `data.integralScore`（抓包实测有值，早于明细复核即可拿到），因此
    「签了但明细没查回来」也能报出本次得分。
  - **已连签 N 天**：取自 App 端 `signin/count` 的 `count`（与 App 签到页一致）；该接口
    不可用时退回 H5 明细推算——从今天往前逐天数已签到记录，遇断签停止，数到本月 1 号
    时再补查上一个月继续累计。
- **盲盒行**：未解锁时显示奖励进度；领取成功时显示本次积分和奖品名称；失败时显示原因，
  该账号计为失败，但仍继续每日任务。签到未成功时跳过领取。
- **任务行**：`✅ XX成功：积分+X`（本次执行）/ `ℹ️ XX已完成：积分+X`（今天早些时候已完成，
  本次跳过不重复执行）/ `❌ XX失败：<原因>`；最后一行是删帖结果
  `✅ 已删除任务用帖子` / `ℹ️ 无任务帖需要删除`。
  - **积分 +X** 取积分规则表里该任务的单次奖励（`integralNumber`，抓包实测三项都是 1）。
- **可用积分**：报告最后一行是所有账号可用积分的合计。每个账号的余额都在**整条任务链
  跑完后**重新查一次（只读查询 App 端用户信息接口
  `tapi.zeehoev.com/v1.0/mine/cfmotoservermine/setting/<user_id>` 的 `data.score`，
  签名密钥与 H5 同一套，无需额外配置），所以合计里已经包含本次签到、盲盒与三项任务赚到的
  积分；日志里对应每个账号的 `[余额] 可用积分：N` 行。
  - 余额**不做推算**：任务行显示的「积分+X」只是规则表里的单次奖励，不参与余额计算。
  - 这次重查失败时（不影响签到与任务结果）退回签到阶段查到的那次余额；两次都没取到
    的账号不计入合计，并在行尾标注「另有 N 个账号未取到」。关闭每日任务
    （`runtime.daily_tasks=false`）时，若盲盒未解锁或已关闭，直接用签到阶段的余额；
    若执行了盲盒领取或资格查询失败，仍重查一次余额，查询失败退回原余额。

`signStatue` 含义（本次抓包实测）：

| 值 | 含义 |
|---|---|
| `3` | 当天已签到（`integralScore` 有值，每天 +1） |
| `5` | 已签到（部分补签场景的状态；脚本同样视为已签） |
| `4` | 当天未签到（已过去的日子漏签） |
| `2` | 未来日期的占位，无需处理 |

## 签到盲盒自动领取

`runtime.blind_box` 默认 `true`，旧配置不补字段也会启用；设为 `false` 可关闭。
它独立于 `runtime.daily_tasks`，关闭社区任务仍会领取签到盲盒。

签到成功（包含今日已签到）后，脚本重新查询 H5 `GET /cfmotoservermine/signin/info`，
仅在服务端返回的 `data.signCount >= 30` 时调用：

```text
GET /cfmotoservermine/signin/supplementPrize?supplementDate=YYYY-MM-DD
```

日期取运行当天，请求使用现有 H5 鉴权和动态签名，无需新增账号字段。盲盒奖励进度
`signCount` 与 App 的连续签到天数 `count` 分开处理；进度缺失或查询失败会报告异常，
不会按连签天数猜测领取资格。每次运行都重新读取服务端进度。

领取响应 `code=10000` 表示成功，积分读取 `data.integral` 或 `data.integralScore`，
奖品名称读取 `data.prizesName`。领取失败或超时在本次运行中不重试，避免请求已生效后
重复开奖；`repeatedly_operation` 也会报告异常，不直接认作领取成功。
这一实现移植自 `Reference/ZEEHO-Loon`，已用模拟响应验证，尚未实测真实账号的开奖流程。

## 每日任务（发帖 → 喜欢 → 分享 → 删帖）

签到之外，App 社区还有三项每日任务会给积分（`runtime.daily_tasks: true`
时签到完成后逐账号自动执行，抓包实测每天各 +1）：

| 任务 | 积分 code | 接口（均走 `tapi.zeehoev.com/v1.0`，签名密钥同上） |
|---|---|---|
| 每日首次发帖 | `1023` +1/天 | `POST social/cfmotoserversocial/commonArticle` |
| 点赞内容 | `1026` +1，每日上限 1 次 | `POST social/cfmotoserversocial/socialCommu/likeFavoriteInfo`（body `{"kindFlag":"0","postId":<id>}`） |
| 分享内容 | `1024` +1，每日上限 1 次 | `PUT social/cfmotoserversocial/article/share/<id>`，随后 `GET mine/cfmotoservermine/integral/adjustByShare` |
| ——（清理） | —— | `DELETE social/cfmotoserversocial/commonArticle/deleteArticle?articleId=<id>&postType=1` |

为了让流程自洽且不影响正常使用，脚本把四步串成一条链：

1. **先查「我的帖子」**（`community/mineArticleInfo?userId=<user_id>`）与今天的任务进度
   （`integral/getByUserId` 规则表，只读）：规则行的 `createDate` 属于今天 ⇒ 今天已经领到
   过该任务积分（App「积分任务」面板就是用这个字段显示已完成的，三个任务都适用）；
   老响应没有该字段时退回备注文本里的 `(1/1)`（点赞/分享的每日上限进度）。
2. **清理残留**：正文形如打卡帖的旧帖（脚本上次运行在删帖前崩溃留下的）先删掉，
   不会动任何普通帖子。
3. **发帖**：正文默认按当天日期生成（`runtime.daily_post_content` 留空时形如
   `9月9日打卡`），无图。今日已有打卡帖（上一轮没跑完）或 App 端今天已经发过帖时
   不重复发；发帖接口不回帖 ID，脚本会**复核**一次列表定位新帖，
   找不到就停下来报错，绝不点赞/分享/删除没确认到的帖子。
4. **喜欢自己的打卡帖**（当天进度已完成、或帖子已被赞就跳过，避免重复点赞变成取消）。
5. **分享** 并同步调用一次积分调整接口。
6. **删帖**：把本次的打卡帖删掉，账号上不留垃圾内容。

幂等与异常兜底：

- 三项任务今天都已完成、又找不到今日打卡帖 → 判定上一轮已整链跑完，
  **整链跳过**（报告里显示「ℹ️ XX已完成」，可放心一天多跑几次）。
- 上一轮在删帖前中断 → 今天会复用那篇打卡帖补齐点赞/分享再删掉。
- 识别只匹配「本人帖子 + 打卡帖正文形状」，用户手动发的帖子永远不会被点赞取消、
  分享或删除。

> 说明：这些接口在原生 App 抓包里用 App 内置的密钥对签名（`appId=S7qPWPU1`），
> 与 H5 的密钥对不同。脚本默认复用 H5 密钥对（与可用积分接口同域同规则，已验证
> 可用）；万一服务端后续校验 App 专属密钥导致 `HTTP 430 code=30121`，可在
> `app.tapi_app_id` / `app.tapi_app_secret` 填 App 的密钥对（支持粘贴二进制串，
> 自动解码写回）。

## 常见错误对照表

| 现象 | 含义 | 处理 |
|---|---|---|
| HTTP `430`，`code=30121` permit error | 签名密钥不匹配：`app_secret` 与 `app_id` 配对不上（密钥被服务端轮换，或误取了 JS 里 TEST/RELEASE 环境块的密钥） | 按“教程第 4 节”重新从当前 H5 JS 的 **`VUE_APP_PRODUCTION_CONFIG`** 提取 `appId`/`appSecret` |
| HTTP `430`，`code=30123` permit error | 签名时间戳过期/超窗（常见于手工回放旧抓包请求） | 用脚本或 Loon 现算签名；Reqable 回放无法用于验证 |
| HTTP `430`，`code=30000` permit error | 签名参数异常（如 nonce 为空） | 检查是否改坏了签名头/参数 |
| HTTP `401` `invalid_token` | `authorization` 登录令牌失效 | 重新抓包刷新该账号的 `authorization` |
| HTTP `200`，`code=40000` / “网络开小差” | 登录态或请求异常 | 优先刷新 `authorization`；仍失败则核对 UA 与签名 |
| HTTP `200`，`code=repeatedly_operation` | 当天已签到或操作过快 | 正常，脚本按已签到处理 |
| 本地 SSL 报错 | 本机残留抓包代理 | 清掉 `http_proxy`/`https_proxy` 环境变量，或确认 `runtime.use_proxy` |

## 本地验证

```bash
python -m unittest discover -s tests -v
```

日常试跑（会真实请求签到接口）：

```bash
python zeeho_auto_sign.py
```

本项目仅用于自动化本人账号，请遵守相关服务条款。
