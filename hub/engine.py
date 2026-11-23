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


# Kept short on purpose: a dispatched agent that re-researches or retries in a
# loop burns tokens and time.  The context a step collected is meant to be used
# as-is, questions go back to the person, and the delivery has to be checkable.
_PROMPT_RULES = (
    "执行约束：上下文已给全，按上下文直接动手，不要重新调研；"
    "缺信息就在会话里提问，不要反复试错。"
    "收尾必须给一段给人看的交付说明，按固定四行写："
    "①改了什么（文件 + 提交/分支）；②怎么验证的（命令与结果）；"
    "③没做/风险；④还需要人做什么（没有就写「无」）。"
    "不要用「正在等待…」收尾；卡在外部流程就写清楚卡在哪、谁去推进。"
)

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_SECRETISH = re.compile(r"secret|token|passw|sign|api_?key|^key$|^pat$", re.I)
# Ticket bodies often paste a curl example with a live token; never let one
# reach a chat card or an agent prompt.
_SECRET_VALUE = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9._\-]{12,}"),
    re.compile(
        r"(?i)\b(token|secret|password|passwd|pwd|access_?key)\s*[:=]\s*[^\s,;，；]{6,}"
    ),
)


def scrub_secrets(text: str) -> str:
    text = _SECRET_VALUE[0].sub(r"\1***", text)
    text = _SECRET_VALUE[1].sub("***", text)
    return _SECRET_VALUE[2].sub(r"\1：***", text)


def plain_text(value, limit: int = 1000) -> str:
    """Render a payload value for a human: no HTML tags, no newline storms.

    TAPD comment bodies arrive as HTML (``<p><b class="at-who">@…</b>…``), and
    dumping that into a card or an agent prompt is unreadable.
    """
    text = _TAG.sub(" ", str(value))
    text = html_lib.unescape(text)
    return scrub_secrets(_SPACE.sub(" ", text).strip())[:limit]


def long_text(value, limit: int = 1000) -> str:
    """Like ``plain_text`` but keeps line breaks and says when it was cut.

    Ticket bodies and agent hand-offs use lines (curl examples, the four-line
    delivery block); collapsing them into one paragraph loses structure, and
    silently truncating makes a reviewer decide on half a requirement.
    """
    text = html_lib.unescape(_TAG.sub(" ", str(value or "")))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text).strip()
    text = scrub_secrets(text)
    return text[:limit] + "\n…（已截断）" if len(text) > limit else text


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
        # A follow-up about a ticket that already has a session continues in
        # that session; only a ticket we have never talked to starts a new one.
        if stage == self.stages[0] and self._merge_into_previous(task):
            return
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
            if isinstance(step, dict) and step.get("session"):
                # A stage that continues the existing session (release after
                # review: the agent still knows the branch, build and services).
                handler = lambda t, spec=step: self._session_step(t, spec)
            elif step is not None:
                handler = lambda t, spec=step: self._run_step(t, spec)
            else:
                handler = self._default_handler(stage)
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
            for part in (
                _PROMPT_RULES,
                task.get("title"),
                # A collect step may copy a ticket body verbatim, credentials
                # included; scrub before it reaches a prompt (keep newlines).
                scrub_secrets(str(task["context"])) if task.get("context") else None,
                task.get("note"),
                self._task_digest(task, raw=True),
            )
            if part
        ).strip()

    def _task_digest(self, task: dict, *, raw: bool = False) -> str:
        """Readable summary of the hook payload, shared by cards and prompts.

        Projects may pick fields out of the payload with ``fields`` (same dotted
        paths as the source map).  With no fields configured, ``raw=True`` (the
        agent prompt) appends the payload itself, first 1500 characters; cards
        stay short instead of dumping half a TAPD payload into the chat.
        """
        payload = task.get("payload")
        lines = [
            f"来源：{task.get('source') or '?'} · 类型：{task.get('kind') or 'hook'} "
            f"· 条目：{task.get('external_id') or '?'}"
        ]
        # A comment event is about a comment; the ticket it belongs to is
        # `entity_id`.  Both matter: the comment is deduped, the ticket is
        # what the agent has to look up.
        entity = str(payload.get("entity_id") or "") if isinstance(payload, dict) else ""
        if entity and entity != str(task.get("external_id") or ""):
            lines.append(f"单据：{entity}")
        if task.get("project"):
            lines.append(f"项目：{task['project']}")
        workspace, branch = self.dispatch_target(task)
        if workspace:
            lines.append(f"派单目标：{workspace}" + (f" @ {branch}" if branch else ""))
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
            "context": result.get("context"),
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

    def _session_step(self, task: dict, spec: dict) -> dict:
        """A stage that continues the existing agent session instead of starting one.

        Used for release: the agent that wrote the code knows the branch, the
        build and which services changed, so asking a fresh run would only
        re-research.  The step sends one message and polls the same session,
        exactly like the work stage does.
        """
        agent = self.agent_for(task)
        if not task.get("session"):
            return {
                "ok": False,
                "status": "failed",
                "note": "该阶段需要已有会话，但任务没有会话（未派单）",
            }
        # Only resume polling for a message this pass actually sent: a crash
        # between entering the stage and sending would otherwise leave the
        # stage watching the previous run and advance without the message.
        if task.get("polling") and self._session_step_sent(task):
            return self._poll_work(task)
        message = str(spec.get("session") or "")
        try:
            result = agent.send(task, message)
        except NotImplementedError:
            return {
                "ok": False,
                "status": "failed",
                "note": "该阶段的 agent 没有配置 send 命令，无法通知会话",
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": "failed",
                "note": f"通知 agent 失败：{type(exc).__name__}: {exc}"[:500],
            }
        if not result.get("ok"):
            return {
                "ok": False,
                "status": "failed",
                "note": "通知 agent 失败：" + str(result.get("detail") or "")[:500],
            }
        self.store.add_event(task["id"], "session_step", task["stage"])
        interval = float(spec.get("interval") or getattr(agent, "poll_interval", 15) or 15)
        return {
            "ok": True,
            "agent": agent.name,
            "status": "pending",
            "stage": task["stage"],
            "delay": interval,
            "polling": 1,
            "poll_started_at": time.time(),
            "note": "已通知 agent 继续（等待交付）",
            "output": str(result.get("detail") or "")[-4000:],
        }

    def _session_step_sent(self, task: dict) -> bool:
        """Whether the session-stage message was already sent in this pass."""
        stage = task["stage"]
        sent = self.store.session_step_at(task["id"], stage)
        return sent > 0 and sent >= self.store.stage_changed_at(task["id"], stage)

    def _ticket_of(self, task: dict) -> str:
        payload = task.get("payload")
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("entity_id") or payload.get("id") or "")

    def _merge_into_previous(self, task: dict) -> bool:
        """Send a follow-up into the session of the ticket's earlier task.

        Every comment is its own event (deduped by comment id), so a second
        comment on the same bug used to start a second agent thread that knew
        nothing about the first.  Instead: message the existing session, and
        close this task as merged.  Returns True when the task was merged.
        """
        try:
            ticket = self._ticket_of(task)
            if not ticket:
                return False
            previous = self.store.find_ticket_task(task["source"], ticket, task["id"])
            if previous is None:
                return False
            text = (
                "工单有新变动，请继续在原会话处理，不要重新调研：\n\n"
                + f"{task.get('title') or ''}\n\n"
                + self._task_digest(task)
            )
            if not previous.get("session"):
                # Not dispatched yet (e.g. parked at the plan gate): there is no
                # thread to send to, so fold the change into the task and let a
                # single dispatch cover both events.  It goes into `context`,
                # not `note`: answering the gate clears the note, and the
                # follow-up would vanish before any agent saw it.
                if previous["status"] not in ("pending", "waiting"):
                    return False
                context = ((previous.get("context") or "") + "\n\n" + text)[:7900]
                updated = self.store.update_task(
                    previous["id"], where_status=previous["status"], context=context
                )
                if updated is None:
                    return False
                self.store.add_event(task["id"], "merged", f"并入任务 #{previous['id']}")
                self.store.update_task(
                    task["id"],
                    where_status="running",
                    status="done",
                    note=f"已并入任务 #{previous['id']}（尚未派单，合并进上下文）",
                )
                return True
            if not self.send_message(previous, text).get("ok"):
                return False  # e.g. the session is gone: run this event normally
            # The message just sent already started an agent run in that thread,
            # so the task must poll it.  A task parked at a gate (waiting)
            # would otherwise leave that run uncollected, and a `pending` one
            # would re-dispatch a new thread.  A `running` task is already
            # being watched by its worker; leave it alone.
            if previous["status"] in ("done", "failed", "pending", "waiting"):
                delay = float(getattr(self.agent_for(previous), "poll_interval", 15) or 15)
                now = time.time()
                gate = self.store.get_approval(previous["id"], previous["stage"])
                if gate is not None and gate["status"] == "open":
                    # The card on the table describes the delivery being
                    # replaced; re-entering the gate will ask afresh.
                    self.store.invalidate_approval(gate["id"])
                reopened = self.store.update_task(
                    previous["id"],
                    where_status=previous["status"],
                    status="pending",
                    stage="work" if "work" in self.stages else previous["stage"],
                    polling=1,
                    poll_started_at=now,
                    next_run_at=now + delay,
                    note=f"接到新变动，继续原会话（#{task['id']}）",
                )
                if reopened is None:
                    return False
            self.store.add_event(task["id"], "merged", f"并入任务 #{previous['id']}")
            self.store.update_task(
                task["id"],
                where_status="running",
                status="done",
                note=f"已并入任务 #{previous['id']}，变动已发往原会话",
            )
            return True
        except Exception:
            traceback.print_exc()
            return False

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
        if result.get("context"):
            fields["context"] = str(result["context"])[:8000]
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

        if approval is not None and approval["status"] != "open":
            entered = self.store.stage_entered_at(task["id"], stage)
            # Only a gate that was never re-opened can be a crash recovery: a
            # decision on a later cycle (attempt > 0) belongs to other work
            # and must be asked again.
            if (
                approval["status"] == "answered"
                and int(approval.get("attempt") or 0) == 0
                and float(approval.get("decided_at") or 0) >= entered
            ):
                # Crash recovery: the decision landed after the task entered
                # this stage, but the task was not advanced.
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
            # The previous answer/superseded gate belongs to an earlier cycle:
            # a follow-up sent this task back into a stage it had already
            # passed, so ask again instead of waving the new work through.
            reopened = self.store.reopen_approval(
                approval["id"],
                question=f"阶段「{stage}」需要确认：{task['title']}",
                detail=self._approval_detail(task, stage),
            )
            if reopened is None:
                # Another worker already re-opened or decided it: park on
                # whatever row exists instead of dropping the task.
                approval = self.store.get_approval(task["id"], stage)
            else:
                approval = reopened
                self.store.add_event(task["id"], "approval_open", f"{stage} (重新确认)")
                self._dispatch_ask(task, approval)

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
        * one send per (task_id, stage, attempt, notifier) — atomic placeholder
          row; ``attempt`` counts re-asks after a follow-up reopened the stage;
        * a failed send logs events(kind=notify_error) and retries once;
        * notification never blocks the task: the gate is already recorded and
          the board can always answer it, even if every channel is down.
        """
        url = self.config.public_base.rstrip("/") + "/a/" + approval["token"]
        title = approval["question"]
        body = self._card_body(task, approval["detail"] or task.get("title") or "")
        options = approval["options"]
        cycle = int(approval.get("attempt") or 0)
        slot = approval["stage"] if cycle == 0 else f"{approval['stage']}#{cycle}"
        for name, notifier in self.dispatch_targets(task).items():
            if not self.store.claim_notification(task["id"], slot, name):
                continue  # already sent for this cycle of the stage
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
                task["id"], slot, name, bool(result.get("ok")), result.get("detail", "")
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
            # 只说「验收标题」等于没说：复核的人要先看到单据要什么，再看
            # agent 交了什么，才知道差异在哪。
            if task.get("context"):
                parts.append("**单据要求**：\n" + long_text(task["context"], 900))
            else:  # tasks created before the context column existed
                parts.append("**单据要求**：\n" + self._task_digest(task))
            parts.append(
                "**agent 交付**：\n" + (self._delivery_summary(task) or "（没有输出记录）")
            )
            workspace, branch = self.dispatch_target(task)
            if workspace:
                parts.append(
                    f"**派单目标**：{workspace}" + (f" @ {branch}" if branch else "")
                )
            if task.get("session"):
                parts.append(f"**原会话**：`{task['session']}`（「去处理」→ 发指令）")
            parts.append(
                "**通过**：进入收尾（按项目配置回写单据/发版）。"
                "**打回**：你的意见会发回原会话，agent 重新处理。"
            )
        else:
            if task.get("context"):
                parts.append(long_text(task["context"], 1500))
            parts.append(self._task_digest(task))
        return "\n\n".join(part for part in parts if part)

    def _delivery_summary(self, task: dict, limit: int = 3000) -> str:
        """Last successful work/finish output — the "what was done" text.

        Keep the *end*: an agent puts its conclusion, verification and open
        items last, so cutting the head (after the usual "收到…" preamble) keeps
        what the reviewer needs.
        """
        runs = self.store.task_detail(task["id"]).get("runs") or []
        for run in reversed(runs):
            if run.get("stage") in ("work", "finish") and run.get("ok"):
                text = str(run.get("output") or "").strip()
                if text:
                    text = long_text(text, 10**6)
                    return "…" + text[-(limit - 1):] if len(text) > limit else text
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

    def _rework_stage(self, gate: str) -> str:
        """Where a rejected gate sends the task: the stage that delivered.

        Walk back from the gate to the nearest stage that is not itself a
        gate — that is where the rejected content was produced.  If nothing
        runs before the gate, return it unchanged so the caller leaves the
        task failed instead of bouncing into the same approval.
        """
        if gate not in self.stages:
            return gate
        for stage in reversed(self.stages[: self.stages.index(gate)]):
            if stage not in self.approve_at:
                return stage
        return gate

    def decide(self, token: str, *, index=None, text=None, by: str = ""):
        """Answer an approval through the store, advancing the task once.

        A rejection is a request for changes, not a dead end: when the task has
        an agent session, the reviewer's note goes back into that session and
        the task returns to work instead of staying failed.

        An approval that carries a note is also an instruction: with no session
        yet (the plan gate) it rides in the task context the next prompt is
        built from; once the agent exists (the review gate) it goes straight
        into the thread, like rejection feedback does.
        """
        comment = str(text or "").strip()
        gate = self.store.get_approval_by_token(token) if comment else None
        approved = gate is not None and (index in (None, "") or int(index) == 0)
        waiting = self.store.get_task(gate["task_id"]) if approved else None
        into_session = bool(waiting and waiting.get("session"))
        approval, task, changed = self.store.decide(
            token,
            index=index,
            text=text,
            by=by,
            next_stage=self.next_stage,
            carry_context=(
                f"审批意见（{gate['stage']} 阶段通过时补充，必须遵守）：\n{comment}"
                if approved and not into_session
                else ""
            ),
        )
        if changed and approval is not None and task is not None and into_session:
            try:
                self.send_message(task, f"审批通过时补充的说明，请在后续阶段遵守：\n\n{comment}")
            except Exception as exc:  # decided already: report, never 500
                traceback.print_exc()
                self.store.add_event(task["id"], "message_error", f"{type(exc).__name__}: {exc}")
        if (
            not changed
            or approval is None
            or task is None
            or approval.get("answer_index") in (None, 0)
            or task.get("status") != "failed"
            or not task.get("session")
        ):
            return approval, task, changed
        target = self._rework_stage(approval["stage"])
        if target == approval["stage"]:
            # Nothing ran before this gate (it is the pipeline's first stage):
            # there is no rework to do, so the rejection stays failed.
            return approval, task, changed
        feedback = (
            "复核打回，请按意见修改后重新交付，仍然不要重新调研：\n\n"
            + (approval.get("answer_text") or "")
        )
        if not self.send_message(task, feedback).get("ok"):
            return approval, task, changed  # session gone: keep it failed
        delay = float(getattr(self.agent_for(task), "poll_interval", 15) or 15)
        now = time.time()
        updated = self.store.update_task(
            task["id"],
            where_status="failed",
            status="pending",
            stage=target,
            polling=1,
            poll_started_at=now,
            next_run_at=now + delay,
            note="复核打回，意见已发回原会话返工",
        )
        if updated is None:
            # The task was retried/cancelled between the decision and now; the
            # agent is already working, so surface the orphaned run instead of
            # reporting a rework that nothing will poll.
            self.store.add_event(
                task["id"],
                "rework_error",
                "打回意见已发进会话，但任务状态已变化，未重新入队",
            )
            return approval, task, changed
        return approval, updated, changed