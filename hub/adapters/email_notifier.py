"""Email notifier over SMTP (docs/CHANNELS.md §3) — stdlib only.

Sends multipart/alternative: an HTML part with a button (``<a>``) plus a
plain-text fallback that also contains the approval link.  Supports SMTP_SSL
(465) and STARTTLS (587).  In outbox / dry-run mode it writes the message parts
to data/outbox/ instead of opening a connection.

Config::

    "notifiers": {
      "email": {"type": "email", "host": "smtp.example.com", "port": 465,
                "tls": "ssl", "user": "bot@example.com", "password": "",
                "from": "bot@example.com", "to": ["you@example.com"], "dry_run": true}
    }

The password is read only from config or ``HUB_EMAIL_PASSWORD``; no real
credential ever lives in the repo.
"""

from __future__ import annotations

import html
import os
import smtplib
from email.message import EmailMessage

from .base import outbox_enabled, write_outbox


class EmailNotifier:
    name = "email"

    def __init__(self, entry: dict | None = None, outbox_dir: str = "data/outbox"):
        entry = entry or {}
        self.host = entry.get("host") or ""
        self.port = int(entry.get("port", 465))
        self.tls = (entry.get("tls") or "ssl").lower()  # ssl | starttls | none
        self.user = entry.get("user") or ""
        self.password = entry.get("password") or os.environ.get("HUB_EMAIL_PASSWORD", "")
        self.sender = entry.get("from") or self.user
        self.to = list(entry.get("to") or [])
        self.timeout = float(entry.get("timeout", 10))
        self.dry_run = outbox_enabled(entry)
        self.outbox_dir = outbox_dir
        self.context: dict = {}

    @staticmethod
    def _parts(title: str, body_md: str, links: list[tuple[str, str]]) -> tuple[str, str]:
        text_lines = [title, "", body_md, ""]
        for label, url in links:
            text_lines.append(f"{label}: {url}")
        text = "\n".join(text_lines)

        buttons = "".join(
            f'<a href="{html.escape(url, quote=True)}" '
            f'style="display:inline-block;padding:10px 18px;margin:6px 8px 0 0;'
            f'background:#1d4ed8;color:#fff;border-radius:8px;text-decoration:none;">'
            f"{html.escape(label)}</a>"
            for label, url in links
        )
        html_body = (
            '<div style="font-family:sans-serif;max-width:560px;">'
            f"<h2>{html.escape(title)}</h2>"
            f'<p style="white-space:pre-wrap;">{html.escape(body_md)}</p>'
            f"<div>{buttons}</div>"
            "</div>"
        )
        return text, html_body

    def _payload(self, title: str, body_md: str, links: list[tuple[str, str]]) -> dict:
        text, html_body = self._parts(title, body_md, links)
        return {
            "channel": "email",
            "multipart": "alternative",
            "from": self.sender,
            "to": self.to,
            "subject": title,
            "text": text,
            "html": html_body,
            "task_id": self.context.get("task_id"),
            "stage": self.context.get("stage"),
        }

    def notify(self, title: str, body_md: str, links: list[tuple[str, str]]) -> dict:
        payload = self._payload(title, body_md, links)
        if self.dry_run:
            path = write_outbox(self.outbox_dir, "email", payload)
            return {"ok": True, "detail": f"outbox:{path}"}
        return self._send(payload)

    def ask(self, title: str, body_md: str, url: str, options: list[str]) -> dict:
        body = body_md
        if options:
            body = f"{body_md}\n选项: {' / '.join(options)}（点链接处理）"
        return self.notify(title, body, [("去处理", url)])

    def _send(self, payload: dict) -> dict:
        if not self.host or not self.to:
            return {"ok": False, "detail": "email host/to not configured"}
        try:
            message = EmailMessage()
            message["Subject"] = payload["subject"]
            message["From"] = payload["from"]
            message["To"] = ", ".join(payload["to"])
            message.set_content(payload["text"])
            message.add_alternative(payload["html"], subtype="html")

            if self.tls == "ssl":
                smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout)
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
                if self.tls == "starttls":
                    smtp.starttls()
            try:
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(message)
            finally:
                smtp.quit()
            return {"ok": True, "detail": "sent"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}


def register(entry: dict | None = None, outbox_dir: str = "data/outbox") -> EmailNotifier:
    return EmailNotifier(entry, outbox_dir)