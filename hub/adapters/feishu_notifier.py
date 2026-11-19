"""Feishu (Lark) self-built-app notifier — docs/CHANNELS.md §1, M1 default channel.

Pure ``urllib``; sending never needs a long-connection or event subscription.
Approvals can be answered two ways, both ending in the same ``store.decide()``:

  1. the web token link (``/a/<token>``) carried in the card;
  2. the card's own 通过 / 打回 buttons — only when ``inbound`` is on, because
     those need the optional WebSocket gateway (``hub/feishu_gateway.py``) to
     receive the card-action callback.

Flow (real send):
  1. POST /open-apis/auth/v3/tenant_access_token/internal  {app_id, app_secret}
  2. POST /open-apis/im/v1/messages?receive_id_type=<chat_id|open_id|union_id|user_id|email>
     Authorization: Bearer <tenant_access_token>
     {receive_id, msg_type: "interactive", content: "<card JSON string>"}

Offline: when ``HUB_OUTBOX=1`` / ``dry_run`` is set, or the target is unknown,
no request is made — the full send request body is written to
``data/outbox/<ts>-feishu.json`` and one warning is logged.  Credentials and
target come only from config or from the binding file the inbound gateway
writes on first contact; no real value and no identity is stored in the repo.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from .base import outbox_enabled, write_outbox

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
BINDING_FILENAME = "feishu_binding.json"
# Human labels for the gates that produce a card, so plan vs review read apart.
STAGE_LABELS = {"plan": "开工前确认", "review": "完成后复核"}


# --------------------------------------------------------------- binding file
# Written by the inbound gateway when a person messages the bot.  Kept next to
# the database (not in the repo) so a fresh clone has no identity of any kind.

def binding_path_for(outbox_dir: str) -> Path:
    return Path(outbox_dir).parent / BINDING_FILENAME


def read_binding(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_binding(path: str | Path, **fields) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = read_binding(path)
    current.update({k: v for k, v in fields.items() if v})
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


class FeishuNotifier:
    name = "feishu"

    def __init__(self, entry: dict | None = None, outbox_dir: str = "data/outbox"):
        entry = entry or {}
        self.enabled = bool(entry.get("enabled", True))
        self.app_id = entry.get("app_id") or ""
        self.app_secret = entry.get("app_secret") or ""
        # Standard Feishu im/v1/messages target: chat_id (group) or open_id /
        # union_id / user_id / email (P2P). Pure config; no identity baked in.
        self.receive_id_type = entry.get("receive_id_type") or "chat_id"
        self.receive_id = entry.get("receive_id") or entry.get("chat_id") or ""
        self.inbound = bool(entry.get("inbound", False))
        self.allowed_open_ids = list(entry.get("allowed_open_ids") or [])
        self.timeout = float(entry.get("timeout", 10))
        self.dry_run = outbox_enabled(entry)
        self.outbox_dir = outbox_dir
        self.binding_path = binding_path_for(outbox_dir)
        self.context: dict = {}
        self._warned = False

    # ---------------------------------------------------------------- target

    def resolve_target(self) -> tuple[str, str]:
        """Explicit config wins; otherwise fall back to the inbound binding."""
        if self.receive_id:
            return self.receive_id_type, self.receive_id
        bound = read_binding(self.binding_path).get("open_id") or ""
        return ("open_id", bound) if bound else (self.receive_id_type, "")

    @property
    def has_creds(self) -> bool:
        _, receive_id = self.resolve_target()
        return bool(self.app_id and self.app_secret and receive_id)

    # ------------------------------------------------------------------ card

    def _card(self, title: str, url: str, body_md: str = "", notice: bool = False) -> dict:
        ctx = self.context
        public_base = (ctx.get("public_base") or url.split("/a/")[0]).rstrip("/")
        token = ctx.get("token") or url.rsplit("/", 1)[-1]
        task_title = ctx.get("task_title") or title
        source = ctx.get("source") or "?"
        external_id = ctx.get("external_id") or "?"
        stage = ctx.get("stage") or "?"
        question = ctx.get("question") or title

        if notice:
            # A finished task is a report, not a gate: title + outcome + link,
            # no decision buttons.
            kind = str(ctx.get("notice_kind") or "done")
            # Three kinds: the task ended well, it ended badly, or a long
            # session stage (release) just started and will report later.
            label, template = {
                "done": ("任务完成", "green"),
                "failed": ("任务失败", "red"),
            }.get(kind, ("进度通知", "blue"))
            link = url or f"{public_base}/"
            markdown = f"**{task_title}**\n\n{(body_md or '').strip()[:7000]}\n\n[打开看板]({link})"
            return {
                "schema": "2.0",
                "header": {
                    "title": {"tag": "plain_text", "content": f"Hookboard · {label}"},
                    "template": template,
                },
                "body": {
                    "elements": [
                        {"tag": "markdown", "content": markdown},
                        {
                            "tag": "button",
                            "type": "default",
                            "text": {"tag": "plain_text", "content": "打开看板"},
                            "behaviors": [{"type": "open_url", "default_url": link}],
                        },
                    ]
                },
            }

        approval_url = f"{public_base}/a/{token}"
        board_url = f"{public_base}/"
        # Two gates per task can carry the same ticket text; label the stage so
        # the second card does not read as a duplicate push.
        stage_label = STAGE_LABELS.get(str(stage), str(stage))
        template = "orange" if str(stage) == "review" else "blue"
        # The engine's digest already carries source / ticket / fields; only
        # fall back to the bare context lines when nobody passed a body.
        if body_md:
            header = body_md.strip()[:7000]
        else:
            header = f"来源: {source} · 条目: {external_id}"
        markdown = (
            f"**{task_title}**\n\n"
            f"{header}\n\n"
            f"**阶段**：{stage}（{stage_label}）\n"
            f"**等你决定**：{question}\n"
            f"**看板**：[打开看板]({board_url})\n\n"
            "> 字段超出卡片长度会被截断，完整原文见「去处理」页。"
        )
        if self.inbound:
            markdown += "\n可在卡片上写意见再点「通过 / 打回」，也可点「去处理」在网页上处理。"

        # Card schema 2.0 dropped the `action` container (Feishu rejects it with
        # 230099 "unsupported tag action"); buttons are top-level elements now.
        def open_url_button(label: str, url: str) -> dict:
            return {
                "tag": "button",
                "type": "default",
                "text": {"tag": "plain_text", "content": label},
                "behaviors": [{"type": "open_url", "default_url": url}],
            }

        elements = [{"tag": "markdown", "content": markdown}]
        if self.inbound:
            # Form so the person can leave a reason with the decision: Feishu
            # collects the input and returns it as action.form_value.  A form
            # must sit at the card root, and buttons inside it use
            # form_action_type=submit instead of behaviors.
            elements.append(
                {
                    "tag": "form",
                    "name": "approval_form",
                    "elements": [
                        {
                            "tag": "input",
                            "name": "comment",
                            "max_length": 300,
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "审批意见（可选，打回时建议写明原因）",
                            },
                        },
                        {
                            "tag": "button",
                            "type": "primary",
                            "name": f"{token}:0",
                            "form_action_type": "submit",
                            "value": {"token": token, "index": 0},
                            "text": {"tag": "plain_text", "content": "通过"},
                        },
                        {
                            "tag": "button",
                            "type": "danger",
                            "name": f"{token}:1",
                            "form_action_type": "submit",
                            "value": {"token": token, "index": 1},
                            "text": {"tag": "plain_text", "content": "打回"},
                        },
                    ],
                }
            )
        elements += [
            open_url_button("去处理", approval_url),
            open_url_button("打开看板", board_url),
        ]
        return {
            "schema": "2.0",
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"Hookboard 审批 · {stage_label}",
                },
                "template": template,
            },
            "body": {"elements": elements},
        }

    def _request_body(self, title: str, url: str, body_md: str = "", notice: bool = False) -> dict:
        card = self._card(title, url, body_md, notice=notice)
        _, receive_id = self.resolve_target()
        return {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }

    # ------------------------------------------------------------- interface

    def notify(self, title: str, body_md: str, links: list[tuple[str, str]]) -> dict:
        url = links[0][1] if links else ""
        return self._deliver(title, url, body_md, notice=True)

    def ask(self, title: str, body_md: str, url: str, options: list[str]) -> dict:
        return self._deliver(title, url, body_md)

    def _deliver(self, title: str, url: str, body_md: str = "", notice: bool = False) -> dict:
        if not self.enabled:
            return {"ok": True, "detail": "feishu disabled"}
        receive_id_type, receive_id = self.resolve_target()
        payload = self._request_body(title, url, body_md, notice=notice)
        if self.dry_run or not receive_id:
            self._warn_missing(receive_id)
            # Outbox artifact = the exact send body plus task/stage metadata so
            # the acceptance can attribute and dedup it. The real send stays pure.
            artifact = dict(payload)
            artifact["task_id"] = self.context.get("task_id")
            artifact["stage"] = self.context.get("stage")
            path = write_outbox(self.outbox_dir, "feishu", artifact)
            return {"ok": True, "detail": f"outbox:{path}"}
        return self._send(payload, receive_id_type)

    def _warn_missing(self, receive_id: str) -> None:
        if self.dry_run or receive_id or self._warned:
            return
        self._warned = True
        print(
            "warn: feishu target missing (set notifiers.feishu.receive_id, or message the "
            "bot once with inbound enabled); writing to outbox instead of sending",
            file=sys.stderr,
        )

    # ------------------------------------------------------- plain text reply

    def send_text(self, text: str, *, to: str, to_type: str = "chat_id") -> dict:
        """Send a plain text message (used for gateway confirmations)."""
        if not (self.app_id and self.app_secret and to):
            return {"ok": False, "detail": "feishu text: credentials or target missing"}
        payload = {
            "receive_id": to,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        if self.dry_run:
            path = write_outbox(self.outbox_dir, "feishu-text", payload)
            return {"ok": True, "detail": f"outbox:{path}"}
        return self._send(payload, to_type)

    # -------------------------------------------------------------- real send

    def _tenant_token(self) -> str:
        request = urllib.request.Request(
            TOKEN_URL,
            data=json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        if body.get("code") != 0:
            raise RuntimeError(f"tenant_access_token failed: {body.get('msg')}")
        return body["tenant_access_token"]

    def _send(self, payload: dict, receive_id_type: str | None = None) -> dict:
        try:
            token = self._tenant_token()
            request = urllib.request.Request(
                f"{SEND_URL}?receive_id_type={receive_id_type or self.receive_id_type}",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            ok = body.get("code") == 0
            return {"ok": ok, "detail": body.get("msg") or f"code={body.get('code')}"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}


def register(entry: dict | None = None, outbox_dir: str = "data/outbox") -> FeishuNotifier:
    return FeishuNotifier(entry, outbox_dir)