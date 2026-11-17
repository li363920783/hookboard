"""M1 unit tests: run with ``python3 -m unittest discover -s tests -v``."""

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hub.adapters.generic_source import register as register_generic
from hub.config import Config
from hub.engine import Engine
from hub.store import Store

STAGES = ["intake", "plan", "dispatch", "work", "review", "finish"]


class StubAgent:
    name = "stub"

    def start(self, task, prompt):
        return {"ok": True, "session": None, "link": None, "error": None, "output": "stub ran"}

    def poll(self, task):
        return {"ok": True, "status": "done", "text": "", "error": None}

    def send(self, task, message):
        raise NotImplementedError


class StubNotifier:
    name = "stub"

    def __init__(self):
        self.calls = []
        self.context = {}

    def notify(self, title, body_md, links):
        self.calls.append(("notify", title))
        return {"ok": True, "detail": "stub"}

    def ask(self, title, body_md, url, options):
        self.calls.append(("ask", url))
        return {"ok": True, "detail": "stub"}


class HubTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.db")
        self.cfg = Config(stages=list(STAGES), approve_at=["plan", "review"], poll_interval=0.01)
        self.notifier = StubNotifier()
        self.engine = Engine(
            self.store, self.cfg, agent=StubAgent(), notifiers={"stub": self.notifier}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def new_task(self, external_id="ext-1"):
        task, created = self.store.create_task(
            source="test",
            kind="demo",
            external_id=external_id,
            title="demo task",
            stage=STAGES[0],
        )
        self.assertTrue(created)
        return task

    def wait_for_approval(self, task_id, stage="plan", limit=50):
        for _ in range(limit):
            approval = self.store.get_approval(task_id, stage)
            if approval and approval["status"] == "open":
                return approval
            if not self.engine.step():
                time.sleep(0.01)
        self.fail(f"no open approval for stage {stage}")

    def run_engine_until(self, predicate, limit=50):
        for _ in range(limit):
            task = predicate()
            if task:
                return task
            if not self.engine.step():
                time.sleep(0.01)
        self.fail("engine did not reach the expected state")

    def drain(self, limit=50):
        for _ in range(limit):
            if not self.engine.step():
                return
        self.fail("engine did not settle")


class AtomicClaimTest(HubTestCase):
    def test_only_one_worker_wins_the_claim(self):
        task = self.new_task()
        results = []
        barrier = threading.Barrier(8)

        def claim():
            barrier.wait()
            results.append(self.store.claim_task(task["id"]) is not None)

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(results), 1, "exactly one claim must win")
        stored = self.store.get_task(task["id"])
        self.assertEqual(stored["status"], "running")
        self.assertEqual(stored["attempts"], 1)


class DuplicateDeliveryTest(HubTestCase):
    def test_same_hook_twice_keeps_one_task(self):
        source = register_generic(
            "generic", {"map": {"kind": "kind", "external_id": "id", "title": "title"}}
        )
        body = {"id": "dup-1", "kind": "demo", "title": "dup"}
        first = source.parse(body, {})
        second = source.parse(dict(body), {})

        self.assertEqual(first["external_id"], second["external_id"])
        task1, created1 = self.store.create_task(
            source=first["source"], kind=first["kind"], external_id=first["external_id"],
            title=first["title"], stage=STAGES[0],
        )
        task2, created2 = self.store.create_task(
            source=second["source"], kind=second["kind"], external_id=second["external_id"],
            title=second["title"], stage=STAGES[0],
        )

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(task1["id"], task2["id"])
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_body_without_id_still_dedupes_on_hash(self):
        source = register_generic("generic", {"map": {"kind": "kind", "title": "title"}})
        first = source.parse({"kind": "demo", "title": "no id here"}, {})
        second = source.parse({"title": "no id here", "kind": "demo"}, {})
        self.assertEqual(first["external_id"], second["external_id"])


class ApprovalIdempotencyTest(HubTestCase):
    def test_second_decision_returns_stored_result(self):
        task = self.new_task()
        approval = self.wait_for_approval(task["id"])

        first, first_task, changed1 = self.engine.decide(approval["token"], index=0)
        second, second_task, changed2 = self.engine.decide(approval["token"], index=0)

        self.assertTrue(changed1)
        self.assertFalse(changed2)
        self.assertEqual(first["status"], "answered")
        self.assertEqual(second["status"], "answered")
        self.assertEqual(second["answer_text"], first["answer_text"])
        self.assertEqual(first_task["stage"], second_task["stage"])
        approved_events = [
            event
            for event in self.store.events_for(task["id"])
            if event["kind"] == "approved"
        ]
        self.assertEqual(len(approved_events), 1)


class StateMachineTest(HubTestCase):
    def test_task_walks_every_stage_with_two_gates(self):
        task = self.new_task()
        plan = self.wait_for_approval(task["id"], "plan")
        self.assertEqual(self.store.get_task(task["id"])["status"], "waiting")
        self.assertEqual(plan["stage"], "plan")

        self.engine.decide(plan["token"], index=0)

        def at_review():
            current = self.store.get_task(task["id"])
            if current["status"] == "waiting" and current["stage"] == "review":
                return current
            return None

        review_task = self.run_engine_until(at_review)
        self.assertEqual(review_task["status"], "waiting")
        review = self.store.get_approval(task["id"], "review")
        self.assertIsNotNone(review)
        self.engine.decide(review["token"], index=0)
        self.drain()

        final = self.store.get_task(task["id"])
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["stage"], "finish")

        detail = self.store.task_detail(task["id"])
        stage_events = {
            event["detail"] for event in detail["events"] if event["kind"] == "stage"
        }
        self.assertEqual(stage_events, set(STAGES))
        work_runs = [run for run in detail["runs"] if run["stage"] == "work"]
        self.assertEqual(len(work_runs), 1)
        self.assertTrue(work_runs[0]["ok"])


class ApprovalAdvancesStageTest(HubTestCase):
    def test_approval_moves_task_exactly_one_stage(self):
        task = self.new_task()
        approval = self.wait_for_approval(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["stage"], "plan")

        _, advanced, changed = self.engine.decide(approval["token"], index=0)

        self.assertTrue(changed)
        self.assertEqual(advanced["status"], "pending")
        self.assertEqual(advanced["stage"], "dispatch")
        self.engine.decide(approval["token"], index=0)
        self.assertEqual(self.store.get_task(task["id"])["stage"], "dispatch")

    def test_approval_clears_the_waiting_note(self):
        # "等待审批：plan" must not ride along into the next agent prompt.
        task = self.new_task()
        approval = self.wait_for_approval(task["id"])
        self.assertTrue(self.store.get_task(task["id"])["note"])
        _, advanced, _ = self.engine.decide(approval["token"], index=0)
        self.assertFalse(advanced["note"])


class RejectionTest(HubTestCase):
    def test_reject_fails_the_task(self):
        task = self.new_task()
        approval = self.wait_for_approval(task["id"])

        answered, decided, changed = self.engine.decide(approval["token"], index=1)

        self.assertTrue(changed)
        self.assertEqual(decided["status"], "failed")
        self.assertEqual(answered["answer_text"], "打回")
        kinds = [event["kind"] for event in self.store.events_for(task["id"])]
        self.assertIn("rejected", kinds)


if __name__ == "__main__":
    unittest.main()