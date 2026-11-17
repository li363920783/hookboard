"""Adapter contracts and shared notification helpers.

The Notifier interface is fixed by docs/CHANNELS.md: ``notify`` / ``ask`` both
return ``{ok, detail}``.  Adding a channel later must not change the core.
"""

from __future__ import annotations

import itertools
import json
import os
import time
from pathlib import Path
from typing import Protocol


class Source(Protocol):
    name: str

    def parse(self, body: dict, headers: dict) -> dict | None:
        """Turn an incoming hook into a task dict, or return None to ignore it."""


class Agent(Protocol):
    name: str

    def start(self, task: dict, prompt: str) -> dict:
        """Start work.  Returns {ok, session, link, error} (+ optional output)."""

    def poll(self, task: dict) -> dict:
        """Return {ok, status: running|done|failed, text, error}."""

    def send(self, task: dict, message: str) -> dict:
        """Continue the session with one more message (optional)."""


class Notifier(Protocol):
    name: str

    def notify(self, title: str, body_md: str, links: list[tuple[str, str]]) -> dict:
        """Plain notification.  links=[(label, url)].  Returns {ok, detail}."""

    def ask(self, title: str, body_md: str, url: str, options: list[str]) -> dict:
        """Approval notification.  *url* is the approval page; *options* are a
        human-readable hint only — the decision still happens on the web page.
        Returns {ok, detail}."""


# --------------------------------------------------------------------------
# Offline / outbox support (docs/CHANNELS.md hard constraint #2).
# --------------------------------------------------------------------------

_OUTBOX_SEQ = itertools.count()


def outbox_enabled(entry: dict | None) -> bool:
    """True when HUB_OUTBOX=1 or the channel config sets dry_run: true."""
    return os.environ.get("HUB_OUTBOX") == "1" or bool((entry or {}).get("dry_run"))


def write_outbox(outbox_dir: str | Path, channel: str, payload: dict) -> str:
    """Write the channel-format payload to data/outbox/<time>-<channel>.json.

    No network request is made.  Returns the file path.
    """
    directory = Path(outbox_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = directory / f"{stamp}-{channel}-{next(_OUTBOX_SEQ):04d}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)