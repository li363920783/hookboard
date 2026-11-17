# Hookboard — 把「钩子进来的活」派给 agent，关键节点由人拍板

> 这份文档是**给开发 agent 的施工图**，也是产品定义。凡是与本文冲突的实现，以本文为准；
> 本文没写的细节，按「简单、零依赖、可开源给别人接入」三条原则自行决定，并写进 README。

- 项目代号：**Hookboard**（目录 `hookboard`）
- 语言/栈：**Python 3.11+，只用标准库**（`http.server` + `sqlite3` + `threading`），前端单页 HTML + 原生 JS
- 许可：MIT
- 一句话：**一个跑在自己机器上的小服务，任何系统通过 webhook/CLI 把活推进来，它按可配置的管道派给 AI agent 干活，到了需要人点头的节点就停下来等审批，全程在同一个看板上看得见。**

---

## 1. 为什么要做（要解决的问题，别做偏）

现有编排工具（Temporal/Windmill/Trigger.dev）重、要学、要外挂基础设施；
面向 coding agent 的看板类工具大多只有「跑」没有「审批」，或者只有「通知」没有「状态机」。

Hookboard 只做四件事，做扎实：

1. **入口**：一个 HTTP 端点收事件，一个 CLI 建任务。
2. **管道**：任务按「阶段（stage）」推进，每个阶段是一个可插拔的处理函数。
3. **闸门**：任何阶段都可以要求人工审批；审批通过才继续，通过与否都留痕。
4. **看板**：一个页面看全部任务在哪一阶段、等谁、卡多久；手机上可开。

不做的（明确划界，别扩）：
- 不做 agent 本身（不内置 LLM 调用；只调用外部 agent CLI 或 HTTP API）
- 不做代码托管/PR 平台
- 不做多租户/权限体系（单用户 + 一个访问令牌够用）
- 不做分布式（单机 SQLite；要集群是以后的事）

## 2. 核心概念

| 概念 | 说明 |
|---|---|
| **Task（任务）** | 一条要做的事。有唯一键 `(source, kind, external_id)`，同一件事重复投递只唤醒不新建。 |
| **Stage（阶段）** | 任务当前所处的处理步骤，内置：`intake → plan → dispatch → work → review → finish`（名字可配）。 |
| **Approval（审批）** | 挂在某个 stage 上的「等人点头」。有 token 链接，点一下就答，不需要 IM 长连接。 |
| **Run（执行记录）** | 每次阶段执行一行，含起止时间、agent、输出摘要，用于排障与耗时统计。 |
| **Adapter（适配器）** | 三类：`Source`（能不能收活）、`Agent`（谁来干）、`Notifier`（怎么通知人）。 |
| **Pipeline（管道）** | 一份配置，声明阶段顺序、每阶段用什么 Agent、哪些阶段要审批。 |

### 状态机（必须严格实现）

```
任务状态：pending ──claim──> running ──┬─> waiting  （生成了审批，等人）
                                      ├─> pending  （还没完，delay 后再跑）
                                      ├─> done
                                      └─> failed / cancelled
审批状态：open ──decide──> answered   （决定后任务被唤醒并切到下一阶段）
```

铁律：
- **领任务必须原子**：`UPDATE tasks SET status='running' WHERE id=? AND status='pending'`，靠 rowcount 判断，禁止「先查再改」。
- **审批必须幂等**：已 answered 的再点一次返回原结果，不重复推进流程。
- **阶段推进与审批解耦**：节点函数只返回「下一阶段/状态/延迟」，由引擎统一写入；节点里不要直接改 status。

## 3. 数据模型（SQLite，`store.py`）

```sql
tasks(id, source, kind, external_id, title, lane, status, stage, priority,
      session, note, payload_json, attempts, next_run_at, created_at, updated_at,
      UNIQUE(source, kind, external_id))
approvals(id, task_id, stage, question, detail, options_json, recommend,
          status, answer_index, answer_text, by, token UNIQUE, created_at, decided_at)
events(id, task_id, ts, kind, detail)     -- 审计流水，看板时间线用
runs(id, task_id, stage, agent, started_at, finished_at, ok, output)
```

索引：`(status, next_run_at)`、`(task_id, status)`。WAL 模式，`busy_timeout=10000`。

## 4. HTTP API（v1 必须全部实现）

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/hooks/{source_name}` | 通用入口：任意 JSON，按 `sources/{name}.py` 解析成任务；返回 `{task_id, created}` |
| `POST` | `/api/tasks` | 直接建任务：`{source,kind,external_id,title,priority,payload}` |
| `GET` | `/api/tasks?status=&stage=` | 任务列表（带当前开放审批） |
| `GET` | `/api/tasks/{id}` | 单任务详情（含 events 时间线、runs） |
| `POST` | `/api/tasks/{id}/wake` | 手动唤醒（把 next_run_at 置空） |
| `POST` | `/api/tasks/{id}/cancel` | 取消 |
| `GET` | `/a/{token}` | **审批页面**：显示问题+选项+输入框（HTML，手机友好） |
| `POST` | `/a/{token}` | 提交审批：`{index?, text?}`；返回结果页 |
| `GET` | `/api/board` | 看板数据（前端轮询用） |
| `GET` | `/api/health` | `{ok:true, version, workers, queue}` |

鉴权：
- 写接口要求 `X-Hub-Token: <api_token>`（配置里设），读接口默认同源免鉴权（绑定 127.0.0.1）。
- `/a/{token}` 用**每张审批自己的 token**，24 位随机，不校验 api_token（这样链接能直接发给人）。

## 5. 看板（`web/index.html`，单文件）

一屏四列，列名与状态对应：

```
┌────────────┬────────────┬──────────────┬────────────┐
│ 待处理      │ 进行中      │ 等你审批 ⚠   │ 已完成      │
│ pending    │ running    │ waiting      │ done       │
└────────────┴────────────┴──────────────┴────────────┘
```

卡片必须显示：标题、来源标记（TAPD/GitHub/…）、外部 id、当前阶段、已耗时、最近一条 note。
`waiting` 卡片直接内嵌审批选项按钮 + 一个文本框（「或者直接说」），点了就 POST `/a/{token}`。
每 3 秒轮询 `/api/board`。深色主题、移动端单列、不引任何 CDN（离线可用）。

## 6. 适配器接口（开源的重点：别人能自己接）

`adapters/` 目录，每个适配器一个文件，提供 `register()` 返回实现。用 `typing.Protocol` 定义契约：

```python
class Source(Protocol):
    name: str
    def parse(self, body: dict, headers: dict) -> dict | None:
        """把进来的 hook 变成 {source,kind,external_id,title,priority,payload}；不关心就返回 None"""

class Agent(Protocol):
    name: str
    def start(self, task: dict, prompt: str) -> dict:
        """派活。返回 {ok, session, link, error}；session 用于下一阶段续接"""
    def poll(self, task: dict) -> dict:
        """查进度。返回 {ok, status: running|done|failed, text, error}；没有轮询概念的返回 done"""
    def send(self, task: dict, message: str) -> dict:
        """续接会话再说一句话（可选，不支持就抛 NotImplementedError）"""

class Notifier(Protocol):
    name: str
    def notify(self, title: str, markdown: str, links: list[tuple[str, str]]) -> bool: ...
    def ask(self, approval: dict, url: str) -> bool:
        """把审批发给人。IM 支持卡片就发卡片按钮，不支持就发一条带链接的文字"""
```

**v1 内置实现（必须自带，作为示例给接入者抄）：**
- Source：`generic`（任意 JSON，字段映射在配置里写）、`github`（issue/PR/push）、`cron`（定时建任务）
- Agent：`shell`（跑一条命令，stdout 当输出）、`claude`（`claude -p`）、`http`（POST 到用户自己的 agent 网关）
- Notifier：`webhook`（POST 到任意 URL）、`slack`（incoming webhook）、`feishu`（app_id/secret + chat_id，发 interactive 卡片）

> 注意：**不要把某家公司/某个私有系统的 id、凭据、路径写进代码**。所有环境相关的东西只能出现在 `config.yaml`。

## 7. 配置（`config.yaml` 或 `HUB_*` 环境变量）

```yaml
server: {bind: 127.0.0.1, port: 8899, api_token: change-me, public_base: http://127.0.0.1:8899}
engine: {workers: 2, poll_interval: 2}
database: {path: ./data/hub.db}
pipeline:
  stages: [intake, plan, dispatch, work, review, finish]
  approve_at: [plan, review]        # 这些阶段必须人工审批
sources:
  generic: {type: generic, map: {kind: kind, external_id: id, title: title}}
agents:
  default: {type: shell, command: "echo 'done'"}
notifiers:
  default: {type: webhook, url: ""}
```

`config.example.yaml` 必须随仓库提供，且里面**没有**任何真实凭据。

## 8. 目录结构（照此实现）

```
hookboard/
├── hub/                  # 服务与引擎
│   ├── __init__.py
│   ├── store.py          # 数据层（唯一碰 SQL 的地方）
│   ├── engine.py         # 状态机 + 工作线程 + 节点注册表
│   ├── server.py         # HTTP 路由（stdlib）
│   ├── config.py         # 配置加载 + 环境变量覆盖
│   └── adapters/         # source/agent/notifier 内置实现
├── web/index.html        # 看板（单文件，无构建）
├── examples/             # 别人的接入示例：curl 脚本、docker-compose、GitHub Actions
├── tests/                # 自测：pytest 或 stdlib unittest 均可
├── scripts/acceptance.sh # 一键跑通闭环（见 §10）
├── docs/{PLAN.md,API.md,ADAPTERS.md}
├── config.example.yaml
├── Dockerfile
├── LICENSE (MIT)  README.md(英文)  README.zh-CN.md
└── pyproject.toml        # 只有 [project]，无第三方依赖
```

## 9. 里程碑（按顺序做，做完一期停下来等审批）

**M1 · 能跑通的闭环（第一轮只做这些）**
- `hub/store.py` + `hub/engine.py` + `hub/server.py` + `web/index.html`
- 内置：`generic` source、`shell` agent、`webhook` notifier
- 审批：`/a/{token}` 页面 + `POST /a/{token}`，审批答完任务自动推进
- CLI：`python -m hub create/run/status`（`create` 建任务、`run` 起服务、`status` 打印队列）
- `scripts/acceptance.sh`：起服务 → 建任务 → 看板出现 → 审批点「通过」→ 任务到 done，全程 curl 断言
- README（英文）+ README.zh-CN（中文），含 60 秒上手

**M2 · 能接真实系统**
- `feishu` notifier（interactive 卡片 + 审批链接按钮）、`slack` notifier
- `github` source（issue opened/labeled）、`cron` source
- `claude` agent adapter（`claude -p`，含续接）、`http` agent adapter
- 并行 worker、失败退避重试（指数退避，≥3 次通知，≥10 次放弃）
- `docs/ADAPTERS.md`：写清楚怎么接自己的系统（含一个 30 行的完整示例适配器）

**M3 · 开源就绪**
- Dockerfile + docker-compose（单容器，卷挂 data/）
- 看板增强：SSE 实时、按来源筛选、耗时排序、失败原因展开
- 审计导出（`/api/export?format=jsonl`）
- CI（GitHub Actions：unittest + ruff）、`CONTRIBUTING.md`、issue 模板
- tag 0.1.0，写 release notes

## 10. 验收判据（每期都要能自动验，别口头宣称完成）

`scripts/acceptance.sh` 必须断言这 7 条，任一失败即非完成：

1. `GET /api/health` 返回 `ok:true`
2. `POST /api/tasks` 建任务后 `GET /api/tasks` 能看到，状态 `pending`
3. 3 秒内任务被 worker 领走，状态变 `running` 或 `waiting`
4. 到达需审批的阶段时生成了一条 open 审批，且 `/a/{token}` 返回 200 HTML
5. `POST /a/{token} {index:0}` 后审批变 `answered`，任务是 `pending` 且阶段已前进
6. 全流程结束后任务 `done`，`events` 里能看到每个阶段各一条流水
7. 库里没有重复任务：同一 `(source,kind,external_id)` 二次投递，`tasks` 行数不变

另外必须做到（人工检查）：
- 拔掉网线（或关掉外网）也能起服务、能建任务、能审批、能看看板 —— 证明零外部依赖
- `pip install -e .` 后 `hookboard --help` 可用；不装任何第三方包

## 11. 给接入者的承诺（写进 README，别吹）

- 5 分钟内能跑起来：`git clone && pip install -e . && hookboard run`
- 接一个新系统 = 写一个 30 行的 `Source` 适配器 + 加 3 行配置
- 审批不需要公网回调、不需要 IM 长连接：一个带 token 的链接就够（放在内网/反代后面即可）
- 数据全在本地 SQLite，删掉 `data/` 就是全新系统

## 12. 明确禁止

- ❌ 引入任何第三方运行时依赖（fasthttp、flask、pydantic、sqlalchemy…）—— 只用标准库
- ❌ 把任何真实凭据、真实公司域名、私人路径写进仓库
- ❌ 用 IM 长连接实现审批（会跟别的客户端抢连接）；审批只走 token 链接
- ❌ 让节点函数直接改任务状态（必须由引擎统一写）
- ❌ 一次性做完 M2/M3 再说 —— 每期做完停下等审批