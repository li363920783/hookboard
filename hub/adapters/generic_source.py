"""Generic JSON source: field names come from config, the body passes through.

Config::

    "sources": {
      "acme": {"type": "generic", "map": {"kind": "type", "external_id": "data.id", "title": "data.title"}}
    }

``map`` keys are ``kind`` / ``external_id`` / ``title`` / ``priority`` /
``source``; values are dotted paths into the hook body.  Without an
``external_id`` mapping, a stable hash of the body is used, so identical
deliveries still deduplicate.
"""

from __future__ import annotations

import hashlib
import json


class GenericSource:
    name = "generic"

    def __init__(self, name: str = "generic", options: dict | None = None):
        self.name = name
        self.map = (options or {}).get("map") or {}

    @staticmethod
    def _dig(body: dict, path: str):
        current = body
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    def parse(self, body: dict, headers: dict) -> dict | None:
        if not isinstance(body, dict):
            return None

        def pick(field: str):
            path = self.map.get(field)
            return self._dig(body, path) if path else None

        external_id = pick("external_id")
        if external_id is None:
            canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            external_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        kind = pick("kind") or "hook"
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