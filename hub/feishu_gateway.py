"""Optional Feishu inbound channel: WebSocket long connection via lark-oapi.

This is the only place Hookboard reaches for a third-party package, and only
when the user opts in:

    pip install 'hookboard[feishu]'
    "notifiers": {"feishu": {"inbound": true, ...}}

The core stays standard-library only: this module imports ``lark_oapi`` lazily
inside :meth:`FeishuGateway.start`, and the rest of the codebase never imports
it at all.

What the long connection is *for* — inbound only (never the approval exchange):
  * a person messages the bot once  -> their open_id is written to
    ``data/feishu_binding.json`` so the notifier knows where to send;
  * a plain text message            -> a task is created (message_id dedupes);
  * a card button (通过 / 打回)      -> ``engine.decide(token, index)`` — the
    exact same call the web token link uses, so both entry points share one
    state machine and one idempotency guard.
"""

from __future__ import annotations

import json
import sys
import threading

from .adapters.feishu_notifier import FeishuNotifier, read_binding, write_binding

HELP = (
    "Hookboard 已绑定。\n"
    "· 直接发一句话 → 建任务\n"
    "· /bind → 重新绑定当前账号\n"
    "· /help → 显示这条帮助\n"
    "审批可在卡片上点「通过 / 打回」，或点「去处理」在网页上处理。"
)


def parse_message(data) -> dict | None:
    """Pull the fields we need out of a P2ImMessageReceiveV1 event.

    Written with ``getattr`` so it is testable without the SDK installed.
    """
    event = getattr(data, "event", None)
    sender = getattr(event, "sender", None)
    message = getattr(event, "message", None)
    if sender is None or message is None:
        return None
    if (getattr(sender, "sender_type", "") or "") == "app":
        return None  # never react to other bots, or two bots could ping-pong
    sender_id = getattr(sender, "sender_id", None)
    raw = getattr(message, "content", "") or ""
    try:
        text = json.loads(raw).get("text", "") if raw.lstrip().startswith("{") else raw
    except (ValueError, AttributeError):
        text = raw
    return {
        "message_id": getattr(message, "message_id", "") or "",
        "chat_id": getattr(message, "chat_id", "") or "",
        "chat_type": getattr(message, "chat_type", "") or "",
        "message_type": getattr(message, "message_type", "") or "",
        "open_id": getattr(sender_id, "open_id", "") or "",
        "user_id": getattr(sender_id, "user_id", "") or "",
        "text": (text or "").strip(),
    }


def parse_card_action(data) -> dict | None:
    """Pull ``{token, index, open_id}`` out of a card-action callback."""
    event = getattr(data, "event", None)
    operator = getattr(event, "operator", None)
    action = getattr(event, "action", None)
    value = getattr(action, "value", None) or {}
    if not isinstance(value, dict) or "token" not in value:
        return None
    try:
        index = int(value.get("index", 0))
    except (TypeError, ValueError):
        return None
    return {
        "token": str(value["token"]),
        "index": index,
        "open_id": getattr(operator, "open_id", "") or "",
    }


class FeishuGateway:
    def __init__(self, store, engine, entry: dict, *, stage: str, outbox_dir: str):
        self.store = store
        self.engine = engine
        self.entry = entry or {}
        self.stage = stage
        self.notifier = FeishuNotifier(self.entry, outbox_dir)
        self.reply = FeishuNotifier(self.entry, outbox_dir)
        self.binding_path = self.notifier.binding_path
        self.allowed = set(self.entry.get("allowed_open_ids") or [])
        self._client = None
        self._thread = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> bool:
        try:
            import lark_oapi as lark
        except ImportError:
            print(
                "warn: feishu inbound is on but lark-oapi is not installed; "
                "run: pip install 'hookboard[feishu]'",
                file=sys.stderr,
            )
            return False

        app_id = self.entry.get("app_id") or ""
        app_secret = self.entry.get("app_secret") or ""
        if not (app_id and app_secret):
            print(
                "warn: feishu inbound is on but app_id/app_secret are empty",
                file=sys.stderr,
            )
            return False

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .register_p2_card_action_trigger(self._on_card_action)
            .build()
        )
        self._client = lark.ws.Client(
            app_id,
            app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.WARNING,
        )
        self._thread = threading.Thread(target=self._client.start, name="feishu-ws", daemon=True)
        self._thread.start()
        return True

    def _allowed(self, open_id: str) -> bool:
        return not self.allowed or open_id in self.allowed

    # --------------------------------------------------------------- inbound

    def _on_message(self, data) -> None:
        msg = parse_message(data)
        if not msg or msg["message_type"] != "text" or not msg["text"]:
            return
        if not self._allowed(msg["open_id"]):
            return
        write_binding(
            self.binding_path,
            open_id=msg["open_id"],
            user_id=msg["user_id"],
            chat_id=msg["chat_id"],
        )

        text = msg["text"]
        target = msg["chat_id"] or msg["open_id"]
        target_type = "chat_id" if msg["chat_id"] else "open_id"
        if text.startswith("/bind"):
            self.reply.send_text("已绑定当前账号，之后的审批卡片会发给你。", to=target, to_type=target_type)
            return
        if text.startswith("/help"):
            self.reply.send_text(HELP, to=target, to_type=target_type)
            return
        if text.startswith("/"):
            self.reply.send_text("可用命令：/bind、/help；或直接发一句话建任务。", to=target, to_type=target_type)
            return

        task, created = self.store.create_task(
            source="feishu",
            kind="im",
            external_id=msg["message_id"] or msg["open_id"],
            title=text[:200],
            payload={"chat_id": msg["chat_id"], "open_id": msg["open_id"]},
            stage=self.stage,
        )
        if created:
            self.reply.send_text(
                f"已建任务 #{task['id']}：{task['title']}",
                to=target,
                to_type=target_type,
            )

    def _on_card_action(self, data):
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        action = parse_card_action(data)
        if action is None:
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "无法识别该操作"}})
        if not self._allowed(action["open_id"]):
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "无权审批"}})

        # Same entry point as POST /a/{token}: one state machine, one idempotency
        # guard, one event trail — the card is just another front door.
        approval, task, changed = self.engine.decide(
            action["token"], index=action["index"], by=f"feishu:{action['open_id']}"
        )
        if approval is None:
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "审批不存在"}})
        if not changed:
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "这条已处理过"}})
        label = "已通过" if action["index"] == 0 else "已打回"
        return P2CardActionTriggerResponse(
            {"toast": {"type": "success", "content": f"{label}（任务 #{task['id']}）"}}
        )