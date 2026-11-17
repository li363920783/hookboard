"""HTTP API — Python standard library only (``http.server``).

Read endpoints are unauthenticated on purpose: the service binds to 127.0.0.1
by default.  Write endpoints require ``X-Hub-Token``.  Approval links carry
their own per-approval token, so a link can be forwarded to a phone without
handing out the API token.
"""

from __future__ import annotations

import hmac
import html
import json
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, routing
from .adapters import load_source

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_BODY = 5 * 1024 * 1024


class HubServer:
    def __init__(self, store, engine, config):
        self.store = store
        self.engine = engine
        self.config = config
        self.sources = {
            name: load_source(name, entry)
            for name, entry in (config.sources or {}).items()
        }

    # ------------------------------------------------------------- helpers

    @staticmethod
    def task_card(task: dict, approval: dict | None) -> dict:
        card = {
            key: task.get(key)
            for key in (
                "id",
                "source",
                "kind",
                "external_id",
                "title",
                "lane",
                "status",
                "stage",
                "priority",
                "note",
                "attempts",
                "next_run_at",
                "created_at",
                "updated_at",
            )
        }
        card["approval"] = approval
        return card

    def board(self) -> dict:
        tasks = self.store.list_tasks(limit=500)
        approvals = self.store.open_approvals([task["id"] for task in tasks])
        columns = [
            ("pending", "待处理"),
            ("running", "进行中"),
            ("waiting", "等你审批"),
            ("done", "已完成"),
        ]
        counts = {key: 0 for key, _ in columns}
        for task in tasks:
            if task["status"] in counts:
                counts[task["status"]] += 1
        return {
            "ok": True,
            "columns": [
                {
                    "key": key,
                    "title": title,
                    "tasks": [
                        self.task_card(task, approvals.get(task["id"]))
                        for task in tasks
                        if task["status"] == key
                    ],
                }
                for key, title in columns
            ],
            "counts": counts,
            "generated_at": time.time(),
        }

    def resolve_project(self, body: dict, default: str = "") -> str:
        """Project name for an incoming body; unknown names are a hard error."""
        if not self.config.projects:
            return ""  # no projects configured -> project is meaningless, not an error
        name = routing.resolve(self.config, body) or default
        if name and name not in self.config.projects:
            known = ", ".join(sorted(self.config.projects)) or "(none configured)"
            raise ValueError(f"unknown project {name!r}; configured: {known}")
        return name

    def create_task(self, body: dict) -> dict:
        external_id = str(body.get("external_id") or uuid.uuid4().hex)
        task, created = self.store.create_task(
            source=str(body.get("source") or "api"),
            kind=str(body.get("kind") or "task"),
            external_id=external_id,
            title=str(body.get("title") or external_id),
            lane=str(body.get("lane") or ""),
            priority=int(body.get("priority") or 0),
            payload=body.get("payload") or {},
            project=self.resolve_project(body),
            stage=self.config.stages[0],
        )
        return {"ok": True, "task_id": task["id"], "created": created, "task": task}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hub: HubServer  # injected by make_server()

    # ----------------------------------------------------------- plumbing

    def log_message(self, fmt, *args):  # keep one tidy line per request
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200):
        self._send(
            code,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _html(self, text: str, code: int = 200):
        self._send(code, text.encode("utf-8"), "text/html; charset=utf-8")

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            raise ValueError("request body too large")
        return self.rfile.read(length) if length > 0 else b""

    def _body(self):
        return self._decode_body(self._read_body())

    def _decode_body(self, raw: bytes):
        if not raw:
            return {}
        content_type = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in content_type:
            return json.loads(raw.decode("utf-8"))
        if "application/x-www-form-urlencoded" in content_type:
            parsed = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            return {key: values[-1] for key, values in parsed.items()}
        raise ValueError(f"unsupported content type: {content_type or 'none'}")

    def _authorized(self) -> bool:
        expected = self.hub.config.api_token
        if not expected:
            return True
        supplied = self.headers.get("X-Hub-Token") or ""
        return hmac.compare_digest(supplied, expected)

    def _require_token(self) -> bool:
        if self._authorized():
            return True
        self._json({"ok": False, "error": "unauthorized: send X-Hub-Token"}, 401)
        return False

    def _path(self) -> tuple[str, dict]:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        return path, query

    @staticmethod
    def _one(query: dict, key: str) -> str | None:
        values = query.get(key)
        return values[-1] if values else None

    def _wants_json(self) -> bool:
        accept = self.headers.get("Accept") or ""
        content_type = (self.headers.get("Content-Type") or "").lower()
        return "application/json" in accept or "application/json" in content_type

    # -------------------------------------------------------------- GET

    def do_GET(self):
        try:
            self._get()
        except Exception as exc:
            traceback.print_exc()
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def _get(self):
        hub = self.hub
        path, query = self._path()

        if path in ("/", "/index.html"):
            index = WEB_DIR / "index.html"
            if not index.is_file():
                return self._json({"ok": False, "error": "web/index.html missing"}, 500)
            return self._send(200, index.read_bytes(), "text/html; charset=utf-8")

        if path == "/api/health":
            return self._json(
                {
                    "ok": True,
                    "version": __version__,
                    "workers": hub.config.workers,
                    "queue": hub.store.pending_count(),
                    "time": time.time(),
                }
            )

        if path == "/api/board":
            return self._json(hub.board())

        if path == "/api/tasks":
            tasks = hub.store.list_tasks(
                status=self._one(query, "status"), stage=self._one(query, "stage")
            )
            approvals = hub.store.open_approvals([task["id"] for task in tasks])
            return self._json(
                {
                    "ok": True,
                    "tasks": [
                        hub.task_card(task, approvals.get(task["id"])) for task in tasks
                    ],
                }
            )

        if path.startswith("/api/tasks/"):
            try:
                task_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._json({"ok": False, "error": "bad task id"}, 400)
            detail = hub.store.task_detail(task_id)
            if detail is None:
                return self._json({"ok": False, "error": "task not found"}, 404)
            detail["ok"] = True
            return self._json(detail)

        if path.startswith("/a/"):
            token = path[len("/a/"):]
            approval = hub.store.get_approval_by_token(token)
            if approval is None:
                return self._html(
                    _page("审批链接无效", "<p>链接不存在或已失效。</p>"), 404
                )
            task = hub.store.get_task(approval["task_id"])
            return self._html(_approval_page(approval, task))

        return self._json({"ok": False, "error": "not found"}, 404)

    # -------------------------------------------------------------- POST

    def do_POST(self):
        try:
            self._post()
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            traceback.print_exc()
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def _post(self):
        hub = self.hub
        path, _query = self._path()

        if path.startswith("/a/"):
            return self._decide(path[len("/a/"):])

        if path.startswith("/hooks/"):
            return self._hook(path[len("/hooks/"):])

        if not self._require_token():
            return

        if path == "/api/tasks":
            body = self._body()
            try:
                return self._json(hub.create_task(body if isinstance(body, dict) else {}))
            except (ValueError, ImportError) as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)

        if path.startswith("/api/tasks/"):
            parts = path.strip("/").split("/")
            if len(parts) != 4 or parts[3] not in ("wake", "cancel"):
                return self._json({"ok": False, "error": "not found"}, 404)
            try:
                task_id = int(parts[2])
            except ValueError:
                return self._json({"ok": False, "error": "bad task id"}, 400)
            if parts[3] == "wake":
                task = hub.store.wake_task(task_id)
            else:
                task = hub.store.cancel_task(task_id)
            if task is None:
                return self._json({"ok": False, "error": "task not found"}, 404)
            return self._json({"ok": True, "task": task})

        return self._json({"ok": False, "error": "not found"}, 404)

    def _hook(self, name: str):
        hub = self.hub
        source = hub.sources.get(name)
        if source is None:
            if not self._require_token():
                return
            return self._json({"ok": False, "error": f"unknown source: {name}"}, 404)
        raw = self._read_body()
        # A source with its own auth (GitHub HMAC, GitLab token) replaces the
        # shared X-Hub-Token, since external systems cannot send our header.
        if getattr(source, "protected", False):
            _, query = self._path()
            if not source.verify(raw, dict(self.headers), query):
                return self._json({"ok": False, "error": "unauthorized"}, 401)
        elif not self._require_token():
            return
        body = self._decode_body(raw)
        parsed = source.parse(
            body if isinstance(body, dict) else {}, dict(self.headers)
        )
        if not parsed:
            return self._json({"ok": True, "task_id": None, "created": False, "ignored": True})
        try:
            # A source may declare the project it feeds ("project": "tapd"),
            # so a webhook whose body carries no project field still lands in
            # the right place.
            source_project = str(
                ((hub.config.sources or {}).get(name) or {}).get("project") or ""
            )
            project = hub.resolve_project(
                body if isinstance(body, dict) else {}, default=source_project
            )
        except (ValueError, ImportError) as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        task, created = hub.store.create_task(
            source=str(parsed.get("source") or name),
            kind=str(parsed.get("kind") or "hook"),
            external_id=str(parsed["external_id"]),
            title=str(parsed.get("title") or parsed["external_id"]),
            priority=int(parsed.get("priority") or 0),
            payload=parsed.get("payload") or body,
            project=project,
            stage=hub.config.stages[0],
        )
        return self._json({"ok": True, "task_id": task["id"], "created": created, "task": task})

    def _decide(self, token: str):
        hub = self.hub
        body = self._body()
        if not isinstance(body, dict):
            body = {}
        index = body.get("index")
        if index in (None, ""):
            index = None
        else:
            try:
                index = int(index)
            except (TypeError, ValueError):
                return self._json({"ok": False, "error": "index must be an integer"}, 400)
        text = body.get("text")
        if isinstance(text, list):
            text = text[-1] if text else None

        approval = hub.store.get_approval_by_token(token)
        if approval is None:
            if self._wants_json():
                return self._json({"ok": False, "error": "approval not found"}, 404)
            return self._html(_page("审批链接无效", "<p>链接不存在或已失效。</p>"), 404)

        try:
            approval, task, changed = hub.engine.decide(
                token,
                index=index,
                text=str(text) if text is not None else None,
                by=str(body.get("by") or ""),
            )
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)

        if self._wants_json():
            return self._json(
                {"ok": True, "changed": changed, "approval": approval, "task": task}
            )
        return self._html(_approval_result_page(approval, task, changed))


# ------------------------------------------------------------------- pages

_PAGE_STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: #0b0f14; color: #e6edf3; padding: 20px;
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Microsoft YaHei", sans-serif;
}
main {
  width: 100%; max-width: 520px; background: #121a22; border: 1px solid #223040;
  border-radius: 16px; padding: 24px;
}
.badge {
  display: inline-block; font-size: 12px; letter-spacing: .08em; text-transform: uppercase;
  color: #7dd3fc; background: #0b2a3a; border-radius: 999px; padding: 4px 10px;
}
h1 { font-size: 20px; margin: 14px 0 6px; }
p { margin: 6px 0; color: #9fb0c0; white-space: pre-wrap; }
.meta { font-size: 13px; color: #6b7d8f; }
.options { display: flex; gap: 10px; margin: 18px 0; flex-wrap: wrap; }
button {
  font: inherit; border-radius: 10px; border: 1px solid #2b3d4f; background: #17232e;
  color: #e6edf3; padding: 10px 18px; cursor: pointer;
}
button.primary { background: #1d4ed8; border-color: #2563eb; }
button.send { margin-top: 10px; width: 100%; }
label { display: block; font-size: 13px; color: #7d8f9f; margin-top: 16px; }
textarea {
  width: 100%; margin-top: 6px; padding: 10px; border-radius: 10px;
  border: 1px solid #2b3d4f; background: #0d141b; color: #e6edf3;
  font: inherit; resize: vertical;
}
.verdict { font-size: 18px; font-weight: 600; color: #4ade80; margin: 14px 0; }
.verdict.rejected { color: #f87171; }
details { margin-top: 16px; }
details summary { cursor: pointer; color: #7dd3fc; font-size: 13px; }
pre {
  white-space: pre-wrap; word-break: break-all; font-size: 12px; line-height: 1.5;
  background: #0d141b; border: 1px solid #2b3d4f; border-radius: 10px;
  padding: 10px; max-height: 60vh; overflow: auto;
}
a { color: #7dd3fc; }
"""


def _page(title: str, inner: str, style_extra: str = "") -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · Hookboard</title>
<style>{_PAGE_STYLE}{style_extra}</style>
</head>
<body><main>{inner}</main></body>
</html>"""


def _raw_payload_block(task: dict | None) -> str:
    """The hook body verbatim, so a short card summary never hides information."""
    payload = (task or {}).get("payload")
    if not payload:
        return ""
    try:
        blob = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        blob = str(payload)
    if len(blob) > 20000:
        blob = blob[:20000] + "\n… （已截断）"
    return (
        "<details><summary>完整原始内容（钩子原样）</summary>"
        f"<pre>{html.escape(blob)}</pre></details>"
    )


def _approval_page(approval: dict, task: dict | None) -> str:
    token = html.escape(approval["token"], quote=True)
    question = html.escape(approval["question"])
    detail = html.escape(approval["detail"] or "")
    stage = html.escape(approval["stage"])
    task_title = html.escape((task or {}).get("title") or "")
    task_id = approval["task_id"]
    options = approval["options"] or ["通过", "打回"]

    if approval["status"] != "open":
        answered = html.escape(approval["answer_text"] or "")
        return _page(
            "已处理",
            f'<span class="badge">Hookboard</span>'
            f"<h1>{question}</h1>"
            f'<p class="verdict">此前已处理</p>'
            f"<p>答复：{answered}</p>"
            f'<p class="meta">任务 #{task_id} · {task_title} · 阶段 {stage}</p>'
            + _raw_payload_block(task),
        )

    buttons = []
    for position, option in enumerate(options):
        css = "primary" if position == (approval["recommend"] or 0) else ""
        buttons.append(
            f'<button class="{css}" name="index" value="{position}">'
            f"{html.escape(str(option))}</button>"
        )
    return _page(
        "审批",
        f'<span class="badge">Hookboard 审批</span>'
        f"<h1>{question}</h1>"
        + (f"<p>{detail}</p>" if detail else "")
        + f'<p class="meta">任务 #{task_id} · {task_title} · 阶段 {stage}</p>'
        + _raw_payload_block(task)
        + f'<form method="post" action="/a/{token}" '
        f"onsubmit=\"this.querySelectorAll('button').forEach(function(b){{b.disabled=true}})\">"
        f'<div class="options">{"".join(buttons)}</div>'
        f"<label>或者直接说</label>"
        f'<textarea name="text" rows="3" placeholder="补充意见（提交即视为通过）"></textarea>'
        f'<button class="send">提交这段话</button>'
        f"</form>",
    )


def _approval_result_page(approval: dict | None, task: dict | None, changed: bool) -> str:
    if approval is None:
        return _page("审批", "<p>审批不存在。</p>")
    index = approval.get("answer_index")
    approved = index in (None, 0)
    verdict = "已通过，任务继续推进" if approved else "已打回，任务终止"
    css = "" if approved else " rejected"
    answer = html.escape(approval.get("answer_text") or "")
    status = html.escape((task or {}).get("status") or "")
    stage = html.escape((task or {}).get("stage") or "")
    note = "" if changed else "<p>该审批此前已处理，本次未重复推进流程。</p>"
    return _page(
        "审批结果",
        f'<span class="badge">Hookboard 审批</span>'
        f'<h1>{html.escape(approval["question"])}</h1>'
        f'<p class="verdict{css}">{verdict}</p>'
        + (f"<p>答复：{answer}</p>" if answer else "")
        + f'<p class="meta">任务 #{approval["task_id"]} · 状态 {status} · 阶段 {stage}</p>'
        + note
        + '<p><a href="/">回到看板</a></p>',
    )


def make_server(store, engine, config) -> ThreadingHTTPServer:
    hub = HubServer(store, engine, config)
    handler = type("HubRequestHandler", (Handler,), {"hub": hub})
    httpd = ThreadingHTTPServer((config.bind, config.port), handler)
    httpd.daemon_threads = True
    return httpd