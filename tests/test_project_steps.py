"""Task digest (card + prompt body) and per-project stage commands."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hub.config import Config
from hub.engine import Engine
from hub.store import Store

STAGES = ["intake", "plan", "dispatch", "work", "review", "finish"]


class RecordingAgent:
    name = "rec"

    def __init__(self):
        self.prompts = []

    def start(self, task, prompt):
        self.prompts.append(prompt)
        return {"ok": True, "session": None, "link": None, "error": None, "output": "ok"}

    def poll(self, task):
        return {"ok": True, "status": "done", "text": "", "error": None}

    def send(self, task, message):
        raise NotImplementedError


class DigestTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.db")
        self.cfg = Config(stages=list(STAGES), approve_at=["plan", "review"], poll_interval=0.01)
        self.cfg.projects = {
            "tapd": {
                "fields": {"缺陷标题": "bug.title", "严重程度": "bug.severity", "链接": "bug.url"},
                "steps": {
                    "finish": "echo \"finished $HUB_EXTERNAL_ID\"",
                },
            }
        }
        self.agent = RecordingAgent()
        self.engine = Engine(self.store, self.cfg, agent=self.agent, notifiers={})

    def tearDown(self):
        self.tmp.cleanup()

    def new_task(self, stage="work", project="tapd", payload=None):
        task, _ = self.store.create_task(
            source="tapd",
            kind="bug",
            external_id="1148599968",
            title="【测试】老四收单链路",
            stage=stage,
            project=project,
            payload=payload
            if payload is not None
            else {"bug": {"title": "收单失败", "severity": "严重", "url": "https://example.com/b/1"}},
        )
        self.store.update_task(task["id"], where_status="pending", status="running")
        return self.store.get_task(task["id"])

    def test_prompt_carries_configured_payload_fields(self):
        task = self.new_task()
        self.engine.process(task)
        prompt = self.agent.prompts[-1]
        self.assertIn("【测试】老四收单链路", prompt)
        self.assertIn("缺陷标题", prompt)
        self.assertIn("收单失败", prompt)
        self.assertIn("严重", prompt)
        self.assertIn("1148599968", prompt)

    def test_digest_falls_back_to_raw_payload_for_prompts_only(self):
        task = self.new_task(project="", payload={"weird": {"shape": [1, 2]}})
        prompt_digest = self.engine._task_digest(task, raw=True)
        self.assertIn("原始内容", prompt_digest)
        self.assertIn('"weird"', prompt_digest)
        # Cards stay short: no half-payload dump in the chat.
        card_digest = self.engine._task_digest(task)
        self.assertNotIn("原始内容", card_digest)
        self.assertIn("来源", card_digest)

    def test_digest_fields_strip_html(self):
        payload = {"bug": {"title": "", "severity": "严重", "url": "https://example.com/b/1",
                          "description": '<p><strong>复现步骤</strong></p><p>点开&nbsp;聊天页</p>'}}
        self.cfg.projects["tapd"]["fields"] = {"描述": "bug.description"}
        task = self.new_task(payload=payload)
        digest = self.engine._task_digest(task, raw=True)
        self.assertIn("复现步骤", digest)
        self.assertNotIn("<p>", digest)
        self.assertNotIn("&nbsp;", digest)

    def test_digest_fields_accept_a_path_list(self):
        self.cfg.projects["tapd"]["fields"] = {"缺陷标题": ["title", "old_title"]}
        task = self.new_task(payload={"old_title": "TAPD 只在更新事件里给 old_title"})
        digest = self.engine._task_digest(task)
        self.assertIn("TAPD 只在更新事件里给 old_title", digest)

    def test_approval_detail_contains_the_digest(self):
        task = self.new_task(stage="plan")
        self.engine.process(task)
        approval = self.store.get_approval(task["id"], "plan")
        self.assertIn("缺陷标题", approval["detail"])
        self.assertIn("收单失败", approval["detail"])

    def test_finish_step_runs_and_completes(self):
        task = self.new_task(stage="finish")
        self.engine.process(task)
        done = self.store.get_task(task["id"])
        self.assertEqual(done["status"], "done")
        runs = self.store.task_detail(task["id"])["runs"]
        self.assertTrue(any(run["agent"] == "step:finish" for run in runs))
        # A successful final step finishes the task; it is not a failure.
        self.assertIsNone(done["note"])

    def test_step_sees_the_ticket_id_in_env(self):
        self.cfg.projects["tapd"]["steps"]["finish"] = "echo \"id=$HUB_EXTERNAL_ID\""
        task = self.new_task(stage="finish")
        self.engine.process(task)
        # The run output carries the command's stdout.
        runs = self.store.task_detail(task["id"])["runs"]
        self.assertTrue(any("id=1148599968" in (run.get("output") or "") for run in runs))

    def test_failing_step_fails_the_task(self):
        self.cfg.projects["tapd"]["steps"]["finish"] = "echo boom >&2; exit 3"
        task = self.new_task(stage="finish")
        self.engine.process(task)
        failed = self.store.get_task(task["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("boom", failed["note"] or "")


if __name__ == "__main__":
    unittest.main()