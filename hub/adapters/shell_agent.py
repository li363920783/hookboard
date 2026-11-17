"""Shell agent: run one command and treat stdout as the result.

Task data reaches the command through environment variables (never string
interpolation), so a hostile task title cannot inject shell syntax.

Config::

    "agents": {"default": {"type": "shell", "command": "echo done", "timeout": 60}}
"""

from __future__ import annotations

import os
import subprocess


class ShellAgent:
    name = "shell"

    def __init__(self, options: dict | None = None):
        options = options or {}
        self.command = options.get("command") or "echo done"
        self.timeout = float(options.get("timeout", 60))
        self.cwd = options.get("cwd")

    def start(self, task: dict, prompt: str) -> dict:
        env = dict(os.environ)
        env.update(
            {
                "HUB_TASK_ID": str(task.get("id", "")),
                "HUB_TASK_TITLE": task.get("title") or "",
                "HUB_TASK_STAGE": task.get("stage") or "",
                "HUB_TASK_NOTE": task.get("note") or "",
                "HUB_PROMPT": prompt,
            }
        )
        try:
            proc = subprocess.run(
                self.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.cwd,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "session": None,
                "link": None,
                "error": f"timeout after {self.timeout}s",
                "output": "",
            }
        except Exception as exc:  # bad cwd, missing shell, ...
            return {"ok": False, "session": None, "link": None, "error": str(exc), "output": ""}
        return {
            "ok": proc.returncode == 0,
            "session": None,
            "link": None,
            "error": (proc.stderr or "").strip()[-2000:],
            "output": (proc.stdout or "").strip()[-4000:],
            "returncode": proc.returncode,
        }

    def poll(self, task: dict) -> dict:
        # The command already finished inside start(); nothing to poll.
        return {"ok": True, "status": "done", "text": "", "error": None}

    def send(self, task: dict, message: str) -> dict:
        raise NotImplementedError("shell agent has no session to continue")


def register(options: dict | None = None) -> ShellAgent:
    return ShellAgent(options)