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
  "sources": {"generic": {"type": "generic",
             "map": {"kind": "kind", "external_id": "id", "title": "title"}}},
  "agents": {"default": {"type": "shell", "command": "echo done"}},
  "notifiers": {
    "feishu": {"type": "feishu", "enabled": true, "app_id": "", "app_secret": "",
               "chat_id": "", "dry_run": true},
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
- `agents`：`shell`（跑一条命令；任务信息通过 `HUB_*` 环境变量传入，stdout 作为运行输出）
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
  `feishu.app_id / app_secret / chat_id`），仓库里不存任何真实 key。

`feishu` 用自建应用：先取 `tenant_access_token`，再发 interactive 卡片（schema 2.0），两个 url
按钮——**去处理**（`/a/<token>`）和**打开看板**（`/`）。**不用长连接 / 事件订阅收审批**，决定永远
在网页上做。缺凭据时只打一行 warn 并落到 outbox，不会崩、不会卡住任务。

## 自己跑一遍

```bash
python3 -m unittest discover -s tests -v      # 状态机 + 通知 outbox/去重/重试用例
ACCEPT_PORT=8899 bash scripts/acceptance.sh   # 7 条判据 + 5 分钟 demo + OUTBOX 渠道；需要 curl + jq
```

验收脚本把包装进一个临时 venv，用 `config.example.json` 原样、在 `HUB_OUTBOX=1` 模式下跑
`hookboard`，走完整闭环并检查 outbox payload，任何一条断言失败都会非零退出。8899 被占用时用
`ACCEPT_PORT` 换一个空闲端口。

## 边界与维护

这是「解决一件事」的工具。它刻意**不做**：工单系统、IDE、内置模型调用、多租户 / 权限体系、
分布式。一个 `api_token` 够用；单机 SQLite 够用。

MIT 许可。无 SLA。issue 尽力回；欢迎 PR，但为了「保持小」会拒掉一部分。