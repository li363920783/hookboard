"""Feishu (Lark) self-built-app notifier — docs/CHANNELS.md §1, M1 default channel.

Pure ``urllib``; no long-connection / event subscription.  Approvals travel as a
link (``/a/<token>``); the interactive card only carries two url buttons.

Flow (real send):
  1. POST /open-apis/auth/v3/tenant_access_token/internal  {app_id, app_secret}
  2. POST /open-apis/im/v1/messages?receive_id_type=chat_id
     Authorization: Bearer <tenant_access_token>
     {receive_id, msg_type: "interactive", content: "<card JSON string>"}

Offline: when ``HUB_OUTBOX=1`` / ``dry_run`` is set, or credentials are missing,
no request is made — the full send request body is written to
``data/outbox/<ts>-feishu.json``.  Missing credentials log a single warning and
never crash or block the task.

Credentials come only from config (``notifiers.feishu.app_id / app_secret /
chat_id / enabled``); no real value is stored in the repo.
"""

from __future__ import annotations

import json
import sys
import urllib.request

from .base import outbox_enabled, write_outbox

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"


class FeishuNotifier:
    name = "feishu"

    def __init__(self, entry: dict | None = None, outbox_dir: str = "data/outbox"):
        entry = entry or {}
        self.enabled = bool(entry.get("enabled", True))
        self.app_id = entry.get("app_id") or ""
        self.app_secret = entry.get("app_secret") or ""
        self.chat_id = entry.get("chat_id") or ""
        self.timeout = float(entry.get("timeout", 10))
        self.dry_run = outbox_enabled(entry)
        self.outbox_dir = outbox_dir
        self.context: dict = {}
        self._warned = False

    @property
    def has_creds(self) -> bool:
        return bool(self.app_id and self.app_secret and self.chat_id)

    # ------------------------------------------------------------------ card

    def _card(self, title: str, url: str) -> dict:
        ctx = self.context
        public_base = (ctx.get("public_base") or url.split("/a/")[0]).rstrip("/")
        token = ctx.get("token") or url.rsplit("/", 1)[-1]
        task_title = ctx.get("task_title") or title
        source = ctx.get("source") or "?"
        external_id = ctx.get("external_id") or "?"
        stage = ctx.get("stage") or "?"
        question = ctx.get("question") or title

        approval_url = f"{public_base}/a/{token}"
        board_url = f"{public_base}/"
        board_api = f"{public_base}/api/board"
        markdown = (
            f"**{task_title}**\n"
            f"来源: {source} · 条目: {external_id}\n"
            f"阶段: {stage}\n"
            f"等你决定: {question}\n"
            f"看板数据: {board_api}"
        )
        return {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "Hookboard 审批"}},
            "body": {
                "elements": [
                    {"tag": "markdown", "content": markdown},
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "type": "default",
                                "text": {"tag": "plain_text", "content": "去处理"},
                                "multi_url": {"url": approval_url},
                            },
                            {
                                "tag": "button",
                                "type": "default",
                                "text": {"tag": "plain_text", "content": "打开看板"},
                                "multi_url": {"url": board_url},
                            },
                        ],
                    },
                ]
            },
        }

    def _request_body(self, title: str, url: str) -> dict:
        card = self._card(title, url)
        return {
            "receive_id": self.chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }

    # ------------------------------------------------------------- interface

    def notify(self, title: str, body_md: str, links: list[tuple[str, str]]) -> dict:
        url = links[0][1] if links else ""
        return self._deliver(title, url)

    def ask(self, title: str, body_md: str, url: str, options: list[str]) -> dict:
        return self._deliver(title, url)

    def _deliver(self, title: str, url: str) -> dict:
        if not self.enabled:
            return {"ok": True, "detail": "feishu disabled"}
        payload = self._request_body(title, url)
        if self.dry_run or not self.has_creds:
            if not self.has_creds and not self.dry_run and not self._warned:
                self._warned = True
                print(
                    "warn: feishu credentials missing (app_id/app_secret/chat_id); "
                    "writing to outbox instead of sending",
                    file=sys.stderr,
                )
            # Outbox artifact = the exact send body plus task/stage metadata so
            # the acceptance can attribute and dedup it. The real send stays pure.
            artifact = dict(payload)
            artifact["task_id"] = self.context.get("task_id")
            artifact["stage"] = self.context.get("stage")
            path = write_outbox(self.outbox_dir, "feishu", artifact)
            return {"ok": True, "detail": f"outbox:{path}"}
        return self._send(payload)

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

    def _send(self, payload: dict) -> dict:
        try:
            token = self._tenant_token()
            request = urllib.request.Request(
                SEND_URL,
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