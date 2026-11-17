"""Generic routing: project selection and per-project engine behaviour."""

import os
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hub.config import Config
from hub.engine import Engine
from hub.routing import load_callable, resolve
from hub.server import HubServer
from hub.store import Store


class CountingNotifier:
    def __init__(self, name):
        self.name = name
        self.calls = 0
        self.context = {}

    def notify(self, title, body_md, links):
        self.calls += 1
        return {"ok": True, "detail": "ok"}

    def ask(self, title, body_md, url, options):
        self.calls += 1
        return {"ok": True, "detail": "ok"}


class ResolveTest(unittest.TestCase):
    def test_reads_the_configured_field(self):
        cfg = Config(routing={"field": "repo"})
        self.assertEqual(resolve(cfg, {"repo": "shop"}), "shop")

    def test_default_field_is_project(self):
        self.assertEqual(resolve(Config(), {"project": "shop"}), "shop")

    def test_no_project_is_empty_string(self):
        self.assertEqual(resolve(Config(), {}), "")

    def test_python_route_function_wins(self):
        with TemporaryDirectory() as tmp:
            Path(tmp, "myrules.py").write_text(
                "def route(body):\n    return 'big' if body.get('n', 0) > 10 else 'small'\n",
                encoding="utf-8",
            )
            sys.path.insert(0, tmp)
            try:
                cfg = Config(routing={"python": "myrules:route"})
                self.assertEqual(resolve(cfg, {"n": 50}), "big")
                self.assertEqual(resolve(cfg, {"n": 1}), "small")
            finally:
                sys.path.remove(tmp)
                sys.modules.pop("myrules", None)

    def test_bad_python_target_raises(self):
        with self.assertRaises(ValueError):
            load_callable("not-a-target")


class UnknownProjectTest(unittest.TestCase):
    def test_hook_with_unknown_project_is_rejected(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "hub.db")
            cfg = Config(projects={"shop": {}}, routing={"field": "project"})
            hub = HubServer(store, Engine(store, cfg), cfg)
            self.assertEqual(hub.resolve_project({"project": "shop"}), "shop")
            with self.assertRaises(ValueError):
                hub.resolve_project({"project": "nope"})

    def test_project_is_ignored_when_none_are_configured(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "hub.db")
            cfg = Config(projects={})
            hub = HubServer(store, Engine(store, cfg), cfg)
            self.assertEqual(hub.resolve_project({"project": "anything"}), "")


class ProjectEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _run_until(self, engine, task_id, statuses, limit=30):
        for _ in range(limit):
            if self.store.get_task(task_id)["status"] in statuses:
                break
            engine.step()

    def test_project_can_disable_gates(self):
        cfg = Config(
            stages=["intake", "work", "done"],
            approve_at=["work"],
            projects={"p": {"approve_at": []}},
        )
        engine = Engine(self.store, cfg, notifiers={})
        task, _ = self.store.create_task(
            source="t", kind="k", external_id="e", title="x", project="p", stage="intake"
        )
        self._run_until(engine, task["id"], {"done"})
        self.assertEqual(self.store.get_task(task["id"])["status"], "done")

    def test_project_only_notifies_its_channels(self):
        one, two = CountingNotifier("one"), CountingNotifier("two")
        cfg = Config(
            stages=["intake", "work", "done"],
            approve_at=["work"],
            projects={"p": {"notifiers": ["one"]}},
        )
        engine = Engine(self.store, cfg, notifiers={"one": one, "two": two})
        task, _ = self.store.create_task(
            source="t", kind="k", external_id="e", title="x", project="p", stage="intake"
        )
        self._run_until(engine, task["id"], {"waiting"})
        self.assertEqual(one.calls, 1)
        self.assertEqual(two.calls, 0)

    def test_project_workspace_becomes_the_agent_cwd(self):
        workspace = str(Path(self.tmp.name, "repo"))
        os.makedirs(workspace)
        cfg = Config(
            stages=["intake", "work", "done"],
            approve_at=[],
            agents={"walker": {"type": "shell", "command": "pwd"}},
            projects={"p": {"agent": "walker", "workspace": workspace}},
        )
        engine = Engine(self.store, cfg, notifiers={})
        task, _ = self.store.create_task(
            source="t", kind="k", external_id="e", title="x", project="p", stage="intake"
        )
        self._run_until(engine, task["id"], {"done"})
        runs = self.store.runs_for(task["id"])
        self.assertTrue(runs, "agent never ran")
        self.assertEqual(runs[-1]["output"].strip(), str(Path(workspace).resolve()))


class MigrationTest(unittest.TestCase):
    def test_old_database_gains_the_project_column(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.execute(
                """CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
                    kind TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                    lane TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
                    stage TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, session TEXT,
                    note TEXT, payload_json TEXT NOT NULL DEFAULT '{}',
                    attempts INTEGER NOT NULL DEFAULT 0, next_run_at REAL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE (source, kind, external_id))"""
            )
            conn.commit()
            conn.close()

            Store(path)  # runs the migration
            conn = sqlite3.connect(path)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            conn.close()
            self.assertIn("project", columns)


if __name__ == "__main__":
    unittest.main()