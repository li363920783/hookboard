# Hookboard

**把「有人让我改代码」这件事，从聊天窗口里捞出来，变成一条有状态、有审批、有记录的工作项。**

钩子进来，任务派给你已经在用的那个 agent CLI，到了你想让人点头的节点就停下来，每一步都留痕。
一个页面看全部。它是「解决一件事」的工具，不是平台：只用 Python 3.11+ 标准库——不要数据库服务、
不要消息队列、没有任何第三方运行时依赖。

> 状态：**M1（闭环）已实现**。施工图：[`docs/PLAN.md`](docs/PLAN.md)。
> 定位口径：[`docs/POSITIONING.md`](docs/POSITIONING.md)。

## 它解决的痛

- 缺陷在工单系统里，讨论在 IM 里，代码在编辑器里——三处割裂，永远对不上。
- 让 AI agent 动手，但你不敢给它全权：它可能改生产库、推错分支。于是变成你盯着它干，比自己干还累。
- 手机上来一条消息，你只能回「等我到电脑前」。
- 干完了，谁批的、改了什么、什么时候能发版——只剩聊天记录，事后没人说得清。

Hookboard 就做四步，多一步都不做：

```
① 钩子进来      curl / webhook / CLI，任何系统都能把一条活推进来
② 派给 agent    你已经在用的那个（Claude Code / Codex / 任意 CLI）
③ 该点头就停    在你配置的节点停下来，发一个链接给你，手机上点一下继续
④ 走完留痕      每一步都有时间、有谁批的、有结果
```

## 5 分钟 demo

不用改配置，也不用先接任何真实系统。

```bash
git clone https://github.com/li363920783/hookboard.git && cd hookboard
pip install -e .
hookboard run
# 浏览器打开 http://127.0.0.1:8899/
```

另开一个终端：

```bash
hookboard create --title "fix the login bug" --kind demo
```

看着卡片从 **待处理 → 等你审批**，打开审批链接（或直接在卡片上点 **通过**），它就继续走到
**已完成**。这就是完整闭环。默认值已经带了 shell agent 和两道审批闸门，所以**完全不写配置文件也能跑**。

想留一份配置文件？`cp config.example.json config.json`——原样就能跑（shell agent 开、webhook
notifier 关）。不想用 CLI 也可以直接推钩子：

```bash
curl -s -X POST http://127.0.0.1:8899/hooks/generic \
  -H "X-Hub-Token: change-me" -H 'Content-Type: application/json' \
  -d '{"id":"acme-42","kind":"demo","title":"fix the bug"}'
```

同一个请求体重复投递会返回 `created: false` 和同一个 `task_id`：`(source, kind, external_id)`
唯一，不会建第二条任务。

## 配置

**为什么用 `config.json` 而不是 `config.yaml`？** 解析 YAML 要引第三方包（PyYAML），JSON 是标准库
自带的，而这个项目坚持运行时零依赖。示例文件是 `config.example.json`，开箱即可跑。

```json
{
  "server": {"bind": "127.0.0.1", "port": 8899, "api_token": "change-me",
             "public_base": "http://127.0.0.1:8899"},
  "engine": {"workers": 2, "poll_interval": 1},
  "database": {"path": "./data/hub.db"},
  "pipeline": {
    "stages": ["intake", "plan", "dispatch", "work", "review", "finish"],
    "approve_at": ["plan", "review"]
  },
  "routing": {"field": "project"},
  "projects": {},
  "sources": {"generic": {"type": "generic",
             "map": {"kind": "kind", "external_id": "id", "title": "title"}}},
  "agents": {"default": {"type": "shell", "command": "echo done"}},
  "notifiers": {
    "feishu": {"type": "feishu", "enabled": true, "app_id": "", "app_secret": "",
               "receive_id_type": "chat_id", "receive_id": "", "inbound": false,
               "dry_run": true},
    "webhook": {"type": "webhook", "url": "https://example.com/hook", "dry_run": true},
    "email": {"type": "email", "host": "smtp.example.com", "port": 465, "tls": "ssl",
              "user": "bot@example.com", "password": "", "from": "bot@example.com",
              "to": ["you@example.com"], "dry_run": true}
  }
}
```

- 配置文件里的相对数据库路径按配置文件所在目录解析。
- `public_base` 用来拼审批链接；放到反代或局域网后面时改成实际地址。
- `dry_run: true`（或 `HUB_OUTBOX=1`）让渠道把 payload 写进 `data/outbox/` 而不真发——示例里
  默认就是这种离线形态。

环境变量可覆盖配置文件（优先级更高）：
`HUB_CONFIG`、`HUB_BIND`、`HUB_PORT`、`HUB_API_TOKEN`、`HUB_PUBLIC_BASE`、`HUB_WORKERS`、
`HUB_POLL_INTERVAL`、`HUB_DB`、`HUB_OUTBOX`、`HUB_EMAIL_PASSWORD`。

## 项目与路由

**项目**是一个配置对象，绑定「在哪跑、谁来批、发到哪」。每个人的项目和规则都不一样，所以全是配置，
代码里不含任何人的身份和工作区路径：

```json
"projects": {
  "followup": {
    "agent": "cc",
    "workspace": "/home/alice/work/followup",
    "approve_at": ["plan", "review"],
    "notifiers": ["feishu_team"]
  },
  "shop": {
    "agent": "cc",
    "workspace": "/home/alice/work/mall",
    "approve_at": ["review"]
  }
}
```

没写的键回落到全局设置。`"approve_at": []` 表示这个项目不设闸门；`"notifiers": []` 表示不通知。
agent 以 `workspace` 为工作目录运行，并拿到 `HUB_PROJECT` / `HUB_WORKSPACE`。

项目还能决定**卡片上显示什么**和**某个阶段跑哪条命令**：

```json
"projects": {
  "tapd": {
    "agent": "t3",
    "workspace": "/home/alice/work/shop",
    "fields": {"缺陷标题": "bug.title", "严重程度": "bug.severity", "链接": "bug.url"},
    "steps": {
      "finish": "python3 ~/bin/tapd_update.py --id \"$HUB_EXTERNAL_ID\" --status resolved"
    }
  }
}
```

- `fields`：从钩子 payload 里挑字段，渲染成**审批卡片正文**和 **agent 提示词**（点号路径，和 source
  的 `map` 是同一套）。不配就把整段 payload 截前 1500 字符带上。
- `steps`：某个阶段要跑的命令，复用 `CommandAgent`——`HUB_*` 全在（含 `HUB_EXTERNAL_ID`、
  `HUB_SOURCE`、`HUB_KIND`、`HUB_PAYLOAD`），退出码决定继续还是失败。发版、回写工单系统都放这里。
- 审批阶段不跑 `steps`（先等人点通过）。
- **按单路由**：让 `steps.intake` 输出 `{"workspace": "...", "branch": "...", "reason": "..."}`
  并在 `parse` 里映射这三个键，引擎会把 workspace/branch 记在任务上，优先于项目的
  `workspace`/`branch`；`reason` 进 note，随审批卡片展示，人点通过后才真正派单。
  路由命令可以是任何东西（本地模型 CLI、规则脚本），目录由它自己掌控——组件只认这三个键。
- agent 的提示词是「标题 + note + 上面那份摘要」，不再是光秃秃一个标题。

**钩子怎么找到自己的项目**——只有两种，刻意不做规则 DSL（`when/then` 语言最后会长成半个工作流引擎）：

| 配置 | 行为 |
|---|---|
| `"routing": {"field": "project"}` | 直接从请求体读 `body["project"]`——零代码，覆盖大多数系统 |
| `"routing": {"python": "myrules:route"}` | 调你自己写的 `route(body) -> str`（约 20 行），逻辑和风格都由你定 |
| `"sources": {"tapd": {"project": "tapd"}}` | 这个 webhook 固定落某个项目；body 里带 `project` 时仍以 body 为准 |

项目名写错返回 HTTP 400，明着失败，不会悄悄按错误的规则跑。没配置 `projects` 时路由被忽略。

## Webhook 入站

外部系统推事件时发的是**它自己的**签名，发不了我们的 `X-Hub-Token`。所以密钥和校验方式按 source 配，
代码里没有厂商分支，GitHub、GitLab、TAPD、自建系统都是同一套：

```json
"sources": {
  "github": {
    "type": "generic",
    "map": {"title": "pull_request.title"},
    "headers": {"kind": "X-GitHub-Event", "external_id": "X-GitHub-Delivery"},
    "auth": {"type": "hmac_sha256", "header": "X-Hub-Signature-256",
             "secret_env": "HUB_GITHUB_SECRET"}
  },
  "gitlab": {
    "type": "generic",
    "map": {"title": "object_attributes.title"},
    "headers": {"kind": "X-Gitlab-Event"},
    "auth": {"type": "token", "header": "X-Gitlab-Token",
             "secret_env": "HUB_GITLAB_SECRET"}
  }
}
```

- `headers` 从请求头取值，**优先于** body 里的 `map`。GitHub 用它把事件名当 `kind`、把
  `X-GitHub-Delivery` 当 `external_id`——同一个投递重发自动去重。
- `map` 的值可以是一个**路径列表**，按顺序取第一个非空值。TAPD 的 `bug::update` 只给
  `old_title`，`bug::create` 才给 `title`，写成 `"title": ["title", "old_title"]` 两边都覆盖。
- `accept` 只接收列出的 `kind`，其它事件回 200 后忽略。**接 TAPD 必须配**，否则每次状态变更
  （包括 agent 回写状态）都会再开一轮。
- `filter`：需要条件判断时（比如「处理人变成我才收」「评论里 @ 我才收」），配一个你自己的函数
  `"filter": "/path/rules.py:accept"`。它收到 `(body, headers)`，返回 `False` 丢弃，返回 `True`
  接收，返回 dict 还能覆盖 `kind` / `title` / `external_id`——评论事件靠这个把标题换成评论正文。
  规则文件放在你的部署里，不进仓库。
- `auth.type`：`hmac_sha256`（对原始请求体算 hex 摘要，带不带 `sha256=` 前缀都认）、`token`
  （头里的值直接比对）或 `query`（连请求头都不发的系统，密钥放 URL：
  `/hooks/tapd?token=...`）。密钥来自 `secret_env` 或内联 `secret`。
- `project`：这个 source 默认落到哪个项目。TAPD 的推送里没有 project 字段，靠这条路由；
  body 里带 project 时仍以 body 为准。
- 配了 `auth` 但拿不到密钥时**一律 401**（fail closed），不会退化成不校验。
- 配了 `auth` 的 URL 不再要求 `X-Hub-Token`；没配的照旧要求，未知 source 也要先过全局校验。

**接 TAPD / 企微这类系统时不要对着文档猜字段。** 先配一个宽松的 source（`map` 留空也行），把它的
webhook 指到 `/hooks/tapd`，触发一次真实事件，再 `GET /api/tasks/{id}` 看存下来的 `payload` 长什么样，
然后补 `map` 和项目的 `fields`。TAPD 如果用表单
（`application/x-www-form-urlencoded`）推送，服务端照样能解析；它的鉴权头也以真实请求为准再配 `auth`。

## HTTP API

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/hooks/{source}` | 把进来的钩子解析成任务（`{task_id, created}`） |
| `POST` | `/api/tasks` | 直接建任务 |
| `GET` | `/api/tasks?status=&stage=` | 任务列表（带当前开放审批） |
| `GET` | `/api/tasks/{id}` | 单任务详情：审批、events、runs |
| `POST` | `/api/tasks/{id}/wake` | 清空 `next_run_at`，让 pending 任务立刻跑 |
| `POST` | `/api/tasks/{id}/cancel` | 取消 pending/running/waiting 任务 |
| `GET` | `/a/{token}` | 手机友好的审批页面（HTML） |
| `POST` | `/a/{token}` | 提交 `{"index":0}` / `{"index":1}` / `{"text":"…"}` |
| `GET` | `/api/board` | 看板数据（前端每 3 秒轮询） |
| `GET` | `/api/health` | `{ok, version, workers, queue}` |

写接口（`/hooks/*`、`/api/tasks/*`）要求 `X-Hub-Token: <api_token>`。读接口默认绑定
`127.0.0.1` 所以不鉴权。`/a/{token}` 用每张审批自己的 24 位随机 token，因此链接可以直接转发到
手机或聊天里，不用暴露 API token。不涉及任何 IM 长连接。

### 审批语义

- `{"index": 0}`：通过，任务前进一个阶段并变回 `pending`。
- `{"index": 1}`：打回，任务变 `failed`，决定写进 events。
- `{"text": "..."}`（不带 index）：通过并附言。
- 同一个决定重复提交返回原结果、`changed: false`，不会重复推进流程。

## 看板

单文件 `web/index.html`，不引 CDN、不需要构建，断网也能用。四列（待处理 / 进行中 / 等你审批 /
已完成），深色主题，手机上自动单列。等待审批的卡片直接内嵌选项按钮和「或者直接说」输入框，
点了就 `POST /a/{token}`。

## 适配器

M1 内置的三个实现就是给接入者抄的样例：

- `sources`：`generic`（JSON 字段映射写在配置里，没有 id 时用请求体哈希去重）
- `agents`：`shell` / `command`——同一个配置驱动的适配器。它跑你的命令，把任务信息放进 `HUB_*`
  环境变量，并能从输出里读回结构化字段。所以 Claude Code、Codex、`t3 thread start`、你自己的脚本
  都只是不同的配置块：

```json
"agents": {
  "t3": {"type": "command",
         "command": "t3 thread start \"$HUB_WORKSPACE\" \"$HUB_PROMPT\" --json --wait",
         "timeout": 1800,
         "parse": {"format": "json", "session": "threadId", "output": "reply"}},
  "cc": {"type": "command",
         "command": "claude -p \"$HUB_PROMPT\" --output-format json --model claude-sonnet-5-5",
         "timeout": 1800,
         "parse": {"format": "json", "session": "session_id"}}
}
```

`parse` 的路径支持点号（`data.thread.id`）。`session` 存到任务上，`link` 给人看的链接，`output`
替代原始 stdout 写进运行记录。`exit_codes` 把非零退出码映射成状态（`{"2": "pending"}` 重试，
未映射的算失败）。默认超时 60 秒——真跑 agent 要调大。
- `notifiers`：`feishu`（自建应用，interactive 卡片 + url 按钮，**默认审批渠道**）、
  `webhook`（POST/GET JSON 到你给的 URL）、`email`（标准库 `smtplib`，multipart/alternative
  的 HTML + 纯文本，支持 SMTP_SSL / STARTTLS）

接口契约在 [`hub/adapters/base.py`](hub/adapters/base.py)，用 `typing.Protocol` 定义。
接自己的系统 = 一个约 30 行的 `Source` 小文件 + 几行配置——或者干脆用 `generic` 加字段映射。
节点函数不碰数据库：只返回 `{status, stage, delay, note}`，由引擎统一写库。

### 通知

`Notifier` 接口是定死的（`notify` / `ask`，都返回 `{ok, detail}`），后面加渠道不改核心。
审批永远走链接（`/a/<token>`），渠道只负责把这条链接送到人眼前。M1 落地 **`feishu`（默认审批
渠道）**，以及 `webhook` 和 `email`。每个渠道都强制遵守：

- **离线可测**：`HUB_OUTBOX=1`（或 `dry_run: true`）不发网络请求，把渠道格式的 payload 写进
  `data/outbox/<时间>-<渠道>.json`。验收脚本就在这种模式下跑。
- **不重发**：同一 `(task_id, stage, notifier)` 只发一次，靠库里一行占位去重。
- **不阻塞**：通知失败只记 `events(kind=notify_error)` 并重试 1 次；审批照旧能在看板上处理。
- **凭据**只从配置 / 环境变量读（`HUB_EMAIL_PASSWORD`、webhook header、
  `feishu.app_id / app_secret / receive_id`），仓库里不存任何真实 key。

`feishu` 用自建应用：先取 `tenant_access_token`，再发 interactive 卡片（schema 2.0）。发送目标纯配置
驱动：`receive_id_type`（`chat_id` 发群，`open_id` / `union_id` / `user_id` / `email` 发个人）+
`receive_id`，就是飞书标准消息 API，代码里不嵌任何具体身份。缺凭据或目标时只打一行 warn 并落到
outbox，不会崩、不会卡住任务。

**两个审批入口，一套状态机。** 每张卡片都带**去处理**（`/a/<token>`）和**打开看板**两个链接；
开着 `inbound` 时再多**通过 / 打回**两个按钮，回调也调同一个 `decide()`——同一套幂等守卫、同一份
事件留痕。两个入口随便点哪个，另一个都会显示「已处理」，不会重复推进。

### 飞书入站（可选）

只发卡片不需要额外依赖。要让 bot「能听」——用户发一条消息即完成绑定、发一句话建任务、在卡片上
直接点通过/打回——才需要官方 SDK，而且严格 opt-in，核心仍是零第三方运行时依赖：

```bash
pip install 'hookboard[feishu]'
```

然后在 `feishu` 配置里设 `"inbound": true`。飞书开放平台这边：应用要有**机器人**能力、**事件订阅
选长连接（WebSocket）模式**（不用公网回调地址）、权限 `im:message` 和 `im:message:send_as_bot`，
并订阅 `im.message.receive_v1` 与 `card.action.trigger` 两个事件。

它做的事：

- **首次接触即绑定**：给 bot 发一条消息（或发 `/bind`），它的 `sender.open_id` 写进
  `data/feishu_binding.json`；配置里 `receive_id` 为空时通知就用这个目标，不用改配置就能收到审批。
- **说一句就建任务**：任意纯文本消息建一条任务（`source=feishu`），按飞书 `message_id` 去重；
  `/help` 列出命令。
- **群里点、网页点都行**：卡片按钮和 `/a/<token>` 页面都走 `decide()`，一边处理了另一边显示已处理。
- **`allowed_open_ids`**（可选）：限制谁能建任务/审批。留空表示能碰到 bot 的人都可以——单人自用
  没问题，多人共享时建议设上。

`lark-oapi` 只在网关内部惰性 import；没装时 `inbound: true` 打一行 warn，服务照常运行。

## 自己跑一遍

```bash
python3 -m unittest discover -s tests -v      # 77 个用例：状态机 + 通知 + 入站网关 + 路由 + webhook 校验 + 阶段命令
ACCEPT_PORT=8899 bash scripts/acceptance.sh   # 7 条判据 + 5 分钟 demo + OUTBOX 渠道；需要 curl + jq
```

验收脚本把包装进一个临时 venv，用 `config.example.json` 原样、在 `HUB_OUTBOX=1` 模式下跑
`hookboard`，走完整闭环并检查 outbox payload，任何一条断言失败都会非零退出。8899 被占用时用
`ACCEPT_PORT` 换一个空闲端口。

## 边界与维护

这是「解决一件事」的工具。它刻意**不做**：工单系统、IDE、内置模型调用、多租户 / 权限体系、
分布式。一个 `api_token` 够用；单机 SQLite 够用。

MIT 许可。无 SLA。issue 尽力回；欢迎 PR，但为了「保持小」会拒掉一部分。