"""Configuration loading for Hookboard.

JSON only: JSON ships with Python's standard library, YAML does not.  The plan
allows a ``config.json`` instead of ``config.yaml`` (see README) so the runtime
stays dependency-free.

Precedence: built-in defaults < config file < ``HUB_*`` environment variables.
Relative database paths in a config file are resolved against that file's
directory, so ``./data/hub.db`` lands next to the config, not the CWD.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    bind: str = "127.0.0.1"
    port: int = 8899
    api_token: str = "change-me"
    public_base: str = "http://127.0.0.1:8899"
    workers: int = 2
    poll_interval: float = 1.0
    db_path: str = "./data/hub.db"
    stages: list[str] = field(
        default_factory=lambda: ["intake", "plan", "dispatch", "work", "review", "finish"]
    )
    approve_at: list[str] = field(default_factory=lambda: ["plan", "review"])
    sources: dict[str, dict] = field(default_factory=dict)
    agents: dict[str, dict] = field(default_factory=dict)
    notifiers: dict[str, dict] = field(default_factory=dict)
    projects: dict[str, dict] = field(default_factory=dict)
    routing: dict = field(default_factory=dict)


def _env(name: str, default, cast=str):
    value = os.environ.get(name)
    return cast(value) if value not in (None, "") else default


def load(path: str | os.PathLike | None = None) -> Config:
    """Load config from *path*, ``$HUB_CONFIG``, or ``./config.json``."""
    cfg = Config()
    explicit = path or os.environ.get("HUB_CONFIG")
    file_path = Path(explicit) if explicit else Path("config.json")
    public_base_set = False

    if file_path.is_file():
        raw = json.loads(file_path.read_text(encoding="utf-8"))
        base_dir = file_path.resolve().parent
        server = raw.get("server") or {}
        engine = raw.get("engine") or {}
        database = raw.get("database") or {}
        pipeline = raw.get("pipeline") or {}

        cfg.bind = str(server.get("bind", cfg.bind))
        cfg.port = int(server.get("port", cfg.port))
        cfg.api_token = str(server.get("api_token", cfg.api_token))
        if server.get("public_base"):
            cfg.public_base = str(server["public_base"])
            public_base_set = True
        cfg.workers = int(engine.get("workers", cfg.workers))
        cfg.poll_interval = float(engine.get("poll_interval", cfg.poll_interval))

        db = str(database.get("path", cfg.db_path))
        cfg.db_path = str((base_dir / db).resolve()) if not os.path.isabs(db) else db

        cfg.stages = list(pipeline.get("stages") or cfg.stages)
        cfg.approve_at = list(pipeline.get("approve_at") or [])
        cfg.sources = dict(raw.get("sources") or {})
        cfg.agents = dict(raw.get("agents") or {})
        cfg.notifiers = dict(raw.get("notifiers") or {})
        cfg.projects = dict(raw.get("projects") or {})
        cfg.routing = dict(raw.get("routing") or {})
    elif explicit:
        raise FileNotFoundError(f"config file not found: {file_path}")

    cfg.bind = _env("HUB_BIND", cfg.bind)
    cfg.port = _env("HUB_PORT", cfg.port, int)
    cfg.api_token = _env("HUB_API_TOKEN", cfg.api_token)
    if os.environ.get("HUB_PUBLIC_BASE"):
        cfg.public_base = os.environ["HUB_PUBLIC_BASE"]
        public_base_set = True
    cfg.workers = _env("HUB_WORKERS", cfg.workers, int)
    cfg.poll_interval = _env("HUB_POLL_INTERVAL", cfg.poll_interval, float)
    cfg.db_path = _env("HUB_DB", cfg.db_path)
    if not public_base_set:
        cfg.public_base = f"http://127.0.0.1:{cfg.port}"

    if not cfg.stages:
        raise ValueError("pipeline.stages must not be empty")
    unknown = sorted(set(cfg.approve_at) - set(cfg.stages))
    if unknown:
        raise ValueError(f"pipeline.approve_at names unknown stages: {unknown}")
    return cfg