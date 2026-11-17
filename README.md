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

- Relative database paths resolve against the config file's directory.
- `public_base` builds the approval links; point it at your reverse proxy or LAN address.
- `dry_run: true` (or `HUB_OUTBOX=1`) makes a channel write its payload to `data/outbox/`
  instead of sending — the offline default shipped in the example.

Environment overrides (win over the file):
`HUB_CONFIG`, `HUB_BIND`, `HUB_PORT`, `HUB_API_TOKEN`, `HUB_PUBLIC_BASE`, `HUB_WORKERS`,
`HUB_POLL_INTERVAL`, `HUB_DB`, `HUB_OUTBOX`, `HUB_EMAIL_PASSWORD`.

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
- `agents`: `shell` (runs one command; task data arrives via `HUB_*` env vars, stdout becomes the run output)
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
  `feishu.app_id / app_secret / chat_id`); none are stored in the repo.

`feishu` uses a self-built app: it fetches a `tenant_access_token` and posts an interactive card
(schema 2.0) with two url buttons — **去处理** (`/a/<token>`) and **打开看板** (`/`). It never uses a
long-connection or event subscription to collect approvals; the decision always happens on the web
page. With missing credentials it logs one warning and falls back to the outbox instead of crashing.

## Validate it yourself

```bash
python3 -m unittest discover -s tests -v      # state machine + notifier outbox/dedup/retry tests
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