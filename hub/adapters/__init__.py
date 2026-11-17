"""Adapter loading.  One file per adapter, each exposing ``register()``."""

from __future__ import annotations

from .email_notifier import EmailNotifier
from .feishu_notifier import FeishuNotifier
from .shell_agent import ShellAgent
from .webhook_notifier import WebhookNotifier


def load_source(name: str, entry: dict | None):
    entry = entry or {}
    kind = entry.get("type") or name
    if kind == "generic":
        from .generic_source import register

        return register(name, entry)
    raise ValueError(f"unknown source type {kind!r} (configured as {name!r})")


def load_agent(entry: dict | None):
    entry = entry or {}
    kind = entry.get("type") or "shell"
    if kind == "shell":
        return ShellAgent(entry)
    raise ValueError(f"unknown agent type {kind!r}")


def load_notifier(name: str, entry: dict | None, outbox_dir: str = "data/outbox"):
    entry = entry or {}
    kind = entry.get("type") or name
    if kind == "webhook":
        return WebhookNotifier(entry, outbox_dir)
    if kind == "email":
        return EmailNotifier(entry, outbox_dir)
    if kind == "feishu":
        return FeishuNotifier(entry, outbox_dir)
    raise ValueError(f"unknown notifier type {kind!r} (configured as {name!r})")