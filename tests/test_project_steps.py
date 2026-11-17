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
        self.tasks = []

    def start(self, task, prompt):
        self.prompts.append(prompt)
        self.tasks.append(task)
        return {"ok": True, "session": None, "link": None, "error": None, "output": "ok"}

    def poll(self, task):
        return {"ok": True, "status": "done", "text": "", "error": None}

    def send(self, task, message):
        self.messages = getattr(self, "messages", [])
        self.messages.append(message)
        return {"ok": True, "detail": "sent"}


class RecordingNotifier:
    name = "rec"

    def __init__(self):
        self.sent = []
        self.context = {}

    def notify(self, title, body_md, links):
        self.sent.append((title, body_md, links))
        return {"ok": True, "detail": "sent"}

    def ask(self, title, body_md, url, options):
        self.sent.append((title, body_md, [(options or [""])[0], url]))
        return {"ok": True, "detail": "sent"}


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

    def test_card_body_keeps_the_tapd_payload_off_the_card(self):
        # The project shows configured fields, so the raw payload (ids, proxy
        # hosts, the webhook secret) must not be dumped into the card.
        task = self.new_task()
        body = self.engine._card_body(task, "摘要")
        self.assertEqual(body, "摘要")

    def test_card_body_falls_back_to_readable_fields_without_config(self):
        self.cfg.projects["plain"] = {}
        task = self.new_task(project="plain", payload={
            "event": "bug::create",
            "secret": "fake-webhook-secret-for-tests",
            "old_title": "重复的旧标题",
            "title": "患者信息异常",
        })
        body = self.engine._card_body(task, "摘要")
        self.assertIn("title: 患者信息异常", body)
        self.assertNotIn("secret", body)
        self.assertNotIn("fake-webhook-secret", body)
        self.assertNotIn("重复的旧标题", body)
        self.assertNotIn('{"', body)

    def test_approval_detail_contains_the_digest(self):
        task = self.new_task(stage="plan")
        self.engine.process(task)
        approval = self.store.get_approval(task["id"], "plan")
        self.assertIn("缺陷标题", approval["detail"])
        self.assertIn("收单失败", approval["detail"])

    def test_routing_step_records_workspace_branch_and_reason(self):
        self.cfg.projects["tapd"]["steps"] = {
            "intake": {
                "command": (
                    "echo '{\"workspace\": \"/tmp/repo-a\", \"branch\": \"main\", "
                    "\"reason\": \"标题命中随访员小程序\"}'"
                ),
                "parse": {
                    "format": "json",
                    "workspace": "workspace",
                    "branch": "branch",
                    "note": "reason",
                },
            }
        }
        task = self.new_task(stage="intake")
        self.engine.process(task)
        routed = self.store.get_task(task["id"])
        self.assertEqual(routed["workspace"], "/tmp/repo-a")
        self.assertEqual(routed["branch"], "main")
        self.assertIn("随访员小程序", routed["note"])
        self.assertEqual(routed["stage"], "plan")

    def test_task_workspace_and_branch_override_the_project(self):
        self.cfg.projects["tapd"]["workspace"] = "/tmp/project-default"
        self.cfg.projects["tapd"]["branch"] = "master"
        task = self.new_task(stage="work")
        self.store.update_task(task["id"], where_status="running", workspace="/tmp/routed", branch="main")
        self.engine.process(self.store.get_task(task["id"]))
        payload = self.agent.tasks[-1]
        self.assertEqual(payload["workspace"], "/tmp/routed")
        self.assertEqual(payload["branch"], "main")

    def test_approval_detail_shows_the_dispatch_target(self):
        task = self.new_task(stage="plan")
        self.store.update_task(
            task["id"], where_status="running", workspace="/tmp/routed", branch="main"
        )
        task = self.store.get_task(task["id"])
        self.engine.process(task)
        approval = self.store.get_approval(task["id"], "plan")
        self.assertIn("派单目标：/tmp/routed @ main", approval["detail"])

    def test_message_reaches_the_agent_and_is_logged(self):
        task = self.new_task(stage="work")
        self.store.update_task(task["id"], where_status="running", session="t-9", status="waiting")
        task = self.store.get_task(task["id"])
        result = self.engine.send_message(task, "先别动发版，看下日志")
        self.assertTrue(result["ok"])
        self.assertEqual(self.agent.messages[-1], "先别动发版，看下日志")
        kinds = [event["kind"] for event in self.store.events_for(task["id"])]
        self.assertIn("message", kinds)

    def test_message_without_a_session_is_rejected(self):
        task = self.new_task(stage="work")
        with self.assertRaises(ValueError):
            self.engine.send_message(task, "你好")

    def test_failed_task_can_be_requeued(self):
        task = self.new_task(stage="work")
        self.store.update_task(task["id"], where_status="running", status="failed", note="boom")
        requeued = self.store.retry_task(task["id"])
        self.assertEqual(requeued["status"], "pending")
        self.assertIsNone(requeued["note"])
        # A task that is not failed is left alone.
        self.store.update_task(task["id"], where_status="pending", status="running")
        untouched = self.store.retry_task(task["id"])
        self.assertEqual(untouched["status"], "running")

    def test_a_step_can_hang_off_any_stage(self):
        # e.g. dispatch: update the ticket system, then hand over to work.
        self.cfg.projects["tapd"]["steps"]["dispatch"] = 'echo "id=$HUB_EXTERNAL_ID"'
        task = self.new_task(stage="dispatch")
        self.engine.process(task)
        advanced = self.store.get_task(task["id"])
        self.assertEqual(advanced["stage"], "work")
        self.assertEqual(advanced["status"], "pending")
        runs = self.store.task_detail(task["id"])["runs"]
        self.assertTrue(any(run["agent"] == "step:dispatch" for run in runs))

    def test_review_gate_focuses_on_the_delivery(self):
        # Work first so there is something the agent delivered.
        task = self.new_task(stage="work")
        self.engine.process(task)  # work -> review (pending)
        task = self.store.get_task(task["id"])
        self.store.update_task(task["id"], where_status="pending", status="running")
        self.engine.process(self.store.get_task(task["id"]))
        detail = self.store.get_approval(task["id"], "review")["detail"]
        self.assertIn("要你决定", detail)
        self.assertIn("agent 交付", detail)
        self.assertIn("变动", detail)
        # The review card does not re-dump the ticket digest.
        self.assertNotIn("缺陷标题", detail)

    def test_done_sends_a_completion_card(self):
        notifier = RecordingNotifier()
        engine = Engine(self.store, self.cfg, agent=self.agent, notifiers={"rec": notifier})
        task = self.new_task(stage="finish")
        engine.process(task)
        self.assertEqual(len(notifier.sent), 1)
        title, body, links = notifier.sent[0]
        self.assertIn("任务完成", title)
        self.assertIn("完成内容", body)
        self.assertIn("finished 1148599968", body)
        self.assertTrue(links[0][1].endswith("/"))

    def test_a_failed_task_notifies_too(self):
        notifier = RecordingNotifier()
        engine = Engine(self.store, self.cfg, agent=self.agent, notifiers={"rec": notifier})
        self.cfg.projects["tapd"]["steps"]["finish"] = "echo boom >&2; exit 3"
        task = self.new_task(stage="finish")
        engine.process(task)
        self.assertEqual(len(notifier.sent), 1)
        self.assertIn("任务失败", notifier.sent[0][0])
        self.assertIn("boom", notifier.sent[0][1])

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