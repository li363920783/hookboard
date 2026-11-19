"""Monitoring: stale `running` recovery and async polling.

The engine must not lose a task when the service dies mid-run, and must not
hold a worker for the whole agent run when the agent supports polling.
"""

import sqlite3
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hub.config import Config
from hub.engine import Engine
from hub.store import Store

STAGES = ["intake", "plan", "dispatch", "work", "review", "finish"]


class AsyncAgent:
    """start() queues the run; every poll() reports the next scripted state."""

    name = "async"
    poll_interval = 0.01
    poll_max_seconds = 0

    def __init__(self, states=("running", "done"), send_ok=True):
        self.states = list(states)
        self.prompts = []
        self.sent = []
        self.send_ok = send_ok

    def start(self, task, prompt):
        self.prompts.append(prompt)
        return {
            "ok": True,
            "status": "poll",
            "session": "t-1",
            "output": "queued",
            "poll_interval": self.poll_interval,
        }

    def poll(self, task):
        state = self.states.pop(0) if self.states else "done"
        if state == "failed":
            return {"ok": False, "status": "failed", "text": "", "error": "崩了"}
        return {"ok": True, "status": state, "text": "交付内容", "error": None}

    def send(self, task, message):
        self.sent.append((task["id"], message))
        return {"ok": self.send_ok, "detail": "sent" if self.send_ok else "send failed"}


class MonitorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "hub.db"
        self.store = Store(self.db_path)
        self.cfg = Config(stages=list(STAGES), approve_at=[], poll_interval=0.01)
        self.agent = AsyncAgent()
        self.engine = Engine(self.store, self.cfg, agent=self.agent, notifiers={})

    def tearDown(self):
        self.tmp.cleanup()

    def new_task(self, stage="work", project=""):
        task, _ = self.store.create_task(
            source="tapd", kind="bug", external_id="1", title="异步任务", stage=stage
        )
        return self.store.get_task(task["id"])

    def claim(self, task):
        claimed = self.store.claim_next(now=time.time() + 10_000)
        self.assertIsNotNone(claimed)
        return claimed

    def age(self, task_id, seconds):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET updated_at=? WHERE id=?", (time.time() - seconds, task_id)
            )

    # --------------------------------------------------------------- stale

    def test_stale_running_task_goes_back_to_the_queue(self):
        task = self.new_task()
        claimed = self.claim(task)
        self.age(claimed["id"], 100_000)
        ids = self.store.requeue_stale(600)
        self.assertEqual(ids, [claimed["id"]])
        recovered = self.store.get_task(claimed["id"])
        self.assertEqual(recovered["status"], "pending")
        self.assertEqual(recovered["polling"], 0)
        self.assertIn("重新入队", recovered["note"])

    def test_a_stale_task_with_a_session_resumes_instead_of_rerunning(self):
        task = self.new_task()
        self.engine.process(self.claim(task))  # queues t-1, polling=1
        self.claim(task)  # a worker picks it up, then the process dies
        self.age(task["id"], 100_000)
        self.store.requeue_stale(600)
        resumed = self.store.get_task(task["id"])
        self.assertEqual(resumed["status"], "pending")
        self.assertEqual(resumed["polling"], 1)
        self.assertIn("继续监控", resumed["note"])
        self.engine.process(self.claim(task))
        # It polls the existing session and never starts a second run.
        self.assertEqual(len(self.agent.prompts), 1)

    def test_a_live_running_task_is_left_alone(self):
        task = self.new_task()
        claimed = self.claim(task)
        self.assertEqual(self.store.requeue_stale(600), [])
        self.assertEqual(self.store.get_task(claimed["id"])["status"], "running")

    def test_stale_threshold_exceeds_the_longest_agent_timeout(self):
        self.cfg.agents = {"t3": {"type": "command", "command": "true", "timeout": 1800}}
        self.assertEqual(self.engine._stale_after(), 3600)
        self.cfg.stale_after = 120
        self.assertEqual(self.engine._stale_after(), 120)

    # --------------------------------------------------------------- polls

    def test_async_run_polls_until_done_then_advances(self):
        task = self.new_task()
        self.engine.process(self.claim(task))

        queued = self.store.get_task(task["id"])
        self.assertEqual(queued["status"], "pending")
        self.assertEqual(queued["stage"], "work")
        self.assertEqual(queued["polling"], 1)
        self.assertEqual(queued["session"], "t-1")
        self.assertGreater(queued["poll_started_at"] or 0, 0)

        # First poll: still running -> back into the queue, still in work.
        self.engine.process(self.claim(task))
        waiting = self.store.get_task(task["id"])
        self.assertEqual(waiting["status"], "pending")
        self.assertEqual(waiting["stage"], "work")
        self.assertEqual(waiting["polling"], 1)

        # Second poll: done -> review gate / next stage, polling cleared.
        self.engine.process(self.claim(task))
        finished = self.store.get_task(task["id"])
        self.assertEqual(finished["stage"], "review")
        self.assertEqual(finished["polling"], 0)
        self.assertEqual(finished["status"], "pending")
        runs = self.store.task_detail(task["id"])["runs"]
        self.assertTrue(any("交付内容" in (run.get("output") or "") for run in runs))
        # Two poll ticks must not each log a stage entry: that is what buried
        # the real history under 100+ identical "stage work" rows.
        stages = [
            event["detail"]
            for event in self.store.task_detail(task["id"])["events"]
            if event["kind"] == "stage"
        ]
        self.assertEqual(stages, ["work"])

    def test_async_failure_marks_the_task_failed(self):
        self.agent.states = ["running", "failed"]
        task = self.new_task()
        self.engine.process(self.claim(task))
        self.engine.process(self.claim(task))
        self.engine.process(self.claim(task))
        failed = self.store.get_task(task["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["polling"], 0)
        self.assertIn("崩了", failed["note"])

    def test_retry_re_attaches_to_the_failed_session(self):
        # The thread usually keeps working after the run hookboard saw fail;
        # retry must poll it again, not dispatch a second thread on the ticket.
        self.agent.states = ["failed"]
        task = self.new_task()
        self.engine.process(self.claim(task))  # dispatch
        self.engine.process(self.claim(task))  # poll -> failed
        self.assertEqual(self.store.get_task(task["id"])["status"], "failed")
        self.assertEqual(self.store.get_task(task["id"])["session"], "t-1")

        self.agent.states = ["done"]
        self.assertEqual(self.store.retry_task(task["id"])["polling"], 1)
        self.engine.process(self.claim(task))

        after = self.store.get_task(task["id"])
        self.assertEqual(after["stage"], "review")
        self.assertEqual(after["status"], "pending")
        self.assertEqual(len(self.agent.prompts), 1)  # no second dispatch

    # ------------------------------------------------- same ticket, same session

    def ticket_task(self, kind="bug_comment::add", external="c1", status="done", session="t-old"):
        task, _ = self.store.create_task(
            source="tapd",
            kind=kind,
            external_id=external,
            title="老任务",
            stage="work",
            payload={"event": kind, "entity_id": "BUG-1"},
        )
        fields = {"status": status}
        if session:
            fields["session"] = session
        self.store.update_task(task["id"], where_status="pending", **fields)
        return self.store.get_task(task["id"])

    def followup(self):
        task, _ = self.store.create_task(
            source="tapd",
            kind="bug_comment::add",
            external_id="c2",
            title="新评论：还是不行",
            stage="intake",
            payload={"event": "bug_comment::add", "entity_id": "BUG-1"},
        )
        return task

    def test_a_done_task_is_reopened_instead_of_a_new_session(self):
        old = self.ticket_task(status="done")
        new = self.followup()
        self.engine.process(self.claim(new))

        merged = self.store.get_task(new["id"])
        self.assertEqual(merged["status"], "done")
        self.assertIn(f"并入任务 #{old['id']}", merged["note"])
        # The old task now polls the session the message woke up.
        reopened = self.store.get_task(old["id"])
        self.assertEqual(reopened["status"], "pending")
        self.assertEqual(reopened["stage"], "work")
        self.assertEqual(reopened["polling"], 1)
        self.assertEqual(reopened["session"], "t-old")
        self.assertEqual(self.agent.sent[0][0], old["id"])
        self.assertIn("新评论：还是不行", self.agent.sent[0][1])
        # No second thread was dispatched.
        self.assertEqual(self.agent.prompts, [])

    def test_a_running_task_only_gets_the_message(self):
        old = self.ticket_task(status="running")
        new = self.followup()
        self.engine.process(self.claim(new))
        self.assertEqual(self.store.get_task(old["id"])["status"], "running")
        self.assertEqual(len(self.agent.sent), 1)
        self.assertEqual(self.store.get_task(new["id"])["status"], "done")

    def test_a_task_waiting_at_the_gate_absorbs_the_follow_up(self):
        # Not dispatched yet, so there is no thread: fold it into the context
        # (the note is cleared when the gate is answered) so one dispatch
        # covers both events.
        old = self.ticket_task(status="waiting", session=None)
        new = self.followup()
        self.engine.process(self.claim(new))
        merged = self.store.get_task(old["id"])
        self.assertEqual(merged["status"], "waiting")
        self.assertIn("新评论：还是不行", merged["context"])
        self.assertEqual(self.store.get_task(new["id"])["status"], "done")
        self.assertEqual(self.agent.prompts, [])

    def test_without_a_session_the_event_runs_normally(self):
        self.ticket_task(status="done", session=None)
        new = self.followup()
        self.engine.process(self.claim(new))
        self.assertEqual(self.agent.sent, [])
        self.assertEqual(self.store.get_task(new["id"])["stage"], "plan")

    def test_a_cancelled_task_is_not_resumed(self):
        self.ticket_task(status="cancelled")
        new = self.followup()
        self.engine.process(self.claim(new))
        self.assertEqual(self.agent.sent, [])
        self.assertEqual(self.store.get_task(new["id"])["stage"], "plan")

    def test_a_failed_send_falls_back_to_a_normal_run(self):
        self.agent.send_ok = False
        old = self.ticket_task(status="done")
        new = self.followup()
        self.engine.process(self.claim(new))
        self.assertEqual(self.store.get_task(old["id"])["status"], "done")
        self.assertEqual(self.store.get_task(new["id"])["stage"], "plan")

    def test_a_polled_stage_logs_one_stage_event_per_pass(self):
        # Hundreds of identical "stage work" rows buried the real work, but a
        # new pass must log again or stage_entered_at goes stale.
        task = self.new_task()
        first = self.claim(task)
        self.engine.process(first)
        self.engine.process(self.claim(first))
        rows = [
            event
            for event in self.store.events_for(task["id"])
            if event["kind"] == "stage" and event["detail"] == "work"
        ]
        self.assertEqual(len(rows), 1)

    def test_polling_times_out_after_max_seconds(self):
        self.agent.poll_max_seconds = 1
        task = self.new_task()
        self.engine.process(self.claim(task))
        self.store.update_task(task["id"], where_status="pending", poll_started_at=time.time() - 60)
        self.engine.process(self.claim(task))
        timed_out = self.store.get_task(task["id"])
        self.assertEqual(timed_out["status"], "failed")
        self.assertIn("超时", timed_out["note"])


if __name__ == "__main__":
    unittest.main()