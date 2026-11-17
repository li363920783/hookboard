# Hookboard

**Pull "someone asked me to change code" out of the chat window and turn it into a work item
with state, approvals, and a record.**

A hook comes in, the task goes to the agent CLI you already use, it stops wherever you want a
human to nod, and every step is logged. One page shows it all. It is a tool for one job, not a
platform: Python 3.11+ standard library only — no database server, no broker, no third-party
runtime dependencies.

> Status: **M1 (the closed loop) is implemented.** Spec: [`docs/PLAN.md`](docs/PLAN.md).
> Positioning: [`docs/POSITIONING.md`](docs/POSITIONING.md).

## The problem it fixes

- The bug lives in a ticket system, the discussion lives in IM, the code lives in an editor —
  three places that never agree.
- You let an AI agent work, but you can't give it full authority: it could touch production or
  push the wrong branch. So you end up watching it, which is more tiring than doing it yourself.
- A message lands on your phone and all you can reply is "wait until I'm at my computer."
- When it's done, who approved it, what changed, when it ships — all that's left is a chat log.

Hookboard does four steps and nothing more:

```
① hook in        curl / webhook / CLI — any system can push a piece of work in
② hand to agent  the one you already use (Claude Code / Codex / any CLI)
③ stop to nod    pause at the stages you choose, send you a link, tap on your phone to continue
④ leave a trail  every step has a time, an approver, a result
```

## 5-minute demo

No config editing, no real system to wire up first.

```bash
git clone https://github.com/li363920783/hookboard.git && cd hookboard
pip install -e .
hookboard run
# open http://127.0.0.1:8899/
```

In another terminal:

```bash
hookboard create --title "fix the login bug" --kind demo
```

Watch the card move **待处理 → 等你审批**, open the approval link (or press **通过** right on the
card), and it continues to **已完成**. That's the whole loop. Defaults already include a shell
agent and two approval gates, so it works with no config file at all.

Prefer to keep a config file? `cp config.example.json config.json` — it runs as-is (shell agent
on, webhook notifier off). Send a hook instead of using the CLI:

```bash
curl -s -X POST http://127.0.0.1:8899/hooks/generic \
  -H "X-Hub-Token: change-me" -H 'Content-Type: application/json' \
  -d '{"id":"acme-42","kind":"demo","title":"fix the bug"}'
```

Repeating the same body returns `created: false` and the same `task_id`: one task per
`(source, kind, external_id)`.

## Configuration

**Why `config.json` and not `config.yaml`?** Parsing YAML needs a third-party package (PyYAML);
JSON is in the standard library, and this project keeps the runtime dependency-free. The example
file is `config.example.json` and runs out of the box.

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

- Relative database paths resolve against the config file's directory.
- `public_base` builds the approval links; point it at your reverse proxy or LAN address.
- `dry_run: true` (or `HUB_OUTBOX=1`) makes a channel write its payload to `data/outbox/`
  instead of sending — the offline default shipped in the example.

Environment overrides (win over the file):
`HUB_CONFIG`, `HUB_BIND`, `HUB_PORT`, `HUB_API_TOKEN`, `HUB_PUBLIC_BASE`, `HUB_WORKERS`,
`HUB_POLL_INTERVAL`, `HUB_DB`, `HUB_OUTBOX`, `HUB_EMAIL_PASSWORD`.

## Projects and routing

A **project** is a config object binding where a task runs, who gets asked, and on which channels.
Every person's projects and rules differ, so all of it is config — no identity and no workspace path
is baked into the code:

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

Omitted keys fall back to the global settings. `"approve_at": []` means this project has no gates;
`"notifiers": []` means it never notifies. The agent runs with `cwd=workspace` and receives
`HUB_PROJECT` / `HUB_WORKSPACE`.

A project also decides **what the card shows** and **what a stage runs**:

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

- `fields` picks values out of the hook payload for the **approval card body** and the **agent
  prompt** (dotted paths, same as the source `map`). Without it the raw payload is included, first
  1500 characters.
- `steps` runs a command at a stage through the same `CommandAgent` — every `HUB_*` variable is set
  (`HUB_EXTERNAL_ID`, `HUB_SOURCE`, `HUB_KIND`, `HUB_PAYLOAD` included), the exit code decides
  whether the pipeline advances. Releases and ticket write-backs go here.
- Gate stages never run `steps` (a human answers first).
- The agent prompt is now title + note + that digest, not just the title.

**How an incoming hook finds its project** — two shapes, and deliberately no rule DSL (a `when/then`
language grows into half a workflow engine):

| Config | What happens |
|---|---|
| `"routing": {"field": "project"}` | Read `body["project"]` straight out of the hook — zero code, covers most systems |
| `"routing": {"python": "myrules:route"}` | Call your own `route(body) -> str` (about 20 lines) in your deployment, your logic, your style |
| `"sources": {"tapd": {"project": "tapd"}}` | This webhook always lands in that project; a `project` in the body still wins |

An unknown project name is rejected with HTTP 400 so a typo fails loudly instead of silently running
under the wrong rules. With no `projects` configured, routing is ignored.

## Inbound webhooks

An external system signs with **its own** secret and cannot send our `X-Hub-Token`. So the secret and
the verification scheme are configured per source — no vendor branches in code. GitHub, GitLab,
TAPD and home-grown systems are all the same shape:

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

- `headers` reads values from request headers and **wins over** the body `map`. GitHub uses it to take
  `kind` from the event name and `external_id` from `X-GitHub-Delivery`, so a redelivery dedupes.
- A `map` value may be a **list of paths** tried in order. TAPD only sends `old_title` on
  `bug::update` and `title` on `bug::create`; `"title": ["title", "old_title"]` covers both.
- `accept` only lets the listed `kind` values through; everything else is acknowledged and ignored.
  Required for TAPD — otherwise every status change, including the agent's own write-back, reopens
  the task.
- `filter` gives you conditional rules: `"filter": "/path/rules.py:accept"` receives
  `(body, headers)` and returns `False` to drop, `True` to accept, or a dict that overrides
  `kind` / `title` / `external_id` (that is how a comment event gets the comment text as its
  title). The rules file lives in your deployment, not in this repo.
- `auth.type` is `hmac_sha256` (hex digest of the raw body, `sha256=` prefix optional), `token`
  (the header value compared directly), or `query` (for systems that send no headers at all — the
  secret rides in the URL, `/hooks/tapd?token=...`). The secret comes from `secret_env` or inline
  `secret`.
- `project`: the project this source feeds. TAPD payloads carry no project field, so this is how
  they route; a `project` in the body still wins.
- Configured `auth` with no secret available rejects **everything** with 401 (fail closed).
- A source with `auth` no longer requires `X-Hub-Token`; sources without it still do, and an
  unknown source must still pass the global token.

**Do not guess field names for TAPD or a similar system.** Configure a lenient source (`map` may be
empty), point its webhook at `/hooks/tapd`, fire one real event, then read the stored `payload` from
`GET /api/tasks/{id}` and fill in `map` and the project's `fields`. A form-encoded
(`application/x-www-form-urlencoded`) delivery is accepted too; confirm the real auth header from
that same request before setting `auth`.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/hooks/{source}` | Parse an incoming hook into a task (`{task_id, created}`) |
| `POST` | `/api/tasks` | Create a task directly |
| `GET` | `/api/tasks?status=&stage=` | List tasks with their open approval |
| `GET` | `/api/tasks/{id}` | Task detail: approvals, events, runs |
| `POST` | `/api/tasks/{id}/wake` | Clear `next_run_at` so a pending task runs now |
| `POST` | `/api/tasks/{id}/cancel` | Cancel a pending/running/waiting task |
| `GET` | `/a/{token}` | Human-friendly approval page (HTML, phone-ready) |
| `POST` | `/a/{token}` | Submit `{"index":0}` / `{"index":1}` / `{"text":"…"}` |
| `GET` | `/api/board` | Board data (the UI polls this every 3 s) |
| `GET` | `/api/health` | `{ok, version, workers, queue}` |

Writes (`/hooks/*`, `/api/tasks/*`) require `X-Hub-Token: <api_token>`. Reads are unauthenticated
because the service binds to `127.0.0.1` by default. `/a/{token}` is protected by the approval's
own 24-character token, so the link can be forwarded to a phone or chat without exposing the API
token. No IM long-connection is involved.

### Approval semantics

- `{"index": 0}` → approved; the task advances exactly one stage and becomes `pending`.
- `{"index": 1}` → rejected; the task becomes `failed` with the decision in its events.
- `{"text": "..."}` (no index) → approved with a comment.
- Replaying the same decision returns the stored result with `changed: false` and never advances
  the task twice.

## Board

Single file, `web/index.html`, no CDN or build step — it works offline. Four columns
(待处理 / 进行中 / 等你审批 / 已完成), dark theme, single column on a phone. Waiting cards embed
the approval buttons and a free-text box; decisions go straight to `POST /a/{token}`.

## Adapters

The built-in M1 adapters are examples to copy:

- `sources`: `generic` (JSON field mapping in config, stable-hash dedupe)
- `agents`: `shell` / `command` — one config-driven adapter. It runs your command with the task in
  `HUB_*` env vars and can read structured output back, so Claude Code, Codex, `t3 thread start` or
  your own script are all just different blocks:

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

`parse` paths are dotted (`data.thread.id` works). `session` is stored on the task, `link` is a URL
for humans, and `output` replaces raw stdout in the run record. `exit_codes` maps a non-zero exit to
a status (`{"2": "pending"}` retries; anything unmapped fails). The default timeout is 60 s — raise
it for real agents.
- `notifiers`: `feishu` (self-built app, interactive card with url buttons — the default
  approval channel), `webhook` (POST/GET JSON to a URL), and `email` (stdlib `smtplib`,
  multipart/alternative HTML + plain text, SMTP_SSL or STARTTLS)

Contracts live in [`hub/adapters/base.py`](hub/adapters/base.py) as `typing.Protocol`s. Wiring in
your own system is one small `Source` file (about 30 lines) plus a few lines of config — or just
use `generic` with a field map. Stage handlers never touch the database themselves: a node returns
`{status, stage, delay, note}` and the engine persists it.

### Notifications

The `Notifier` interface is fixed (`notify` / `ask`, both returning `{ok, detail}`), so adding a
channel later does not change the core. Approvals always travel as a link (`/a/<token>`); a channel
only delivers that link to a human. M1 ships **`feishu` (the default approval channel)**, plus
`webhook` and `email`. The rules, enforced for every channel:

- **Offline-testable**: `HUB_OUTBOX=1` (or `dry_run: true`) sends nothing and writes the
  channel-format payload to `data/outbox/<time>-<channel>.json`. The acceptance script runs in this
  mode.
- **No duplicates**: one send per `(task_id, stage, notifier)`, guarded by a placeholder row.
- **Never blocks**: a failed send logs `events(kind=notify_error)` and retries once; the gate stays
  answerable on the board regardless.
- **Credentials** come only from config / environment (`HUB_EMAIL_PASSWORD`, webhook headers,
  `feishu.app_id / app_secret / receive_id`); none are stored in the repo.

`feishu` uses a self-built app: it fetches a `tenant_access_token` and posts an interactive card
(schema 2.0). The target is config-driven via `receive_id_type` (`chat_id` for a group, `open_id` /
`union_id` / `user_id` / `email` for a person) plus `receive_id` — the standard Feishu message API,
with no identity baked into the code. With missing credentials or no target it logs one warning and
falls back to the outbox instead of crashing.

**Two approval entry points, one state machine.** Every card carries a **去处理** url button
(`/a/<token>`) plus **打开看板**; the decision is applied by `store.decide()` no matter which door
it came through. With `inbound` on, the card also gets **通过 / 打回** buttons whose callback hits
the same `decide()` — same idempotency guard, same event trail. Replaying either one returns
"already handled" and never advances the task twice.

### Feishu inbound (optional)

Sending cards needs nothing extra. Listening to the bot — so a person can message it once to be
bound, or send a sentence to create a task, or press 通过 / 打回 on the card — needs the official
SDK and is strictly opt-in, so the core stays standard-library only:

```bash
pip install 'hookboard[feishu]'
```

Then set `"inbound": true` on the `feishu` notifier. In the Feishu console, the app needs the
**bot** capability, **event subscription in long-connection (WebSocket) mode** — no public URL or
callback address required — and the `im:message` / `im:message:send_as_bot` permissions. Enable the
events `im.message.receive_v1` and `card.action.trigger`.

What it does:

- **First contact binds.** Message the bot once (or send `/bind`) and its `sender.open_id` is
  written to `data/feishu_binding.json`. The notifier uses that target when `receive_id` is empty,
  so approvals start arriving without editing config.
- **Say it, it becomes work.** Any plain text message creates a task (`source=feishu`), deduped on
  the Feishu `message_id`; `/help` lists the commands.
- **Approve in chat or on the web.** Card buttons and the `/a/<token>` page both call
  `decide()`; whichever the human uses, the other shows it is already answered.
- **`allowed_open_ids`** (optional list) restricts who may create tasks or approve. Empty means
  anyone who can reach the bot — fine for a single-owner instance, set it for shared ones.

`lark-oapi` is imported lazily inside the gateway only; without it, `inbound: true` prints one
warning and the service runs normally.

## Validate it yourself

```bash
python3 -m unittest discover -s tests -v      # 73 tests: state machine + notifiers + inbound gateway + routing + webhook auth + stage steps
ACCEPT_PORT=8899 bash scripts/acceptance.sh   # 7 criteria + 5-minute demo + OUTBOX channels; curl + jq
```

The acceptance script installs the package into a throwaway venv, runs `hookboard` with
`config.example.json` verbatim in `HUB_OUTBOX=1` mode, drives the full loop, checks the outbox
payloads, and exits non-zero on the first failed assertion. Pick a free port with `ACCEPT_PORT` if
8899 is taken.

## Scope and maintenance

This is a tool for one job. It deliberately does **not** do: a ticket system, an IDE, built-in
model calls, multi-tenancy or a permission system, or distribution. One `api_token` is enough;
single-machine SQLite is enough.

MIT licensed. No SLA. Issues answered best-effort; pull requests welcome, but some will be
declined to keep the project small.