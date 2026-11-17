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

import threading
import time
import traceback
from pathlib import Path

from .adapters import load_agent, load_notifier


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
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

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

        if stage in self.approve_at:
            self._gate(task)
            return

        handler = self.handlers.get(stage) or self._default_handler(stage)
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

    def _run_agent(self, task: dict) -> dict:
        prompt = "\n\n".join(
            part for part in (task.get("title"), task.get("note")) if part
        ).strip()
        result = self.agent.start(dict(task), prompt)
        ok = bool(result.get("ok"))
        following = self.next_stage(task["stage"])
        return {
            "ok": ok,
            "agent": self.agent.name,
            "session": result.get("session"),
            "output": result.get("output") or result.get("error") or "",
            "status": "pending" if ok else "failed",
            "stage": following if ok else None,
            "note": None if ok else (result.get("error") or "agent failed"),
        }

    def _apply(self, task: dict, result: dict) -> None:
        status = result.get("status") or "pending"
        fields: dict = {"status": status}
        if result.get("stage"):
            fields["stage"] = result["stage"]
        if result.get("session") is not None:
            fields["session"] = result["session"]
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
                detail=task.get("note") or "",
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
        for name, notifier in self.notifiers.items():
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