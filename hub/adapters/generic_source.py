"""Generic JSON source: field names come from config, the body passes through.

Config::

    "sources": {
      "acme": {"type": "generic", "map": {"kind": "type", "external_id": "data.id", "title": "data.title"}}
    }

``map`` keys are ``kind`` / ``external_id`` / ``title`` / ``priority`` /
``source``; values are dotted paths into the hook body.  ``headers`` maps the
same keys to HTTP header names (tried first) — GitHub's ``X-GitHub-Event`` and
``X-GitHub-Delivery`` are the usual case.  Without an ``external_id``, a stable
hash of the body is used, so identical deliveries still deduplicate.

A source with its own ``auth`` replaces the global ``X-Hub-Token`` for that
URL, because external systems send their own signature, not ours::

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

``auth.type`` is ``token`` (shared secret in a header), ``hmac_sha256`` (hex
digest of the raw body, optional ``sha256=`` prefix), or ``query`` (secret in
the URL, ``/hooks/tapd?token=...``, for providers that send neither).  The
secret comes from ``secret_env`` or inline ``secret``.  With no secret available
the source fails closed — it rejects every request rather than falling back to
open.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os


def dig(body, path: str):
    """Follow a dotted path through nested dicts/lists.  ``None`` if absent."""
    current = body
    for part in str(path).split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def dig_any(body, paths):
    """First non-empty value found at any of the dotted paths.

    A config value may be one path or a list tried in order — TAPD sends
    ``old_title`` on updates and ``title`` on creates, for example.
    """
    if isinstance(paths, str):
        return dig(body, paths)
    for path in paths or []:
        value = dig(body, path)
        if value not in (None, ""):
            return value
    return None


def _header(headers: dict, name: str) -> str:
    """Case-insensitive header lookup, tolerant of lists and Message objects."""
    if not name:
        return ""
    wanted = name.lower()
    try:
        items = headers.items()
    except AttributeError:
        return ""
    for key, value in items:
        if str(key).lower() == wanted:
            if isinstance(value, (list, tuple)):
                value = value[0] if value else ""
            return str(value or "")
    return ""


class GenericSource:
    name = "generic"

    def __init__(self, name: str = "generic", options: dict | None = None):
        options = options or {}
        self.name = name
        self.map = options.get("map") or {}
        self.header_map = {k: v for k, v in (options.get("headers") or {}).items() if v}
        self.auth = dict(options.get("auth") or {})
        # Only these event values create tasks; anything else is acknowledged
        # and ignored. Without it, every TAPD status change (including our own
        # write-back) would re-open the same task.
        self.accept = {str(e) for e in (options.get("accept") or [])}

    # ------------------------------------------------------------ auth

    @property
    def protected(self) -> bool:
        return bool(self.auth)

    def _secret(self) -> str:
        env = self.auth.get("secret_env")
        if env:
            return os.environ.get(str(env)) or ""
        return str(self.auth.get("secret") or "")

    def verify(self, raw: bytes, headers: dict, query: dict | None = None) -> bool:
        if not self.auth:
            return True
        secret = self._secret()
        if not secret:  # configured but no secret available: fail closed
            return False
        kind = str(self.auth.get("type") or "token").lower()
        if kind == "query":
            # Providers that send neither a signature nor a header can still
            # carry a secret in the URL: /hooks/tapd?token=...
            param = self.auth.get("param") or "token"
            values = (query or {}).get(param)
            if isinstance(values, (list, tuple)):
                values = values[-1] if values else None
            return bool(values) and hmac.compare_digest(str(values), secret)
        supplied = _header(headers, self.auth.get("header") or "").strip()
        if not supplied:
            return False
        if kind == "token":
            return hmac.compare_digest(supplied, secret)
        if kind in ("hmac_sha256", "sha256"):
            if supplied.lower().startswith("sha256="):
                supplied = supplied[7:]
            expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            return hmac.compare_digest(supplied.lower(), expected)
        return False

    # ----------------------------------------------------------- parse

    def parse(self, body: dict, headers: dict) -> dict | None:
        if not isinstance(body, dict):
            return None

        def pick(field: str):
            header_name = self.header_map.get(field)
            if header_name:
                value = _header(headers, header_name)
                if value:
                    return value
            path = self.map.get(field)
            return dig_any(body, path) if path else None

        external_id = pick("external_id")
        if external_id is None:
            canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            external_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        kind = pick("kind") or "hook"
        if self.accept and str(kind) not in self.accept:
            return None
        title = pick("title") or str(external_id)
        try:
            priority = int(pick("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        return {
            "source": str(pick("source") or self.name),
            "kind": str(kind),
            "external_id": str(external_id),
            "title": str(title),
            "priority": priority,
            "payload": body,
        }


def register(name: str = "generic", options: dict | None = None) -> GenericSource:
    return GenericSource(name, options)