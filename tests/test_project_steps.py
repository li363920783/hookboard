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

    def test_digest_scrubs_credentials_pasted_into_the_ticket(self):
        payload = {"bug": {"title": "超时", "severity": "严重", "url": "u",
                           "description": "接口 x token：Bearer fake-token-for-tests-0000"}}
        self.cfg.projects["tapd"]["fields"] = {"描述": "bug.description"}
        task = self.new_task(payload=payload)
        digest = self.engine._task_digest(task)
        self.assertIn("Bearer ***", digest)
        self.assertNotIn("fake-token-for-tests", digest)

    def test_digest_fields_accept_a_path_list(self):
        self.cfg.projects["tapd"]["fields"] = {"缺陷标题": ["title", "old_title"]}
        task = self.new_task(payload={"old_title": "TAPD 只在更新事件里给 old_title"})
        digest = self.engine._task_digest(task)
        self.assertIn("TAPD 只在更新事件里给 old_title", digest)

    def test_digest_names_the_ticket_behind_a_comment_event(self):
        # The comment id is the dedupe key; the bug the agent must read is
        # entity_id, and a card that shows only the comment id sends the agent
        # looking up a comment as if it were a bug.
        task, _ = self.store.create_task(
            source="tapd",
            kind="bug_comment::add",
            external_id="1148599968001000757",
            title="Pad端测试",
            stage="plan",
            project="tapd",
            payload={
                "event": "bug_comment::add",
                "id": "1148599968001000757",
                "entity_id": "1148599968001000430",
            },
        )
        digest = self.engine._task_digest(task)
        self.assertIn("条目：1148599968001000757", digest)
        self.assertIn("单据：1148599968001000430", digest)

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

    def test_step_context_survives_the_gates_and_reaches_the_prompt(self):
        # The ticket context a collect step writes must outlive the approval
        # (which clears `note`) and show up in the agent prompt.
        cfg = Config(stages=["intake", "collect", "plan", "work", "finish"], approve_at=["plan"])
        cfg.projects = {"tapd": {"steps": {"collect": {
            "command": "echo '{\"context\": \"单据：bug 1 · 完整描述\", \"output\": \"ok\"}'",
            "parse": {"format": "json", "context": "context", "output": "output"},
        }}}}
        engine = Engine(self.store, cfg, agent=self.agent, notifiers={})
        task, _ = self.store.create_task(
            source="tapd", kind="bug", external_id="1", title="单据", stage="collect", project="tapd"
        )
        self.store.update_task(task["id"], where_status="pending", status="running")
        engine.process(self.store.get_task(task["id"]))
        saved = self.store.get_task(task["id"])
        self.assertEqual(saved["context"], "单据：bug 1 · 完整描述")
        self.assertEqual(saved["stage"], "plan")

        # Enter the plan gate, then answer it; note is cleared, context is not.
        self.store.update_task(task["id"], where_status="pending", status="running")
        engine.process(self.store.get_task(task["id"]))
        approval = self.store.get_approval(task["id"], "plan")
        engine.decide(approval["token"], index=0)
        after = self.store.get_task(task["id"])
        self.assertIsNone(after["note"])
        self.assertEqual(after["context"], "单据：bug 1 · 完整描述")

        self.store.update_task(task["id"], where_status="pending", status="running")
        engine.process(self.store.get_task(task["id"]))
        self.assertIn("单据：bug 1 · 完整描述", self.agent.prompts[-1])
        self.assertIn("执行约束", self.agent.prompts[-1])

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

    def test_review_gate_shows_the_ticket_and_the_delivery(self):
        # Work first so there is something the agent delivered.
        task = self.new_task(stage="work")
        self.store.update_task(
            task["id"], where_status="running", context="**描述**：二次转发后待办仍显示第一次备注"
        )
        self.engine.process(self.store.get_task(task["id"]))  # work -> review (pending)
        task = self.store.get_task(task["id"])
        self.store.update_task(task["id"], where_status="pending", status="running")
        self.engine.process(self.store.get_task(task["id"]))
        detail = self.store.get_approval(task["id"], "review")["detail"]
        # What the ticket asked for, and what the agent delivered.
        self.assertIn("单据要求", detail)
        self.assertIn("二次转发后待办仍显示第一次备注", detail)
        self.assertIn("agent 交付", detail)
        # What either button does.
        self.assertIn("回写单据状态", detail)
        self.assertIn("发回原会话", detail)
        # The review card does not re-dump the raw field digest.
        self.assertNotIn("缺陷标题", detail)

    def test_review_card_keeps_the_end_of_a_long_delivery(self):
        task = self.new_task(stage="work")
        long = "收到，我先看下代码。" + "过程细节。" * 800 + "结论：已修复 b636c00bc，测试通过"
        self.store.add_run(task["id"], "work", "rec", 0, 1, True, long)
        summary = self.engine._delivery_summary(self.store.get_task(task["id"]))
        self.assertLessEqual(len(summary), 3000)
        self.assertTrue(summary.startswith("…"))
        self.assertIn("结论：已修复 b636c00bc", summary)

    def test_rejecting_review_sends_the_comment_back_to_the_session(self):
        task = self.new_task(stage="review")
        self.engine.process(task)  # opens the review gate
        self.store.update_task(task["id"], where_status="waiting", session="t-9")
        approval = self.store.get_approval(task["id"], "review")
        _, after, changed = self.engine.decide(
            approval["token"], index=1, text="加急标记没做，补上"
        )
        self.assertTrue(changed)
        self.assertIn("加急标记没做", self.agent.messages[-1])
        self.assertEqual(after["status"], "pending")
        self.assertEqual(after["stage"], "work")
        self.assertEqual(after["polling"], 1)

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

    def test_a_merged_placeholder_does_not_shadow_the_session_task(self):
        # Ticket 428 regressed here: comment #1 dispatched a thread, comment #2
        # was merged into it and closed as a session-less placeholder, then
        # comment #3 picked the placeholder as "previous" and opened a second
        # thread instead of messaging the first one.
        payload = {"id": "777", "title": "收单失败"}

        def event(external_id):
            task, _ = self.store.create_task(
                source="tapd", kind="bug_comment::add", external_id=external_id,
                title="收单失败", stage="intake", project="tapd", payload=payload,
            )
            return task

        first = event("c-1")
        self.store.update_task(first["id"], where_status="pending", status="running", session="t-1")
        self.store.update_task(first["id"], where_status="running", status="done")

        second = event("c-2")
        self.store.update_task(second["id"], where_status="pending", status="running")
        self.assertTrue(self.engine._merge_into_previous(self.store.get_task(second["id"])))

        third = event("c-3")
        self.store.update_task(third["id"], where_status="pending", status="running")
        self.assertTrue(self.engine._merge_into_previous(self.store.get_task(third["id"])))
        self.assertEqual(len(self.agent.messages), 2)
        self.assertIn(f"并入任务 #{first['id']}", self.store.get_task(third["id"])["note"])
        self.assertFalse([t for t in self.agent.tasks])

    def test_reentering_an_answered_gate_asks_again(self):
        # A follow-up sends a finished task back into a gate it already passed;
        # the old answer must not fast-forward the new work ("review (recovered)").
        notifier = RecordingNotifier()
        engine = Engine(self.store, self.cfg, agent=self.agent, notifiers={"rec": notifier})
        task = self.new_task(stage="plan")
        engine.process(task)
        first = self.store.get_approval(task["id"], "plan")
        engine.decide(first["token"], index=0)

        self.store.update_task(task["id"], where_status="pending", status="running", stage="plan")
        engine.process(self.store.get_task(task["id"]))

        again = self.store.get_approval(task["id"], "plan")
        self.assertEqual(again["status"], "open")
        self.assertNotEqual(again["token"], first["token"])
        self.assertEqual(again["attempt"], 1)
        self.assertEqual(self.store.get_task(task["id"])["status"], "waiting")
        self.assertEqual(len(notifier.sent), 2)

    def test_context_is_scrubbed_before_the_prompt_and_the_plan_card(self):
        # A collect step may copy a ticket body verbatim; it must not carry a
        # live token into the prompt or the approval card.
        task = self.new_task(stage="plan")
        self.store.update_task(
            task["id"], where_status="running", context="接口 x 需要 token=abcdef123456"
        )
        self.engine.process(self.store.get_task(task["id"]))
        detail = self.store.get_approval(task["id"], "plan")["detail"]
        self.assertNotIn("abcdef123456", detail)
        # And in the work prompt (after the gate is answered).
        self.store.update_task(task["id"], where_status="waiting", status="running", stage="work")
        self.engine.process(self.store.get_task(task["id"]))
        self.assertNotIn("abcdef123456", self.agent.prompts[-1])
        self.assertIn("token：***", self.agent.prompts[-1])

    def test_follow_up_on_a_waiting_task_with_a_session_is_polled_again(self):
        # Sending the change into the session of a task parked at a gate must
        # also make that task poll the run, or the reply is never collected.
        payload = {"id": "778", "title": "收单失败"}
        first, _ = self.store.create_task(
            source="tapd", kind="bug_comment::add", external_id="c-1", title="单据",
            stage="review", project="tapd", payload=payload,
        )
        self.store.update_task(first["id"], where_status="pending", status="waiting", session="t-1")
        second, _ = self.store.create_task(
            source="tapd", kind="bug_comment::add", external_id="c-2", title="单据",
            stage="intake", project="tapd", payload=payload,
        )
        self.store.update_task(second["id"], where_status="pending", status="running")
        self.assertTrue(self.engine._merge_into_previous(self.store.get_task(second["id"])))
        after = self.store.get_task(first["id"])
        self.assertEqual(after["status"], "pending")
        self.assertEqual(after["stage"], "work")
        self.assertEqual(after["polling"], 1)
        self.assertEqual(len(self.agent.messages), 1)

    def test_reworking_a_parked_task_replaces_its_stale_gate(self):
        payload = {"id": "779", "title": "收单失败"}
        first, _ = self.store.create_task(
            source="tapd", kind="bug_comment::add", external_id="c-1", title="单据",
            stage="review", project="tapd", payload=payload,
        )
        self.store.update_task(first["id"], where_status="pending", status="running", session="t-1")
        self.engine.process(self.store.get_task(first["id"]))  # opens the review gate
        old = self.store.get_approval(first["id"], "review")

        second, _ = self.store.create_task(
            source="tapd", kind="bug_comment::add", external_id="c-2", title="单据",
            stage="intake", project="tapd", payload=payload,
        )
        self.store.update_task(second["id"], where_status="pending", status="running")
        self.assertTrue(self.engine._merge_into_previous(self.store.get_task(second["id"])))
        self.assertEqual(self.store.get_approval(first["id"], "review")["status"], "superseded")

        # Coming back into the gate asks again with a fresh card.
        self.store.update_task(
            first["id"], where_status="pending", status="running", stage="review"
        )
        self.engine.process(self.store.get_task(first["id"]))
        fresh = self.store.get_approval(first["id"], "review")
        self.assertEqual(fresh["status"], "open")
        self.assertNotEqual(fresh["token"], old["token"])
        self.assertEqual(fresh["attempt"], 1)

    def test_find_ticket_task_matches_a_numeric_payload_id(self):
        # TAPD sends ids as strings, but a numeric payload id must not silently
        # miss: JSON integers do not equal a TEXT parameter in SQLite.
        task, _ = self.store.create_task(
            source="tapd", kind="bug", external_id="c-1", title="单据",
            stage="work", project="tapd", payload={"id": 428},
        )
        found = self.store.find_ticket_task("tapd", "428", 999)
        self.assertEqual(found["id"], task["id"])

    def test_a_session_step_continues_the_existing_session(self):
        # Release: after review the agent still holds the branch/build context,
        # so the stage messages the same session and polls it instead of
        # starting a fresh run.
        cfg = Config(stages=["work", "review", "release", "finish"], approve_at=["review"])
        cfg.projects = {
            "tapd": {"steps": {"release": {"session": "复核已通过，请发版", "interval": 0.01}}}
        }
        rec = RecordingNotifier()
        engine = Engine(self.store, cfg, agent=self.agent, notifiers={"rec": rec})
        task, _ = self.store.create_task(
            source="tapd", kind="bug", external_id="1", title="单据",
            stage="release", project="tapd",
        )
        self.store.update_task(task["id"], where_status="pending", status="running", session="t-1")

        engine.process(self.store.get_task(task["id"]))
        self.assertIn("复核已通过，请发版", self.agent.messages[-1])
        waiting = self.store.get_task(task["id"])
        self.assertEqual(waiting["status"], "pending")
        self.assertEqual(waiting["stage"], "release")
        self.assertEqual(waiting["polling"], 1)
        # Entering a long session stage must announce itself, not wait for the end.
        self.assertEqual(len(rec.sent), 1)
        self.assertIn("开始发版", rec.sent[0][0])
        self.assertEqual(rec.context.get("notice_kind"), "stage")

        self.store.update_task(task["id"], where_status="pending", status="running")
        engine.process(self.store.get_task(task["id"]))
        done = self.store.get_task(task["id"])
        self.assertEqual(done["stage"], "finish")
        self.assertEqual(len(self.agent.messages), 1)  # polling never re-sends
        self.assertEqual(len(rec.sent), 1)  # polling ticks do not re-announce

    def test_a_session_step_sends_after_a_crash_before_the_message(self):
        # requeue_stale turns a stale running task into polling=1; if the crash
        # happened before the message went out, the stage must send it instead
        # of watching the previous (finished) run.
        cfg = Config(stages=["work", "release", "finish"], approve_at=[])
        cfg.projects = {"tapd": {"steps": {"release": {"session": "请发版", "interval": 0.01}}}}
        engine = Engine(self.store, cfg, agent=self.agent, notifiers={})
        task, _ = self.store.create_task(
            source="tapd", kind="bug", external_id="1", title="单据",
            stage="release", project="tapd",
        )
        self.store.update_task(
            task["id"], where_status="pending", status="running", session="t-1",
            polling=1, poll_started_at=1.0,
        )
        engine.process(self.store.get_task(task["id"]))
        self.assertEqual(len(self.agent.messages), 1)
        self.assertIn("请发版", self.agent.messages[-1])

    def test_a_session_step_without_a_session_fails(self):
        cfg = Config(stages=["work", "release", "finish"], approve_at=[])
        cfg.projects = {"tapd": {"steps": {"release": {"session": "发版"}}}}
        engine = Engine(self.store, cfg, agent=self.agent, notifiers={})
        task, _ = self.store.create_task(
            source="tapd", kind="bug", external_id="1", title="单据",
            stage="release", project="tapd",
        )
        self.store.update_task(task["id"], where_status="pending", status="running")
        engine.process(self.store.get_task(task["id"]))
        failed = self.store.get_task(task["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("没有会话", failed["note"])

    def test_long_text_keeps_lines_and_marks_truncation(self):
        from hub.engine import long_text

        self.assertIn("第一行\n第二行", long_text("第一行\n第二行"))
        cut = long_text("x" * 50, 10)
        self.assertTrue(cut.startswith("x" * 10))
        self.assertIn("已截断", cut)

    def test_rework_stage_never_skips_forward_over_the_gate(self):
        cfg = Config(stages=["plan", "work", "review"], approve_at=["plan", "review"])
        engine = Engine(self.store, cfg, agent=self.agent, notifiers={})
        # The plan gate has nothing before it: stay failed, don't jump to work.
        self.assertEqual(engine._rework_stage("plan"), "plan")
        self.assertEqual(engine._rework_stage("review"), "work")

    def test_rejection_rework_stage_is_not_hard_coded_to_work(self):
        # A pipeline without a `work` stage must rework in the stage that
        # delivered, never bounce back into the gate it just answered.
        cfg = Config(stages=["intake", "build", "ship"], approve_at=["ship"])
        cfg.projects = {"tapd": {}}
        engine = Engine(self.store, cfg, agent=self.agent, notifiers={})
        task, _ = self.store.create_task(
            source="tapd", kind="bug", external_id="1", title="单据", stage="ship", project="tapd"
        )
        self.store.update_task(task["id"], where_status="pending", status="running")
        engine.process(self.store.get_task(task["id"]))
        self.store.update_task(task["id"], where_status="waiting", session="t-9")
        approval = self.store.get_approval(task["id"], "ship")
        _, after, _ = engine.decide(approval["token"], index=1, text="返工")
        self.assertEqual(after["stage"], "build")
        self.assertEqual(after["status"], "pending")

    def test_failing_step_fails_the_task(self):
        self.cfg.projects["tapd"]["steps"]["finish"] = "echo boom >&2; exit 3"
        task = self.new_task(stage="finish")
        self.engine.process(task)
        failed = self.store.get_task(task["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("boom", failed["note"] or "")


if __name__ == "__main__":
    unittest.main()