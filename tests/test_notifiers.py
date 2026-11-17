"""Notifier tests (docs/CHANNELS.md).

The webhook and email payload tests run in OUTBOX mode (HUB_OUTBOX=1): no
network request is made, the channel-format payload is written to disk, and we
assert its structure — including that BOTH the HTML and the plain-text email
parts contain the approval link.  Extra tests cover the dedup and retry rules.
"""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from hub.adapters.email_notifier import EmailNotifier
from hub.adapters.feishu_notifier import FeishuNotifier
from hub.adapters.webhook_notifier import WebhookNotifier
from hub.config import Config
from hub.engine import Engine
from hub.store import Store

URL = "http://127.0.0.1:8899/a/token-abc"


class OutboxPayloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.outbox = Path(self.tmp.name) / "outbox"

    def tearDown(self):
        self.tmp.cleanup()

    def _files(self):
        return sorted(self.outbox.glob("*.json"))

    def test_webhook_outbox_payload(self):
        with mock.patch.dict(os.environ, {"HUB_OUTBOX": "1"}):
            notifier = WebhookNotifier({"url": "https://example.com/hook"}, str(self.outbox))
            notifier.context = {"task_id": 7, "stage": "plan"}
            result = notifier.ask("需要审批", "请确认发布", URL, ["通过", "打回"])

        self.assertTrue(result["ok"])
        files = self._files()
        self.assertEqual(len(files), 1)
        self.assertIn("-webhook-", files[0].name)
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["title"], "需要审批")
        self.assertEqual(payload["task_id"], 7)
        self.assertEqual(payload["stage"], "plan")
        self.assertEqual(payload["links"][0]["url"], URL)
        self.assertIn("请确认发布", payload["body"])

    def test_email_outbox_payload_has_link_in_html_and_text(self):
        with mock.patch.dict(os.environ, {"HUB_OUTBOX": "1"}):
            notifier = EmailNotifier(
                {
                    "host": "smtp.example.com",
                    "port": 465,
                    "tls": "ssl",
                    "from": "bot@example.com",
                    "to": ["you@example.com"],
                },
                str(self.outbox),
            )
            notifier.context = {"task_id": 7, "stage": "plan"}
            result = notifier.ask("需要审批", "请确认发布", URL, ["通过", "打回"])

        self.assertTrue(result["ok"])
        files = self._files()
        self.assertEqual(len(files), 1)
        self.assertIn("-email-", files[0].name)
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["multipart"], "alternative")
        self.assertEqual(payload["to"], ["you@example.com"])
        self.assertEqual(payload["subject"], "需要审批")
        # Both representations must carry the approval link.
        self.assertIn(URL, payload["text"])
        self.assertIn(URL, payload["html"])
        self.assertIn("<a ", payload["html"])

    def _feishu_context(self):
        return {
            "task_id": 7,
            "stage": "plan",
            "source": "tapd",
            "external_id": "bug-42",
            "task_title": "修复登录缺陷",
            "question": "阶段「plan」需要确认",
            "token": "token-abc",
            "public_base": "http://127.0.0.1:8899",
        }

    def test_feishu_outbox_interactive_card(self):
        with mock.patch.dict(os.environ, {"HUB_OUTBOX": "1"}):
            notifier = FeishuNotifier({"enabled": True}, str(self.outbox))
            notifier.context = self._feishu_context()
            result = notifier.ask("阶段「plan」需要确认", "详情", URL, ["通过", "打回"])

        self.assertTrue(result["ok"])
        files = self._files()
        self.assertEqual(len(files), 1)
        self.assertIn("-feishu-", files[0].name)
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["msg_type"], "interactive")
        self.assertIn("receive_id", payload)
        content = payload["content"]
        self.assertIsInstance(content, str)
        # Acceptance greps the content for the approval and board links.
        self.assertIn("/a/", content)
        self.assertIn("/api/board", content)
        card = json.loads(content)
        self.assertEqual(card["schema"], "2.0")
        elements = card["body"]["elements"]
        self.assertEqual(elements[0]["tag"], "markdown")
        self.assertEqual(elements[1]["tag"], "action")
        buttons = elements[1]["actions"]
        self.assertEqual(len(buttons), 2)
        self.assertTrue(all(b["type"] == "default" for b in buttons))
        self.assertIn("/a/token-abc", buttons[0]["multi_url"]["url"])
        self.assertEqual(buttons[1]["multi_url"]["url"], "http://127.0.0.1:8899/")

    def test_feishu_missing_credentials_does_not_crash(self):
        # No HUB_OUTBOX, no dry_run, empty credentials: must warn + write outbox,
        # return ok, and never raise.
        with mock.patch.dict(os.environ, {"HUB_OUTBOX": ""}):
            notifier = FeishuNotifier({"enabled": True}, str(self.outbox))
            notifier.context = self._feishu_context()
            result = notifier.ask("需要审批", "详情", URL, ["通过", "打回"])

        self.assertTrue(result["ok"])
        self.assertEqual(len(self._files()), 1)
        payload = json.loads(self._files()[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["msg_type"], "interactive")


    def test_feishu_send_uses_configured_receive_id_type(self):
        # Real-send path (creds present, not dry_run): the request URL must carry
        # the configured receive_id_type and the body the configured receive_id —
        # the target is config-driven, not hardcoded to a group.
        notifier = FeishuNotifier(
            {
                "enabled": True,
                "app_id": "cli_example",
                "app_secret": "x",
                "receive_id_type": "open_id",
                "receive_id": "ou_example_user",
            },
            str(self.outbox),
        )
        notifier.context = self._feishu_context()
        captured = {}

        class FakeResp:
            def __init__(self, body):
                self._body = body

            def read(self):
                return json.dumps(self._body).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            if "tenant_access_token" in req.full_url:
                return FakeResp({"code": 0, "tenant_access_token": "t", "expire": 7200})
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResp({"code": 0, "msg": "success"})

        with mock.patch.dict(os.environ, {"HUB_OUTBOX": ""}), mock.patch(
            "hub.adapters.feishu_notifier.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            result = notifier.ask("需要审批", "详情", URL, ["通过", "打回"])

        self.assertTrue(result["ok"])
        self.assertIn("receive_id_type=open_id", captured["url"])
        self.assertEqual(captured["body"]["receive_id"], "ou_example_user")
        self.assertEqual(captured["body"]["msg_type"], "interactive")
        self.assertEqual(self._files(), [])  # a real send writes no outbox artifact


class CountingNotifier:
    name = "counting"

    def __init__(self):
        self.calls = 0
        self.context = {}

    def notify(self, title, body_md, links):
        self.calls += 1
        return {"ok": True, "detail": "ok"}

    def ask(self, title, body_md, url, options):
        self.calls += 1
        return {"ok": True, "detail": "ok"}


class FailingNotifier:
    name = "failing"

    def __init__(self):
        self.calls = 0
        self.context = {}

    def notify(self, title, body_md, links):
        self.calls += 1
        return {"ok": False, "detail": "boom"}

    def ask(self, title, body_md, url, options):
        self.calls += 1
        return {"ok": False, "detail": "boom"}


class DispatchRuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.db")
        self.cfg = Config(stages=["intake", "plan", "finish"], approve_at=["plan"])

    def tearDown(self):
        self.tmp.cleanup()

    def _engine(self, notifier):
        return Engine(self.store, self.cfg, notifiers={"n": notifier})

    def _reach_gate(self, engine):
        task, _ = self.store.create_task(
            source="t", kind="k", external_id="e", title="demo", stage="intake"
        )
        for _ in range(20):
            current = self.store.get_task(task["id"])
            if current["status"] == "waiting":
                return current, self.store.get_approval(task["id"], "plan")
            if not engine.step():
                break
        self.fail("did not reach the plan gate")

    def test_one_send_per_task_stage_notifier(self):
        notifier = CountingNotifier()
        engine = self._engine(notifier)
        task, approval = self._reach_gate(engine)
        self.assertEqual(notifier.calls, 1)

        # Re-running the same stage must not send again.
        engine._dispatch_ask(task, approval)
        self.assertEqual(notifier.calls, 1)
        self.assertFalse(self.store.claim_notification(task["id"], "plan", "n"))

    def test_failure_retries_once_and_never_blocks(self):
        notifier = FailingNotifier()
        engine = self._engine(notifier)
        task, approval = self._reach_gate(engine)

        self.assertEqual(notifier.calls, 2)  # first attempt + one retry
        self.assertEqual(self.store.get_task(task["id"])["status"], "waiting")
        kinds = [e["kind"] for e in self.store.events_for(task["id"])]
        self.assertIn("notify_error", kinds)


if __name__ == "__main__":
    unittest.main()