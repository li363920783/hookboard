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
_SECRETISH = re.compile(r"secret|token|passw|sign|api_?key|^key$|^pat$", re.I)


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
        self._last_stale_check = 0.0

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
        # A previous process may have died while a task was `running`; nobody
        # will ever finish it, so send it back to the queue before workers run.
        self._recover_stale()
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

    def _stale_after(self) -> float:
        """How long an untouched `running` row counts as dead.

        Must exceed the longest agent run, or a slow-but-alive agent would be
        duplicated; twice the largest configured timeout is the safe default.
        """
        if self.config.stale_after:
            return float(self.config.stale_after)
        timeouts = [
            float(entry.get("timeout") or 0)
            for entry in (self.config.agents or {}).values()
            if isinstance(entry, dict)
        ]
        return max(max(timeouts + [1800.0]) * 2, 600.0)

    def _recover_stale(self) -> list[int]:
        self._last_stale_check = time.time()
        try:
            ids = self.store.requeue_stale(self._stale_after())
        except Exception:
            traceback.print_exc()
            return []
        if ids:
            print(f"recovered stale running tasks: {ids}", flush=True)
        return ids

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if time.time() - self._last_stale_check > 60:
                    self._recover_stale()
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
            return self._work
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

    def _work(self, task: dict) -> dict:
        # Async agents are started once, then only polled on later ticks.
        if task.get("polling") and task.get("session"):
            return self._poll_work(task)
        return self._run_agent(task)

    def _poll_work(self, task: dict) -> dict:
        agent = self.agent_for(task)
        result = agent.poll(task) or {}
        state = str(result.get("status") or "running")
        text = str(result.get("text") or "")
        started = float(task.get("poll_started_at") or 0) or time.time()

        if state == "running":
            limit = float(getattr(agent, "poll_max_seconds", 0) or 0)
            if limit and time.time() - started > limit:
                return {
                    "ok": False,
                    "status": "failed",
                    "note": f"等待 agent 完成超时（{int(limit)}s），已停止轮询",
                    "polling": 0,
                    "poll_started_at": None,
                }
            return {
                "ok": True,
                "status": "pending",
                "stage": task["stage"],  # stay in work, just look again
                "delay": float(getattr(agent, "poll_interval", 15) or 15),
                "polling": 1,
                "poll_started_at": started,
            }
        if state == "failed":
            return {
                "ok": False,
                "status": "failed",
                "note": result.get("error") or "agent failed",
                "output": text,
                "polling": 0,
                "poll_started_at": None,
            }

        following = self.next_stage(task["stage"])
        return {
            "ok": True,
            "agent": agent.name,
            "status": "pending" if following else "done",
            "stage": following,
            "output": text,
            "polling": 0,
            "poll_started_at": None,
            "note": "agent 已完成",
        }

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
        if status == "poll":
            # Async: the run is queued; stay in this stage and poll instead of
            # keeping a worker blocked for the whole agent run.
            return {
                "ok": True,
                "agent": agent.name,
                "session": result.get("session"),
                "link": result.get("link"),
                "output": result.get("output") or "",
                "status": "pending",
                "stage": task["stage"],
                "delay": float(result.get("poll_interval") or 15),
                "polling": 1,
                "poll_started_at": time.time(),
                "note": result.get("note") or "已派单，agent 运行中（轮询监控）",
            }
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
        if "polling" in result:
            fields["polling"] = 1 if result["polling"] else 0
        if "poll_started_at" in result:
            fields["poll_started_at"] = result["poll_started_at"]
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
            self._notify_finished(updated, "done")
        elif status == "failed":
            self.store.add_event(task["id"], "failed", fields.get("note", ""))
            self._notify_finished(updated, "failed")

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
                detail=self._approval_detail(task, stage),
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
        body = self._card_body(task, approval["detail"] or task.get("title") or "")
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

    def _card_body(self, task: dict, detail: str) -> str:
        """Approval detail; raw fields only when the project configures none.

        A TAPD payload is full of ids, proxy hosts and the webhook `secret` —
        noise on a card and one credential too many.  Projects with ``fields``
        already show what matters, and the untouched payload stays on the
        「去处理」 page.
        """
        spec = self.project_spec(task)
        payload = task.get("payload")
        if spec.get("fields") or not isinstance(payload, dict) or not payload:
            return detail
        lines = []
        for key, value in payload.items():
            if str(key).startswith("old_") or _SECRETISH.search(str(key)):
                continue
            text = plain_text(value, 200)
            if text:
                lines.append(f"{key}: {text}")
            if len(lines) >= 10:
                break
        if not lines:
            return detail
        return (detail + "\n\n**原始字段**\n" + "\n".join(lines)).strip()

    def _approval_detail(self, task: dict, stage: str) -> str:
        """What the person needs at *this* gate.

        plan  = what the ticket is about (the digest);
        review = what the agent delivered, plus the one line they must decide.
        """
        parts = [str(task.get("note"))] if task.get("note") else []
        if stage == "review":
            parts.append("**变动**：计划已通过，agent 已交付（见下）")
            parts.append(f"**要你决定**：验收「{task.get('title') or ''}」")
            parts.append(
                "**agent 交付**：\n" + (self._delivery_summary(task) or "（没有输出记录）")
            )
            workspace, branch = self.dispatch_target(task)
            if workspace:
                parts.append(
                    f"**派单目标**：{workspace}" + (f" @ {branch}" if branch else "")
                )
        else:
            parts.append(self._task_digest(task))
        return "\n\n".join(part for part in parts if part)

    def _delivery_summary(self, task: dict, limit: int = 1500) -> str:
        """Last successful work/finish output — the "what was done" text."""
        runs = self.store.task_detail(task["id"]).get("runs") or []
        for run in reversed(runs):
            if run.get("stage") in ("work", "finish") and run.get("ok"):
                text = str(run.get("output") or "").strip()
                if text:
                    return plain_text(text, limit)
        return ""

    @staticmethod
    def _elapsed_text(task: dict) -> str:
        created = float(task.get("created_at") or 0)
        if not created:
            return "—"
        seconds = max(0, int(time.time() - created))
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"

    def _finished_body(self, task: dict, kind: str) -> str:
        if kind == "done":
            parts = [
                "**变动**：复核已通过，任务结束",
                "**完成内容**：\n" + (self._delivery_summary(task) or "（没有输出记录）"),
            ]
            if task.get("note"):
                parts.append(f"**备注**：{task['note']}")
        else:
            parts = [f"**失败原因**：{task.get('note') or 'agent failed'}"]
            summary = self._delivery_summary(task)
            if summary:
                parts.append("**已有输出**：\n" + summary)
        parts.append(
            f"**阶段**：{task.get('stage')} · **耗时**：{self._elapsed_text(task)}"
        )
        return "\n\n".join(parts)

    def _notify_finished(self, task: dict, kind: str) -> None:
        """One card when the task ends (done or failed), deduped per task."""
        label = "任务完成" if kind == "done" else "任务失败"
        title = f"{label}：{task.get('title') or ''}"
        body = self._finished_body(task, kind)
        link = self.config.public_base.rstrip("/") + "/"
        # Attempt number in the key: a replayed claim is deduped, while a
        # retried task that ends again still reports a second time.
        key = f"{kind}#{task.get('attempts') or 0}"
        for name, notifier in self.dispatch_targets(task).items():
            if not self.store.claim_notification(task["id"], key, name):
                continue
            notifier.context = {
                "task_id": task["id"],
                "stage": task.get("stage"),
                "notice_kind": kind,
                "source": task.get("source"),
                "external_id": task.get("external_id"),
                "task_title": task.get("title"),
                "public_base": self.config.public_base.rstrip("/"),
            }
            try:
                result = notifier.notify(title, body, [("打开看板", link)])
                ok = bool(result.get("ok"))
                detail = str(result.get("detail") or "")
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            self.store.finish_notification(task["id"], key, name, ok, detail)
            if not ok:
                self.store.add_event(
                    task["id"], "notify_error", f"{name}: {detail}"
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