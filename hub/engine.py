"""The state machine and its worker threads.

Rules enforced here (and only here):

* Nodes never write the database.  A node returns ``{status, stage, delay,
  note, ...}`` and the engine persists it.
* Entering a stage listed in ``pipeline.approve_at`` opens exactly one
  approval and parks the task as ``waiting``.  Answering it advances the task.
* A task can only move out of ``running`` (claimed) or ``waiting`` through the
  guarded updates in :mod:`hub.store`, so a cancel or a concurrent decision
  always wins over a late worker.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import threading
import time
import traceback
from pathlib import Path

from .adapters import load_agent, load_notifier
from .adapters.generic_source import dig_any


_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def plain_text(value, limit: int = 1000) -> str:
    """Render a payload value for a human: no HTML tags, no newline storms.

    TAPD comment bodies arrive as HTML (``<p><b class="at-who">@…</b>…``), and
    dumping that into a card or an agent prompt is unreadable.
    """
    text = _TAG.sub(" ", str(value))
    text = html_lib.unescape(text)
    return _SPACE.sub(" ", text).strip()[:limit]


class Engine:
    def __init__(self, store, config, *, agent=None, notifiers=None, handlers=None):
        self.store = store
        self.config = config
        self.stages = list(config.stages)
        self.approve_at = set(config.approve_at)
        self.agent = agent if agent is not None else load_agent(config.agents.get("default"))
        outbox_dir = str(Path(store.path).parent / "outbox")
        if notifiers is not None:
            self.notifiers = dict(notifiers)
        else:
            self.notifiers = {
                name: load_notifier(name, entry, outbox_dir)
                for name, entry in (config.notifiers or {}).items()
            }
        self.handlers = dict(handlers or {})
        self.projects = dict(getattr(config, "projects", None) or {})
        self._agents: dict = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -------------------------------------------------------------- projects

    def project_spec(self, task: dict) -> dict:
        """Per-project overrides; an unset/unknown project means global defaults."""
        name = task.get("project") or ("default" if "default" in self.projects else "")
        return self.projects.get(name) or {}

    def approve_stages(self, task: dict) -> set:
        spec = self.project_spec(task)
        # An explicit empty list means "this project has no gates" — only a
        # missing key falls back to the global pipeline.
        if spec.get("approve_at") is not None:
            return set(spec["approve_at"])
        return set(self.approve_at)

    def agent_for(self, task: dict):
        name = self.project_spec(task).get("agent")
        if not name:
            return self.agent
        if name not in self._agents:
            entry = (self.config.agents or {}).get(str(name))
            if entry is None:
                raise ValueError(f"project names unknown agent {name!r}")
            self._agents[str(name)] = load_agent(entry)
        return self._agents[str(name)]

    def dispatch_targets(self, task: dict) -> dict:
        """Notifiers this project uses; no list means every configured one."""
        spec = self.project_spec(task)
        if spec.get("notifiers") is None:
            return dict(self.notifiers)
        return {name: self.notifiers[name] for name in spec["notifiers"] if name in self.notifiers}

    # --------------------------------------------------------- pipeline view

    def next_stage(self, stage: str) -> str | None:
        try:
            index = self.stages.index(stage)
        except ValueError:
            return None
        return self.stages[index + 1] if index + 1 < len(self.stages) else None

    def start(self) -> None:
        for index in range(max(1, self.config.workers)):
            thread = threading.Thread(
                target=self._loop, name=f"hub-worker-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.step():
                    self._stop.wait(self.config.poll_interval)
            except Exception:
                traceback.print_exc()
                self._stop.wait(1.0)

    # ------------------------------------------------------------- one tick

    def step(self, now: float | None = None) -> bool:
        """Claim and process one task.  Returns False when the queue is empty."""
        task = self.store.claim_next(now)
        if task is None:
            return False
        self.process(task)
        return True

    def process(self, task: dict) -> None:
        if task.get("status") != "running":
            return
        stage = task["stage"]
        self.store.add_event(task["id"], "stage", stage)

        if stage in self.approve_stages(task):
            self._gate(task)
            return

        handler = self.handlers.get(stage)
        if handler is None:
            # A project may hang its own command off a stage (release, update
            # the ticket system, ...).  Gate stages never reach here: the
            # approval branch above returns first.
            step = (self.project_spec(task).get("steps") or {}).get(stage)
            handler = (
                (lambda t, spec=step: self._run_step(t, spec))
                if step is not None
                else self._default_handler(stage)
            )
        started = time.time()
        try:
            result = handler(task) or {}
        except Exception as exc:
            traceback.print_exc()
            result = {"status": "failed", "note": f"{type(exc).__name__}: {exc}"}

        if result.get("agent") is not None or result.get("output") is not None:
            self.store.add_run(
                task["id"],
                stage,
                str(result.get("agent") or ""),
                started,
                time.time(),
                bool(result.get("ok")),
                str(result.get("output") or ""),
            )
        self._apply(task, result)

    def _default_handler(self, stage: str):
        if stage == self.stages[-1]:
            return lambda task: {"status": "done"}
        if stage == "work":
            return self._run_agent
        following = self.next_stage(stage)
        return lambda task: {"status": "pending", "stage": following}

    def _prompt(self, task: dict) -> str:
        """What an agent (or a stage command) is told about the task."""
        return "\n\n".join(
            part
            for part in (task.get("title"), task.get("note"), self._task_digest(task, raw=True))
            if part
        ).strip()

    def _task_digest(self, task: dict, *, raw: bool = False) -> str:
        """Readable summary of the hook payload, shared by cards and prompts.

        Projects may pick fields out of the payload with ``fields`` (same dotted
        paths as the source map).  With no fields configured, ``raw=True`` (the
        agent prompt) appends the payload itself, first 1500 characters; cards
        stay short instead of dumping half a TAPD payload into the chat.
        """
        lines = [
            f"来源：{task.get('source') or '?'} · 类型：{task.get('kind') or 'hook'} "
            f"· 条目：{task.get('external_id') or '?'}"
        ]
        if task.get("project"):
            lines.append(f"项目：{task['project']}")
        workspace, branch = self.dispatch_target(task)
        if workspace:
            lines.append(f"派单目标：{workspace}" + (f" @ {branch}" if branch else ""))
        payload = task.get("payload")
        if isinstance(payload, dict) and payload:
            fields = self.project_spec(task).get("fields") or {}
            if fields:
                for label, path in fields.items():
                    value = dig_any(payload, path)
                    if value not in (None, ""):
                        lines.append(f"**{label}**：{plain_text(value)}")
            elif raw:
                blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                lines.append(f"原始内容：{blob[:1500]}")
        return "\n".join(lines)

    def dispatch_target(self, task: dict) -> tuple[str, str]:
        """(workspace, branch) for this task: the task's own choice wins.

        A routing step may pick a repository and a branch per task; otherwise
        the project config supplies them.
        """
        spec = self.project_spec(task)
        return (
            str(task.get("workspace") or spec.get("workspace") or ""),
            str(task.get("branch") or spec.get("branch") or ""),
        )

    def _run_agent(self, task: dict, agent=None) -> dict:
        prompt = self._prompt(task)
        agent = agent or self.agent_for(task)
        payload = dict(task)
        workspace, branch = self.dispatch_target(task)
        if workspace:
            payload["workspace"] = workspace
        if branch:
            payload["branch"] = branch
        result = agent.start(payload, prompt)
        ok = bool(result.get("ok"))
        # The adapter may choose the outcome (e.g. exit_codes in config); only
        # `pending` advances the pipeline.
        status = result.get("status") or ("pending" if ok else "failed")
        following = self.next_stage(task["stage"])
        if status == "pending" and following is None:
            status = "done"
        note = result.get("note")
        if not note and status not in ("pending", "done"):
            note = result.get("error") or "agent failed"
        return {
            "ok": ok,
            "agent": agent.name,
            "session": result.get("session"),
            "output": result.get("output") or result.get("error") or "",
            "status": status,
            "stage": following if status == "pending" else None,
            "workspace": result.get("workspace"),
            "branch": result.get("branch"),
            "note": note,
        }

    def _run_step(self, task: dict, spec) -> dict:
        """Run a project's per-stage command (``steps``) through CommandAgent.

        Same env contract as an agent (``HUB_*``), so a step can read
        ``HUB_EXTERNAL_ID`` / ``HUB_PAYLOAD`` and call the ticketing system.
        """
        from .adapters.shell_agent import CommandAgent

        options = {"command": spec} if isinstance(spec, str) else dict(spec)
        options.setdefault("timeout", 600)
        result = self._run_agent(task, agent=CommandAgent(options))
        result["agent"] = f"step:{task['stage']}"
        return result

    def send_message(self, task: dict, text: str) -> dict:
        """Relay a human instruction into the task's running agent session."""
        if not task.get("session"):
            raise ValueError("任务还没有会话（未派单），无法发送指令")
        agent = self.agent_for(task)
        result = agent.send(task, text)
        ok = bool(result.get("ok"))
        detail = str(result.get("detail") or "")
        self.store.add_event(task["id"], "message" if ok else "message_error", text[:2000])
        return {"ok": ok, "detail": detail}

    def _apply(self, task: dict, result: dict) -> None:
        status = result.get("status") or "pending"
        fields: dict = {"status": status}
        if result.get("stage"):
            fields["stage"] = result["stage"]
        if result.get("session") is not None:
            fields["session"] = result["session"]
        for key in ("workspace", "branch"):
            if result.get(key):
                fields[key] = str(result[key])
        if result.get("note"):
            fields["note"] = str(result["note"])[:2000]
        if result.get("delay"):
            fields["next_run_at"] = time.time() + float(result["delay"])
        elif status == "pending":
            fields["next_run_at"] = None

        updated = self.store.update_task(task["id"], where_status="running", **fields)
        if updated is None:  # cancelled (or decided) underneath us
            return
        if status == "done":
            self.store.add_event(task["id"], "done", updated["stage"])
        elif status == "failed":
            self.store.add_event(task["id"], "failed", fields.get("note", ""))

    # ---------------------------------------------------------------- gates

    def _gate(self, task: dict) -> None:
        stage = task["stage"]
        approval = self.store.get_approval(task["id"], stage)

        if approval is not None and approval["status"] == "answered":
            # Crash recovery: the decision landed but the task was not advanced.
            following = self.next_stage(stage)
            self.store.update_task(
                task["id"],
                where_status="running",
                status="pending" if following else "done",
                stage=following or stage,
                next_run_at=None,
            )
            self.store.add_event(task["id"], "approved", f"{stage} (recovered)")
            return

        if approval is None:
            approval, created = self.store.create_approval(
                task_id=task["id"],
                stage=stage,
                question=f"阶段「{stage}」需要确认：{task['title']}",
                detail="\n\n".join(
                    part for part in (task.get("note"), self._task_digest(task)) if part
                ),
                options=["通过", "打回"],
                recommend=0,
            )
            if created:
                self.store.add_event(task["id"], "approval_open", stage)
                self._dispatch_ask(task, approval)

        self.store.update_task(
            task["id"],
            where_status="running",
            status="waiting",
            note=f"等待审批：{stage}",
        )

    def _dispatch_ask(self, task: dict, approval: dict) -> None:
        """Send the approval link through every configured channel.

        Hard constraints from docs/CHANNELS.md:
        * one send per (task_id, stage, notifier) — atomic placeholder row;
        * a failed send logs events(kind=notify_error) and retries once;
        * notification never blocks the task: the gate is already recorded and
          the board can always answer it, even if every channel is down.
        """
        url = self.config.public_base.rstrip("/") + "/a/" + approval["token"]
        title = approval["question"]
        body = approval["detail"] or task.get("title") or ""
        options = approval["options"]
        for name, notifier in self.dispatch_targets(task).items():
            if not self.store.claim_notification(task["id"], approval["stage"], name):
                continue  # already sent for this (task, stage, channel)
            notifier.context = {
                "task_id": task["id"],
                "stage": approval["stage"],
                "source": task.get("source"),
                "external_id": task.get("external_id"),
                "task_title": task.get("title"),
                "question": approval["question"],
                "token": approval["token"],
                "public_base": self.config.public_base.rstrip("/"),
            }
            result = self._try_ask(notifier, name, task["id"], title, body, url, options)
            self.store.finish_notification(
                task["id"], approval["stage"], name, bool(result.get("ok")), result.get("detail", "")
            )

    def _try_ask(self, notifier, name, task_id, title, body, url, options) -> dict:
        result = self._call_ask(notifier, title, body, url, options)
        if not result.get("ok"):
            self.store.add_event(task_id, "notify_error", f"{name}: {result.get('detail', '')}")
            result = self._call_ask(notifier, title, body, url, options)  # retry once
            if not result.get("ok"):
                self.store.add_event(task_id, "notify_error", f"{name}: {result.get('detail', '')}")
        return result

    @staticmethod
    def _call_ask(notifier, title, body, url, options) -> dict:
        try:
            result = notifier.ask(title, body, url, options)
            return result if isinstance(result, dict) else {"ok": bool(result), "detail": ""}
        except Exception as exc:
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------- decisions

    def decide(self, token: str, *, index=None, text=None, by: str = ""):
        """Answer an approval through the store, advancing the task once."""
        return self.store.decide(
            token,
            index=index,
            text=text,
            by=by,
            next_stage=self.next_stage,
        )