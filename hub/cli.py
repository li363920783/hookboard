"""Command line: ``hookboard create / run / status`` (also ``python -m hub``)."""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from . import __version__, config, server
from .engine import Engine
from .store import Store


def _store(cfg) -> Store:
    return Store(cfg.db_path)


def cmd_run(args) -> int:
    cfg = config.load(args.config)
    store = _store(cfg)
    engine = Engine(store, cfg)
    httpd = server.make_server(store, engine, cfg)
    engine.start()
    print(
        f"Hookboard {__version__} listening on http://{cfg.bind}:{cfg.port} "
        f"(db={cfg.db_path}, workers={cfg.workers})",
        flush=True,
    )
    _start_feishu_inbound(store, engine, cfg)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…", flush=True)
    finally:
        engine.stop()
        httpd.server_close()
    return 0


def _start_feishu_inbound(store, engine, cfg) -> None:
    """Opt-in Feishu WebSocket gateway (needs ``pip install 'hookboard[feishu]'``)."""
    entry = (cfg.notifiers or {}).get("feishu") or {}
    if not entry.get("inbound"):
        return
    from pathlib import Path

    from .feishu_gateway import FeishuGateway

    gateway = FeishuGateway(
        store,
        engine,
        entry,
        stage=cfg.stages[0],
        outbox_dir=str(Path(store.path).parent / "outbox"),
    )
    if gateway.start():
        print("feishu inbound: websocket gateway started (long connection)", flush=True)


def cmd_create(args) -> int:
    cfg = config.load(args.config)
    store = _store(cfg)
    external_id = args.external_id or uuid.uuid4().hex
    payload = {}
    if args.payload:
        payload = json.loads(args.payload)
    task, created = store.create_task(
        source=args.source,
        kind=args.kind,
        external_id=external_id,
        title=args.title or external_id,
        priority=args.priority,
        payload=payload,
        project=args.project,
        stage=cfg.stages[0],
    )
    print(
        f"task {task['id']} {'created' if created else 'already exists'} "
        f"status={task['status']} stage={task['stage']}"
    )
    return 0


def cmd_status(args) -> int:
    cfg = config.load(args.config)
    store = _store(cfg)
    counts = store.status_counts()
    order = ["pending", "running", "waiting", "done", "failed", "cancelled"]
    summary = " ".join(f"{key}={counts.get(key, 0)}" for key in order)
    print(f"tasks: {summary}")
    print(f"open approvals: {len(store.open_approvals())}")
    print(f"db: {cfg.db_path}")
    print("recent:")
    for task in store.list_tasks(limit=10):
        print(
            f"  #{task['id']:<4} [{task['status']:<9}] {task['stage']:<8} "
            f"{task['title'][:48]}  ({task['source']}/{task['kind']}/{task['external_id']})"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="path to config.json (default: ./config.json or $HUB_CONFIG)",
    )
    parser = argparse.ArgumentParser(prog="hookboard", description="Hooks in, work out.")
    parser.add_argument(
        "--config", default=None, help="path to config.json (default: ./config.json or $HUB_CONFIG)"
    )
    parser.add_argument("--version", action="version", version=f"hookboard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common], help="start the HTTP server and workers")
    run.set_defaults(func=cmd_run)

    create = sub.add_parser("create", parents=[common], help="create a task directly")
    create.add_argument("--title", default="")
    create.add_argument("--source", default="cli")
    create.add_argument("--kind", default="task")
    create.add_argument("--external-id", default="")
    create.add_argument("--priority", type=int, default=0)
    create.add_argument("--payload", default="", help="JSON object stored with the task")
    create.add_argument("--project", default="", help="project name from config.projects")
    create.set_defaults(func=cmd_create)

    status = sub.add_parser(
        "status", parents=[common], help="print queue and approval counts"
    )
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())