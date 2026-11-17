"""Command agent: generic command execution + configured output parsing.

No product-specific code is tested here on purpose — t3, claude and codex are
all just different `agents` blocks.
"""

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

    def test_workspace_becomes_cwd(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            agent = CommandAgent({"command": "pwd"})
            result = agent.start({"workspace": tmp}, "p")
            self.assertEqual(result["output"].strip(), str(tmp))


if __name__ == "__main__":
    unittest.main()