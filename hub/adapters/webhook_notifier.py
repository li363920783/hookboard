"""Generic webhook notifier (docs/CHANNELS.md §4).

POSTs (or GETs) a JSON payload to any URL.  In outbox / dry-run mode it writes
the exact payload to data/outbox/ instead of touching the network.

Config::

    "notifiers": {
      "webhook": {"type": "webhook", "url": "https://example.com/hook",
                  "method": "POST", "headers": {"X-Token": "..."}, "dry_run": true}
    }

Credentials (header tokens) come only from config / environment, never the repo.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from .base import outbox_enabled, write_outbox


class WebhookNotifier:
    name = "webhook"

    def __init__(self, entry: dict | None = None, outbox_dir: str = "data/outbox"):
        entry = entry or {}
        self.url = (entry.get("url") or "").strip()
        self.method = (entry.get("method") or "POST").upper()
        self.headers = dict(entry.get("headers") or {})
        self.timeout = float(entry.get("timeout", 5))
        self.dry_run = outbox_enabled(entry)
        self.outbox_dir = outbox_dir
        self.context: dict = {}  # set by the engine: task_id / stage

    def _payload(self, title: str, body_md: str, links: list[tuple[str, str]]) -> dict:
        return {
            "title": title,
            "body": body_md,
            "links": [{"text": text, "url": url} for text, url in links],
            "task_id": self.context.get("task_id"),
            "stage": self.context.get("stage"),
        }

    def notify(self, title: str, body_md: str, links: list[tuple[str, str]]) -> dict:
        payload = self._payload(title, body_md, links)
        if self.dry_run:
            path = write_outbox(self.outbox_dir, "webhook", payload)
            return {"ok": True, "detail": f"outbox:{path}"}
        return self._send(payload)

    def ask(self, title: str, body_md: str, url: str, options: list[str]) -> dict:
        body = body_md
        if options:
            body = f"{body_md}\n选项: {' / '.join(options)}（点链接处理）"
        return self.notify(title, body, [("去处理", url)])

    def _send(self, payload: dict) -> dict:
        if not self.url:
            return {"ok": False, "detail": "no url configured"}
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json", **self.headers}
            if self.method == "GET":
                query = urllib.parse.urlencode(
                    {
                        "title": payload["title"],
                        "body": payload["body"],
                        "url": (payload["links"][0]["url"] if payload["links"] else ""),
                    }
                )
                request = urllib.request.Request(
                    f"{self.url}?{query}", headers=self.headers, method="GET"
                )
            else:
                request = urllib.request.Request(
                    self.url, data=data, headers=headers, method="POST"
                )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                ok = 200 <= response.status < 300
                return {"ok": ok, "detail": f"HTTP {response.status}"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}


def register(entry: dict | None = None, outbox_dir: str = "data/outbox") -> WebhookNotifier:
    return WebhookNotifier(entry, outbox_dir)