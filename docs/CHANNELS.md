# 通知渠道（Notifier）规格 —— 国内开发者先用这四个

> 目标：**任何一个渠道，只要能把「标题 + 正文 + 一两个链接」发到人手机上**，就能接进 Hookboard。
> 审批永远走链接（`/a/<token>`），渠道只负责把这条链接送到人眼前。**渠道不允许承载业务逻辑。**

## 统一接口（M1 就按这个定，后面加渠道不改核心）

```python
class Notifier(Protocol):
    name: str
    def notify(self, title: str, body_md: str, links: list[tuple[str, str]]) -> dict:
        """普通通知。links=[(按钮文字, url)]，渠道自己决定能放几个。返回 {ok, detail}"""
    def ask(self, title: str, body_md: str, url: str, options: list[str]) -> dict:
        """审批通知。url 是审批页链接；options 只是给人看的提示（点了仍进网页）"""
```

**降级规则**（渠道能力不同，必须显式降级，不能报错就完）：

| 渠道 | 卡片能力 | 降级形态 |
|---|---|---|
| 飞书（自建应用） | interactive 卡片，多个 url 按钮 | 只放得下 1 个按钮时，只放「去处理」 |
| 企业微信（群机器人） | markdown 消息，**只有 1 个可点链接** | 正文里放链接 + 一行提示「点链接处理」 |
| 企业微信（应用消息） | textcard（标题/摘要/一个按钮） | 同上 |
| 邮件（SMTP） | HTML，按钮就是 `<a>` | 纯文本兜底（客户端不支持 HTML 时） |
| 通用 webhook | 原样 POST JSON | 不做任何渲染 |

## 必须实现四个

### 1. `feishu`（自建应用，推荐主渠道）

```
POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
     {"app_id": "...", "app_secret": "..."}          → tenant_access_token
POST https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id
     Header: Authorization: Bearer <tenant_access_token>
     {"receive_id": "oc_xxx", "msg_type": "interactive", "content": "<JSON 字符串>"}
```
- 卡片内容用 schema 2.0（`body.elements` 里放 markdown + `action` 按钮），按钮 `type: "default"`，`multi_url.url` 指向审批链接。
- **不要**用长连接 / 事件订阅收审批：审批走链接，避免和别的客户端抢连接（这是本项目最重要的一条设计决定）。
- 也可以只配 `webhook`（群自定义机器人），但那种没有按钮，只有 markdown 链接 —— 保留作为最简接入。

### 2. `wecom`（企业微信，两种模式都要）

**(a) 群机器人**（最省事，管理员把机器人拉进群就有 key）
```
POST https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<KEY>
     {"msgtype":"markdown","markdown":{"content":"### 标题\n> 正文\n[去处理](https://...)"}}
```

**(b) 应用消息**（能发给指定人，需要 corpid/secret/agentid）
```
GET  https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=..&corpsecret=..   → access_token
POST https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=..
     {"touser":"@all|userid","msgtype":"textcard","agentid":1000002,
      "textcard":{"title":"标题","description":"摘要","url":"https://审批链接","btntxt":"去处理"}}
```
- 注意：`textcard.url` 必须是**企业微信可信域名**里的地址，否则会提示不安全——README 里必须写这条（无数人会卡在这）。

### 3. `email`（SMTP，兜底渠道；内网几乎总能通）

- 标准库 `smtplib` + `email.message.EmailMessage`，支持 `SMTP_SSL(465)` 与 `STARTTLS(587)` 两种。
- 正文发 `multipart/alternative`：HTML（带按钮） + 纯文本（`标题\n正文\n去处理: <url>`）。
- 配置：`host/port/user/password/from/to[]/tls`。**密码只从配置/环境变量读**。
- 用途：**内网没有 IM 时的保底**，也是"审批证据"最自然的留档（邮件本身就是审计材料）。

### 4. `webhook`（通用，接入者自己接任何东西）

```
POST <url>  {"title":..,"body":..,"links":[{"text":..,"url":..}],"task_id":..,"stage":..}
```
- 允许自定义 header（放 token），允许 `GET` 模式（把参数拼到 query，给某些国产 webhook 用）。

## 四个渠道共同的硬约束

1. **只从配置读凭据**：`config.yaml` 或 `HUB_*` 环境变量，仓库里绝不出现真实 app_secret / key / 邮箱密码。
2. **离线可测**：`HUB_OUTBOX=1`（或配置 `notifiers.default.dry_run: true`）时，不发网络请求，把消息按渠道格式写进 `data/outbox/<时间>-<渠道>.json`。**acceptance.sh 必须在这种模式下跑通**，否则 CI 无法验证。
3. **失败不阻塞流程**：通知失败只记 `events`（kind=`notify_error`）并重试 1 次；审批照旧可在看板上处理 —— 不能因为"飞书挂了"把活卡死。
4. **不重发**：同一 `(task_id, stage, notifier)` 只发一次，靠库里一行占位去重（同一阶段重跑不能给人发三条一样的）。

## 优先级（2026-11-17 调整：飞书提到第一，先跑通再说）

1. **`feishu` —— M1 必做**，并且作为默认审批通知渠道（interactive 卡片 + url 按钮）。
   开发者已明确：先接飞书，先弄出来再说。
2. `webhook` + `email` —— M1 保留（OUTBOX 模式即可，用于离线验收/内网兜底），不阻塞 M1 交付。
3. `wecom`（群机器人先做，应用消息后做）—— M2
4. （M3 之后可选）`dingtalk`、`telegram`、`slack`

## 渠道自己的测试夹具（放进 tests/）

- `tests/test_notifiers.py`：四个渠道各 1 个用例，全部在 `HUB_OUTBOX=1` 下跑，断言生成的 payload 结构正确（飞书是 `msg_type=interactive` 且 `content` 里含 url、企微是 `msgtype=textcard` 且 `textcard.url` 非空、邮件是 `multipart/alternative` 且纯文本里含链接）。
- 真实发一条的冒烟脚本放 `scripts/smoke_notify.py`，**默认不跑**，需要显式传 `--send` 并提供环境变量，防止误发到生产群。