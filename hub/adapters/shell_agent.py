"""Command agent: run one command and interpret its output from config.

Task data reaches the command through environment variables (never string
interpolation), so a hostile task title cannot inject shell syntax:
``HUB_TASK_ID``, ``HUB_TASK_TITLE``, ``HUB_TASK_STAGE``, ``HUB_TASK_NOTE``,
``HUB_PROJECT``, ``HUB_WORKSPACE``, ``HUB_PROMPT``, ``HUB_SOURCE``,
``HUB_KIND``, ``HUB_EXTERNAL_ID``, ``HUB_SESSION`` and ``HUB_PAYLOAD``
(the hook body as JSON, capped at 60000 characters) and ``HUB_BRANCH``.

This is the generic adapter behind both ``type: "shell"`` and
``type: "command"`` — there is no per-product code here.  Anything that can be
driven from a command line (Claude Code, Codex, ``t3 thread start``, a script of
your own) is just a different config block.

Config::

    "agents": {
      "default": {"type": "shell", "command": "echo done", "timeout": 60},

      "t3": {
        "type": "command",
        "command": "t3 thread start \\"$HUB_WORKSPACE\\" \\"$HUB_PROMPT\\" --wait --json",
        "timeout": 1800,
        "parse": {"format": "json", "session": "threadId", "link": "link"},
        "exit_codes": {"2": "failed"}
      },

      "cc": {
        "type": "command",
        "command": "claude -p \\"$HUB_PROMPT\\" --output-format json --model claude-sonnet-5-5",
        "timeout": 1800,
        "parse": {"format": "json", "session": "session_id"}
      }
    }

``parse`` keys are dotted paths into the command's JSON output:
``session`` (stored as the task session), ``link`` (shown to humans),
``output`` (what to record instead of raw stdout) and — for a routing step —
``workspace``, ``branch`` and ``note`` (stored on the task, branch/reason
shown on the approval card).  ``exit_codes`` maps a
non-zero exit status to a task status (``pending`` retries, ``failed`` stops);
anything unmapped is ``failed``.

``async`` + ``poll`` (optional): ``command`` gets ``HUB_ASYNC=1``, should return
as soon as the run is queued, and must print a session id.  The engine then runs
``poll.command`` every ``poll.interval`` seconds instead of blocking a worker::

    "async": true,
    "poll": {"command": "t3 thread show \\"$HUB_SESSION\\" --json",
             "interval": 15, "max_seconds": 2400}

The run state is read from ``parse.state`` (default ``thread.status``); states
in ``poll.running`` keep waiting, ``poll.failed`` ends the task as failed and
anything else counts as done.  The last agent reply (``parse.reply``, default
``messages``) becomes the delivered output.
"""

from __future__ import annotations

import json
import os
import subprocess


class CommandAgent:
    kind = "command"
    name = "command"

    def __init__(self, options: dict | None = None):
        options = options or {}
        self.command = options.get("command") or "echo done"
        self.timeout = float(options.get("timeout", 60))
        # Optional continuation: send a message into an existing session
        # (e.g. `t3 thread send "$HUB_SESSION" -`).  The message arrives on
        # stdin, so long instructions need no shell quoting.
        self.send_command = options.get("send") or ""
        self.send_timeout = float(options.get("send_timeout", 30))
        self.cwd = options.get("cwd")
        self.parse = dict(options.get("parse") or {})
        self.exit_codes = {
            int(code): str(status) for code, status in (options.get("exit_codes") or {}).items()
        }
        # Async mode: the start command returns as soon as the run is queued
        # (HUB_ASYNC=1), then each engine tick polls ``poll.command`` with
        # HUB_SESSION instead of holding a worker thread for the whole run.
        self.async_mode = bool(options.get("async"))
        poll = dict(options.get("poll") or {})
        self.poll_command = str(poll.get("command") or "")
        self.poll_interval = float(poll.get("interval", 15))
        self.poll_max_seconds = float(poll.get("max_seconds", 0) or 0)
        self.poll_running = [
            str(v) for v in (poll.get("running") or ["running", "starting", "queued", "preparing"])
        ]
        self.poll_failed = [
            str(v) for v in (poll.get("failed") or ["failed", "error", "cancelled", "interrupted"])
        ]

    # ------------------------------------------------------------------ run

    def _workspace(self, task: dict):
        # A project's `workspace` overrides the agent's own cwd, so different
        # projects can run the same agent in different checkouts.
        return task.get("workspace") or self.cwd

    def _env(self, task: dict, prompt: str = "", message: str = "") -> dict:
        workspace = self._workspace(task)
        env = dict(os.environ)
        env.update(
            {
                "HUB_TASK_ID": str(task.get("id", "")),
                "HUB_TASK_TITLE": task.get("title") or "",
                "HUB_TASK_STAGE": task.get("stage") or "",
                "HUB_TASK_NOTE": task.get("note") or "",
                "HUB_PROJECT": task.get("project") or "",
                "HUB_WORKSPACE": workspace or "",
                "HUB_BRANCH": task.get("branch") or "",
                "HUB_PROMPT": prompt,
                "HUB_MESSAGE": message,
                "HUB_SOURCE": task.get("source") or "",
                "HUB_KIND": task.get("kind") or "",
                "HUB_EXTERNAL_ID": task.get("external_id") or "",
                "HUB_SESSION": task.get("session") or "",
            }
        )
        if task.get("payload") is not None:
            try:
                env["HUB_PAYLOAD"] = json.dumps(task["payload"], ensure_ascii=False)[:60000]
            except (TypeError, ValueError):
                pass
        return env

    def start(self, task: dict, prompt: str) -> dict:
        workspace = self._workspace(task)
        env = self._env(task, prompt)
        async_start = bool(self.async_mode and self.poll_command)
        if async_start:
            # The command may use this to skip a blocking wait; the engine
            # polls `poll.command` afterwards instead.
            env["HUB_ASYNC"] = "1"
        try:
            proc = subprocess.run(
                self.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=workspace,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "session": None,
                "link": None,
                "error": f"timeout after {self.timeout}s",
                "output": "",
                "status": "failed",
                "note": f"timeout after {self.timeout}s",
            }
        except Exception as exc:  # bad cwd, missing shell, ...
            return {
                "ok": False,
                "session": None,
                "link": None,
                "error": str(exc),
                "output": "",
                "status": "failed",
                "note": str(exc),
            }

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        status = self.exit_codes.get(proc.returncode)
        if status is None:
            status = "pending" if proc.returncode == 0 else "failed"
        parsed = self._parse(stdout)
        output = parsed.get("output") or stdout
        if async_start and proc.returncode == 0:
            if not parsed.get("session"):
                return {
                    "ok": False,
                    "session": None,
                    "link": None,
                    "error": "异步派单没有返回会话 id",
                    "output": output[-4000:],
                    "status": "failed",
                    "note": "异步派单没有返回会话 id（检查 parse.session）",
                }
            return {
                "ok": True,
                "session": parsed.get("session"),
                "link": parsed.get("link"),
                "error": stderr[-2000:],
                "output": output[-4000:],
                "returncode": 0,
                "status": "poll",
                "note": None,
                "poll_interval": self.poll_interval,
                "workspace": parsed.get("workspace"),
                "branch": parsed.get("branch"),
            }
        note = parsed.get("note")
        if not note and status != "pending":
            note = (stderr or stdout)[-2000:] or f"exit {proc.returncode}"
        return {
            "ok": proc.returncode == 0 or status == "pending",
            "session": parsed.get("session"),
            "link": parsed.get("link"),
            "error": stderr[-2000:],
            "output": (output or "")[-4000:],
            "returncode": proc.returncode,
            "status": status,
            "note": note,
            "workspace": parsed.get("workspace"),
            "branch": parsed.get("branch"),
        }

    # -------------------------------------------------------------- parsing

    def _parse(self, stdout: str) -> dict:
        if (self.parse.get("format") or "").lower() != "json" or not stdout:
            return {}
        data = None
        try:
            data = json.loads(stdout)
        except ValueError:
            # Some CLIs print a log line first; try the last non-empty line.
            for line in reversed(stdout.splitlines()):
                if line.strip():
                    try:
                        data = json.loads(line)
                    except ValueError:
                        pass
                    break
        if not isinstance(data, dict):
            return {}
        result = {}
        for key in ("session", "link", "output", "workspace", "branch", "note"):
            path = self.parse.get(key)
            if path:
                value = self._dig(data, path)
                if value not in (None, ""):
                    result[key] = value if key == "output" else str(value)
        return result

    @staticmethod
    def _dig(data, path: str):
        current = data
        for part in str(path).split("."):
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                index = int(part)
                current = current[index] if index < len(current) else None
            else:
                return None
        return current

    def poll(self, task: dict) -> dict:
        """Ask what happened to the run started earlier (async mode).

        Returns ``status`` = ``running`` / ``done`` / ``failed``.  A failing
        poll command counts as ``running``: a transient CLI error must not fail
        a task that is still working; ``max_seconds`` ends it eventually.
        """
        if not self.poll_command:
            # The command already finished inside start(); nothing to poll.
            return {"ok": True, "status": "done", "text": "", "error": None}
        try:
            proc = subprocess.run(
                self.poll_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=min(self.timeout, 120),
                cwd=self._workspace(task),
                env=self._env(task),
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "status": "running", "text": "", "error": "poll timeout"}
        except Exception as exc:  # missing binary, bad cwd, ...
            return {"ok": False, "status": "running", "text": "", "error": str(exc)}
        if proc.returncode != 0:
            detail = ((proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}")[-500:]
            return {"ok": False, "status": "running", "text": "", "error": detail}

        doc = None
        try:
            doc = json.loads(proc.stdout or "")
        except ValueError:
            for line in reversed((proc.stdout or "").splitlines()):
                if line.strip():
                    try:
                        doc = json.loads(line)
                    except ValueError:
                        pass
                    break
        data = doc if isinstance(doc, dict) else None
        if data is None:
            return {"ok": False, "status": "running", "text": "", "error": "poll output is not JSON"}

        state = str(self._dig(data, self.parse.get("state") or "thread.status") or "").lower()
        text = self._reply_text(data)
        if state in self.poll_failed:
            detail = text[:300].strip()
            return {"ok": False, "status": "failed", "text": text,
                    "error": f"agent {state}" + (f"：{detail}" if detail else "")}
        if not state or state in self.poll_running:
            return {"ok": True, "status": "running", "text": text, "error": None}
        return {"ok": True, "status": "done", "text": text, "error": None}

    def _reply_text(self, data: dict) -> str:
        messages = self._dig(data, self.parse.get("reply") or "messages")
        if not isinstance(messages, list):
            return ""
        for item in reversed(messages):
            if isinstance(item, dict) and item.get("role") == "assistant":
                return str(item.get("text") or "")[-4000:]
        return ""

    def send(self, task: dict, message: str) -> dict:
        """Continue an existing session through ``send_command`` (optional).

        The message goes to the command's stdin, so it never has to survive
        shell quoting: ``t3 thread send "$HUB_SESSION" -`` reads it as-is.
        """
        if not self.send_command:
            raise NotImplementedError("this agent has no `send` command configured")
        try:
            proc = subprocess.run(
                self.send_command,
                shell=True,
                input=message,
                capture_output=True,
                text=True,
                timeout=self.send_timeout,
                cwd=self._workspace(task),
                env=self._env(task, message=message),
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "detail": f"timeout after {self.send_timeout}s"}
        except Exception as exc:  # bad cwd, missing binary, ...
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        return {
            "ok": proc.returncode == 0,
            "detail": output[-2000:] or f"exit {proc.returncode}",
            "returncode": proc.returncode,
        }


# Backwards-compatible name for existing configs and imports.
ShellAgent = CommandAgent


def register(options: dict | None = None) -> CommandAgent:
    return CommandAgent(options)