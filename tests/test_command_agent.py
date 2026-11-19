"""Command agent: generic command execution + configured output parsing.

No product-specific code is tested here on purpose — t3, claude and codex are
all just different `agents` blocks.
"""

import datetime
import json
import time
import unittest

from hub.adapters import load_agent
from hub.adapters.shell_agent import CommandAgent


class CommandAgentTest(unittest.TestCase):
    def test_shell_alias_still_works(self):
        agent = load_agent({"type": "shell", "command": "echo done"})
        result = agent.start({}, "prompt")
        self.assertTrue(result["ok"])
        self.assertEqual(result["output"], "done")
        self.assertEqual(result["status"], "pending")

    def test_command_kind_is_accepted(self):
        agent = load_agent({"type": "command", "command": "echo hi"})
        self.assertIsInstance(agent, CommandAgent)

    def test_json_output_supplies_session_and_link(self):
        agent = CommandAgent(
            {
                "type": "command",
                "command": 'echo \'{"threadId":"t-1","link":"https://example.com/t/1",'
                '"summary":"did the work"}\'',
                "parse": {
                    "format": "json",
                    "session": "threadId",
                    "link": "link",
                    "output": "summary",
                },
            }
        )
        result = agent.start({}, "prompt")
        self.assertEqual(result["session"], "t-1")
        self.assertEqual(result["link"], "https://example.com/t/1")
        self.assertEqual(result["output"], "did the work")

    def test_json_on_the_last_line_after_logs(self):
        agent = CommandAgent(
            {
                "type": "command",
                "command": "printf 'starting...\\n{\"threadId\":\"t-2\"}\\n'",
                "parse": {"format": "json", "session": "threadId"},
            }
        )
        self.assertEqual(agent.start({}, "p")["session"], "t-2")

    def test_nested_path(self):
        agent = CommandAgent(
            {
                "type": "command",
                "command": 'echo \'{"data":{"thread":{"id":"t-3"}}}\'',
                "parse": {"format": "json", "session": "data.thread.id"},
            }
        )
        self.assertEqual(agent.start({}, "p")["session"], "t-3")

    def test_exit_code_mapping_can_retry(self):
        agent = CommandAgent({"command": "exit 2", "exit_codes": {"2": "pending"}})
        result = agent.start({}, "p")
        self.assertEqual(result["status"], "pending")
        self.assertTrue(result["ok"])

    def test_unmapped_non_zero_is_failed(self):
        agent = CommandAgent({"command": "echo boom >&2; exit 3"})
        result = agent.start({}, "p")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["ok"])
        self.assertIn("boom", result["note"])

    def test_timeout_is_a_failure(self):
        agent = CommandAgent({"command": "sleep 5", "timeout": 0.2})
        result = agent.start({}, "p")
        self.assertEqual(result["status"], "failed")
        self.assertIn("timeout", result["note"] or result["error"])

    def test_task_identity_reaches_the_command(self):
        agent = CommandAgent({"command": 'echo "$HUB_SOURCE:$HUB_EXTERNAL_ID:$HUB_PAYLOAD"'})
        result = agent.start(
            {"source": "tapd", "external_id": "42", "payload": {"bug": {"id": 42}}}, "p"
        )
        self.assertEqual(result["output"].strip(), 'tapd:42:{"bug": {"id": 42}}')

    def test_send_relays_the_message_on_stdin(self):
        agent = CommandAgent({"command": "echo start", "send": "cat"})
        result = agent.send({"session": "t-1"}, "多行指令\n第二行")
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "多行指令\n第二行")

    def test_send_without_a_command_is_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            CommandAgent({"command": "echo start"}).send({"session": "t-1"}, "hi")

    def test_an_oversized_prompt_keeps_both_ends(self):
        from hub.adapters.shell_agent import _cap

        capped = _cap("开头" + "x" * 100000 + "结论", 1000)
        self.assertLessEqual(len(capped), 1000)
        self.assertTrue(capped.startswith("开头"))
        self.assertTrue(capped.endswith("结论"))

    def test_ticket_id_prefers_the_ticket_over_the_event(self):
        agent = CommandAgent({"command": 'echo "$HUB_TICKET_ID"'})
        comment = agent.start(
            {"external_id": "comment-1", "payload": {"entity_id": "bug-9", "id": "comment-1"}}, "p"
        )
        self.assertEqual(comment["output"].strip(), "bug-9")
        update = agent.start({"external_id": "bug-9", "payload": {"id": "bug-9"}}, "p")
        self.assertEqual(update["output"].strip(), "bug-9")
        bare = agent.start({"external_id": "42"}, "p")
        self.assertEqual(bare["output"].strip(), "42")

    def test_workspace_becomes_cwd(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            agent = CommandAgent({"command": "pwd"})
            result = agent.start({"workspace": tmp}, "p")
            self.assertEqual(result["output"].strip(), str(tmp))


# ------------------------------------------------------------- async mode

    def async_agent(self, poll_command: str, command: str = None) -> CommandAgent:
        return CommandAgent(
            {
                "command": command
                or "printf 'async=%s' \"$HUB_ASYNC\" >&2; echo '{\"threadId\":\"t-9\"}'",
                "parse": {"format": "json", "session": "threadId"},
                "async": True,
                "poll": {"command": poll_command, "interval": 7},
            }
        )

    def test_async_start_returns_poll_instead_of_waiting(self):
        agent = self.async_agent("true")
        result = agent.start({}, "p")
        self.assertEqual(result["status"], "poll")
        self.assertEqual(result["session"], "t-9")
        self.assertEqual(result["poll_interval"], 7)
        # The dispatch script learns it must not block.
        self.assertIn("async=1", result["error"])

    def test_async_start_without_a_session_fails(self):
        agent = self.async_agent("true", command="echo '{}'")
        result = agent.start({}, "p")
        self.assertEqual(result["status"], "failed")
        self.assertIn("会话", result["note"])

    def test_poll_maps_thread_state_to_running_done_failed(self):
        running = self.async_agent(
            """printf '%s' '{"thread":{"status":"running"},"messages":[{"role":"assistant","text":"半成品"}]}'"""
        ).poll({})
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["text"], "半成品")

        done = self.async_agent(
            """printf '%s' '{"thread":{"status":"completed"},"messages":[{"role":"user","text":"go"},{"role":"assistant","text":"改完了"}]}'"""
        ).poll({})
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["text"], "改完了")

        failed = self.async_agent(
            """printf '%s' '{"thread":{"status":"failed"},"messages":[{"role":"assistant","text":"构建挂了"}]}'"""
        ).poll({})
        self.assertEqual(failed["status"], "failed")
        self.assertIn("构建挂了", failed["error"])

    def test_a_just_completed_turn_is_not_a_finished_job(self):
        # 2026-11-19: the release agent said «已启动，正在等结果» and ended its
        # turn; the thread read `completed` and the task was closed before the
        # deploy (a new run) came back. A recently updated thread keeps polling.
        def thread(updated_at):
            body = json.dumps({"thread": {"status": "completed", "updatedAt": updated_at}})
            return self.async_agent("printf '%s' '" + body + "'").poll({})

        now = datetime.datetime.now(datetime.timezone.utc)
        fresh = thread(now.isoformat().replace("+00:00", "Z"))
        self.assertEqual(fresh["status"], "running")

        old = now - datetime.timedelta(minutes=5)
        quiet = thread(old.isoformat().replace("+00:00", "Z"))
        self.assertEqual(quiet["status"], "done")

    def test_a_fresh_poll_pass_waits_even_if_the_thread_is_old(self):
        # A session stage starts with a prompt, so the thread may read
        # `completed` with an old `updatedAt` before the agent even runs; the
        # pass's own age has to cover the settle window too.
        body = json.dumps({"thread": {"status": "completed", "updatedAt": "2020-01-01T00:00:00Z"}})
        agent = self.async_agent("printf '%s' '" + body + "'")
        result = agent.poll({"poll_started_at": time.time()})
        self.assertEqual(result["status"], "running")

    def test_a_failing_poll_command_keeps_the_task_running(self):
        # Transient CLI errors must not fail a live task; max_seconds ends it.
        result = self.async_agent("exit 4").poll({})
        self.assertEqual(result["status"], "running")
        self.assertIn("exit 4", result["error"])

    def test_async_agent_without_poll_command_waits_normally(self):
        agent = CommandAgent(
            {
                "command": "echo '{\"threadId\":\"t-1\"}'",
                "parse": {"format": "json", "session": "threadId"},
                "async": True,
            }
        )
        self.assertNotEqual(agent.start({}, "p")["status"], "poll")


if __name__ == "__main__":
    unittest.main()