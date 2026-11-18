"""SQLite data layer — the only module in Hookboard that writes SQL.

Concurrency: every call opens its own short-lived connection (WAL, 10s busy
timeout), so worker threads and the HTTP server never share one.  Claiming a
task is a single ``UPDATE ... WHERE status='pending'``; the row count decides
the winner, so several workers can race safely.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    external_id   TEXT    NOT NULL,
    title         TEXT    NOT NULL DEFAULT '',
    lane          TEXT    NOT NULL DEFAULT '',
    project       TEXT    NOT NULL DEFAULT '',
    workspace     TEXT    NOT NULL DEFAULT '',
    branch        TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'pending',
    stage         TEXT    NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 0,
    session       TEXT,
    note          TEXT,
    context       TEXT,
    payload_json  TEXT    NOT NULL DEFAULT '{}',
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_run_at   REAL,
    polling       INTEGER NOT NULL DEFAULT 0,
    poll_started_at REAL,
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL,
    UNIQUE (source, kind, external_id)
);

CREATE TABLE IF NOT EXISTS approvals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL,
    stage         TEXT    NOT NULL,
    question      TEXT    NOT NULL,
    detail        TEXT    NOT NULL DEFAULT '',
    options_json  TEXT    NOT NULL DEFAULT '[]',
    recommend     INTEGER,
    status        TEXT    NOT NULL DEFAULT 'open',
    answer_index  INTEGER,
    answer_text   TEXT,
    "by"          TEXT    NOT NULL DEFAULT '',
    token         TEXT    NOT NULL UNIQUE,
    created_at    REAL    NOT NULL,
    decided_at    REAL,
    attempt       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (task_id, stage)
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  INTEGER NOT NULL,
    ts       REAL    NOT NULL,
    kind     TEXT    NOT NULL,
    detail   TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL,
    stage        TEXT    NOT NULL,
    agent        TEXT    NOT NULL DEFAULT '',
    started_at   REAL    NOT NULL,
    finished_at  REAL,
    ok           INTEGER NOT NULL DEFAULT 0,
    output       TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notifications (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   INTEGER NOT NULL,
    stage     TEXT    NOT NULL,
    notifier  TEXT    NOT NULL,
    ts        REAL    NOT NULL,
    ok        INTEGER,
    detail    TEXT    NOT NULL DEFAULT '',
    UNIQUE (task_id, stage, notifier)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_run ON tasks (status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_approvals_task_status ON approvals (task_id, status);
CREATE INDEX IF NOT EXISTS idx_events_task ON events (task_id);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs (task_id);
"""

UPDATE_FIELDS = {
    "status",
    "stage",
    "priority",
    "session",
    "note",
    "context",
    "next_run_at",
    "lane",
    "workspace",
    "branch",
    "polling",
    "poll_started_at",
}


def _task(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    task = dict(row)
    task["payload"] = json.loads(task.pop("payload_json") or "{}")
    return task


def _approval(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    approval = dict(row)
    approval["options"] = json.loads(approval.pop("options_json") or "[]")
    return approval


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # Migration for databases created before `project` existed.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            for name in ("project", "workspace", "branch"):
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE tasks ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            if "polling" not in columns:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN polling INTEGER NOT NULL DEFAULT 0"
                )
            if "poll_started_at" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN poll_started_at REAL")
            if "context" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN context TEXT")
            approval_columns = {row[1] for row in conn.execute("PRAGMA table_info(approvals)")}
            if "attempt" not in approval_columns:
                conn.execute(
                    "ALTER TABLE approvals ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0"
                )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            with conn:  # one transaction; commits on success, rolls back on error
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _event(conn: sqlite3.Connection, task_id: int, kind: str, detail: str = "") -> None:
        conn.execute(
            "INSERT INTO events (task_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
            (task_id, time.time(), kind, str(detail)[:4000]),
        )

    # ------------------------------------------------------------------ tasks

    def create_task(
        self,
        *,
        source: str,
        kind: str,
        external_id: str,
        title: str = "",
        stage: str,
        lane: str = "",
        priority: int = 0,
        payload: dict | None = None,
        project: str = "",
    ) -> tuple[dict, bool]:
        """Insert a task, or wake the existing one with the same unique key.

        Returns ``(task, created)``.  A redelivery never creates a second row.
        """
        now = time.time()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO tasks
                    (source, kind, external_id, title, lane, project, status, stage,
                     priority, payload_json, attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, 0, ?, ?)
                """,
                (
                    source,
                    kind,
                    external_id,
                    title,
                    lane,
                    project,
                    stage,
                    int(priority),
                    json.dumps(payload or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            created = cur.rowcount == 1
            row = conn.execute(
                "SELECT * FROM tasks WHERE source=? AND kind=? AND external_id=?",
                (source, kind, external_id),
            ).fetchone()
            task = _task(row)
            if created:
                self._event(conn, task["id"], "created", f"{source}/{kind}/{external_id}")
            elif task["status"] == "pending":
                # Duplicate delivery: wake, never create a second row.
                conn.execute(
                    "UPDATE tasks SET next_run_at=NULL, updated_at=? WHERE id=? AND status='pending'",
                    (now, task["id"]),
                )
                self._event(conn, task["id"], "redelivered", f"{source}/{kind}/{external_id}")
                task = _task(
                    conn.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
                )
        return task, created

    def get_task(self, task_id: int) -> dict | None:
        with self._conn() as conn:
            return _task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def find_task(self, source: str, kind: str, external_id: str) -> dict | None:
        with self._conn() as conn:
            return _task(
                conn.execute(
                    "SELECT * FROM tasks WHERE source=? AND kind=? AND external_id=?",
                    (source, kind, external_id),
                ).fetchone()
            )

    def list_tasks(self, status: str | None = None, stage: str | None = None, limit: int = 500):
        sql = "SELECT * FROM tasks"
        clauses, values = [], []
        if status:
            clauses.append("status=?")
            values.append(status)
        if stage:
            clauses.append("stage=?")
            values.append(stage)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority DESC, updated_at DESC, id DESC LIMIT ?"
        values.append(int(limit))
        with self._conn() as conn:
            return [_task(row) for row in conn.execute(sql, values)]

    def claim_task(self, task_id: int, now: float | None = None) -> dict | None:
        """Atomically pending -> running.  ``None`` means somebody else won."""
        now = time.time() if now is None else now
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='running', attempts=attempts+1, updated_at=? "
                "WHERE id=? AND status='pending'",
                (now, task_id),
            )
            if cur.rowcount != 1:
                return None
            return _task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def requeue_stale(self, seconds: float) -> list[int]:
        """Requeue tasks a dead worker left in ``running`` (restart, kill).

        Only rows untouched for *seconds* are touched, so a live long-running
        agent is never stolen mid-flight.
        """
        now = time.time()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, session FROM tasks WHERE status='running' AND updated_at < ?",
                (now - float(seconds),),
            ).fetchall()
            ids = [row["id"] for row in rows]
            for row in rows:
                task_id = row["id"]
                # A task that already has an agent session is resumed by
                # polling it; starting a fresh run would duplicate the work.
                resume = bool(row["session"])
                conn.execute(
                    "UPDATE tasks SET status='pending', next_run_at=NULL, polling=?, "
                    "poll_started_at=?, note=?, updated_at=? "
                    "WHERE id=? AND status='running'",
                    (
                        1 if resume else 0,
                        now if resume else None,
                        "检测到中断，继续监控已有会话"
                        if resume
                        else "疑似中断（进程重启/被杀），已自动重新入队",
                        now,
                        task_id,
                    ),
                )
                self._event(conn, task_id, "requeue", "stale running task recovered")
            return ids

    def find_ticket_task(self, source: str, ticket: str, exclude_id: int) -> dict | None:
        """Most recent task for the same ticket, cancelled ones excluded.

        A comment event's ``external_id`` is the comment, not the ticket; the
        ticket comes from the payload (``entity_id`` for comments, ``id`` for
        creates/updates).  The task may have no session yet (still waiting at
        an approval gate) — the caller decides how to merge.

        A merged follow-up is closed as a placeholder, so the newest row for a
        ticket is often a dead end; order live tasks first, then session-bearing
        ones, so a placeholder never shadows the thread the follow-up belongs in.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE source=? AND id<>? AND status<>'cancelled' "
                # cast: a numeric payload id extracts as INTEGER, and SQLite
                # never equates INTEGER with the TEXT parameter.
                "AND cast(coalesce(json_extract(payload_json,'$.entity_id'), "
                "json_extract(payload_json,'$.id')) AS TEXT)=? "
                "ORDER BY (status IN ('pending','running','waiting')) DESC, "
                "(coalesce(session,'')<>'') DESC, id DESC LIMIT 1",
                (source, int(exclude_id), str(ticket)),
            ).fetchone()
            return _task(row)

    def claim_next(self, now: float | None = None) -> dict | None:
        """Pick and claim the next due pending task, or return ``None``."""
        now = time.time() if now is None else now
        with self._conn() as conn:
            for _ in range(10):  # bounded retry if another worker wins the row
                row = conn.execute(
                    "SELECT id FROM tasks WHERE status='pending' "
                    "AND (next_run_at IS NULL OR next_run_at <= ?) "
                    "ORDER BY priority DESC, id ASC LIMIT 1",
                    (now,),
                ).fetchone()
                if row is None:
                    return None
                cur = conn.execute(
                    "UPDATE tasks SET status='running', attempts=attempts+1, updated_at=? "
                    "WHERE id=? AND status='pending'",
                    (now, row["id"]),
                )
                if cur.rowcount == 1:
                    return _task(
                        conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
                    )
        return None

    def update_task(self, task_id: int, *, where_status: str | None = None, **fields):
        """Update whitelisted columns; returns the new row or ``None`` if guarded out."""
        unknown = set(fields) - UPDATE_FIELDS
        if unknown:
            raise ValueError(f"cannot update task fields: {sorted(unknown)}")
        if not fields:
            return self.get_task(task_id)
        fields["updated_at"] = time.time()
        sql = "UPDATE tasks SET " + ", ".join(f"{name}=?" for name in fields)
        values = list(fields.values()) + [task_id]
        sql += " WHERE id=?"
        if where_status is not None:
            sql += " AND status=?"
            values.append(where_status)
        with self._conn() as conn:
            cur = conn.execute(sql, values)
            if cur.rowcount == 0:
                return None
            return _task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def wake_task(self, task_id: int) -> dict | None:
        now = time.time()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET next_run_at=NULL, updated_at=? WHERE id=? AND status='pending'",
                (now, task_id),
            )
            if cur.rowcount == 1:
                self._event(conn, task_id, "wake", "next_run_at cleared")
            return _task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def retry_task(self, task_id: int) -> dict | None:
        """Requeue a failed task at its current stage; anything else is untouched."""
        now = time.time()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='pending', next_run_at=NULL, note=NULL, polling=0, "
                "poll_started_at=NULL, updated_at=? WHERE id=? AND status='failed'",
                (now, task_id),
            )
            if cur.rowcount == 1:
                self._event(conn, task_id, "retry", "requeued after failure")
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return _task(row) if row else None

    def cancel_task(self, task_id: int) -> dict | None:
        now = time.time()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='cancelled', updated_at=? "
                "WHERE id=? AND status IN ('pending','running','waiting')",
                (now, task_id),
            )
            if cur.rowcount == 1:
                conn.execute(
                    "UPDATE approvals SET status='answered', answer_text='task cancelled', "
                    'decided_at=? WHERE task_id=? AND status=\'open\'',
                    (now, task_id),
                )
                self._event(conn, task_id, "cancelled", "")
            return _task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def pending_count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'").fetchone()[0]

    def status_counts(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
            return {row["status"]: row["n"] for row in rows}

    # -------------------------------------------------------------- approvals

    def create_approval(
        self,
        *,
        task_id: int,
        stage: str,
        question: str,
        detail: str = "",
        options: list[str] | None = None,
        recommend: int | None = None,
    ) -> tuple[dict, bool]:
        """Create one approval per (task, stage); idempotent on retry."""
        now = time.time()
        token = secrets.token_urlsafe(18)  # 24 URL-safe characters
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO approvals
                    (task_id, stage, question, detail, options_json, recommend,
                     status, token, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    task_id,
                    stage,
                    question,
                    detail,
                    json.dumps(options or [], ensure_ascii=False),
                    recommend,
                    token,
                    now,
                ),
            )
            created = cur.rowcount == 1
            row = conn.execute(
                "SELECT * FROM approvals WHERE task_id=? AND stage=?", (task_id, stage)
            ).fetchone()
        return _approval(row), created

    def get_approval(self, task_id: int, stage: str) -> dict | None:
        with self._conn() as conn:
            return _approval(
                conn.execute(
                    "SELECT * FROM approvals WHERE task_id=? AND stage=?", (task_id, stage)
                ).fetchone()
            )

    def get_approval_by_token(self, token: str) -> dict | None:
        with self._conn() as conn:
            return _approval(
                conn.execute("SELECT * FROM approvals WHERE token=?", (token,)).fetchone()
            )

    def approvals_for(self, task_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE task_id=? ORDER BY id ASC", (task_id,)
            )
            return [_approval(row) for row in rows]

    def invalidate_approval(self, approval_id: int) -> None:
        """Mark an open gate stale (a follow-up reworked the task underneath it)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE approvals SET status='superseded' WHERE id=? AND status='open'",
                (approval_id,),
            )

    def reopen_approval(
        self, approval_id: int, *, question: str, detail: str = ""
    ) -> dict | None:
        """Rotate a settled approval back to open for a new cycle of its stage.

        A follow-up can send a finished task back into a stage it already
        passed; the old decision then says nothing about the new work.  The row
        is reused (UNIQUE task_id, stage) with a fresh token and a bumped
        ``attempt`` so the card is sent again instead of being deduped away.
        """
        now = time.time()
        token = secrets.token_urlsafe(18)
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status='open', question=?, detail=?, token=?, "
                "answer_index=NULL, answer_text=NULL, \"by\"='', decided_at=NULL, "
                "created_at=?, attempt=attempt+1 WHERE id=? AND status<>'open'",
                (question, detail, token, now, approval_id),
            )
            if cur.rowcount != 1:
                return None
            return _approval(
                conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            )

    def open_approvals(self, task_ids: list[int] | None = None) -> dict[int, dict]:
        with self._conn() as conn:
            if task_ids:
                marks = ",".join("?" * len(task_ids))
                rows = conn.execute(
                    f"SELECT * FROM approvals WHERE status='open' AND task_id IN ({marks})",
                    list(task_ids),
                )
            else:
                rows = conn.execute("SELECT * FROM approvals WHERE status='open'")
            return {row["task_id"]: _approval(row) for row in rows}

    def decide(
        self,
        token: str,
        *,
        index: int | None = None,
        text: str | None = None,
        by: str = "",
        next_stage=None,
    ) -> tuple[dict | None, dict | None, bool]:
        """Answer an approval exactly once, then advance its task.

        Idempotent: an already-answered approval returns its stored result with
        ``changed=False``.  ``next_stage(stage)`` maps the gate's stage to the
        following one (``None`` means the pipeline is finished).  This keeps
        pipeline policy in the engine while all writes stay here.
        """
        now = time.time()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE token=?", (token,)).fetchone()
            if row is None:
                return None, None, False
            approval = _approval(row)
            if approval["status"] != "open":
                task = _task(
                    conn.execute(
                        "SELECT * FROM tasks WHERE id=?", (approval["task_id"],)
                    ).fetchone()
                )
                return approval, task, False

            options = approval["options"]
            if index is not None and not 0 <= int(index) < len(options):
                raise ValueError(f"index must be between 0 and {len(options) - 1}")
            approved = index is None or int(index) == 0
            answer_text = (text or "").strip() or (
                options[int(index)] if index is not None else ""
            )

            cur = conn.execute(
                "UPDATE approvals SET status='answered', answer_index=?, answer_text=?, "
                '"by"=?, decided_at=? WHERE id=? AND status=\'open\'',
                (index, answer_text, by, now, approval["id"]),
            )
            if cur.rowcount != 1:  # lost the race to a concurrent decision
                row = conn.execute(
                    "SELECT * FROM approvals WHERE id=?", (approval["id"],)
                ).fetchone()
                task = _task(
                    conn.execute(
                        "SELECT * FROM tasks WHERE id=?", (row["task_id"],)
                    ).fetchone()
                )
                return _approval(row), task, False

            approval = _approval(
                conn.execute("SELECT * FROM approvals WHERE id=?", (approval["id"],)).fetchone()
            )
            task = _task(
                conn.execute("SELECT * FROM tasks WHERE id=?", (approval["task_id"],)).fetchone()
            )
            if task["status"] == "waiting":
                if approved:
                    following = next_stage(approval["stage"]) if next_stage else None
                    if following:
                        conn.execute(
                            "UPDATE tasks SET status='pending', stage=?, next_run_at=NULL, "
                            "note=NULL, updated_at=? WHERE id=? AND status='waiting'",
                            (following, now, task["id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE tasks SET status='done', updated_at=? "
                            "WHERE id=? AND status='waiting'",
                            (now, task["id"]),
                        )
                    self._event(
                        conn,
                        task["id"],
                        "approved",
                        f"{approval['stage']} -> {following or 'done'} ({answer_text})",
                    )
                    if not following:
                        self._event(conn, task["id"], "done", "approval")
                else:
                    conn.execute(
                        "UPDATE tasks SET status='failed', note=?, updated_at=? "
                        "WHERE id=? AND status='waiting'",
                        (answer_text or "打回", now, task["id"]),
                    )
                    self._event(conn, task["id"], "rejected", answer_text)
                task = _task(
                    conn.execute(
                        "SELECT * FROM tasks WHERE id=?", (task["id"],)
                    ).fetchone()
                )
            return approval, task, True

    # --------------------------------------------------------- notifications

    def claim_notification(self, task_id: int, stage: str, notifier: str) -> bool:
        """Atomically reserve the single send for (task, stage, notifier).

        Returns True only for the caller that inserted the placeholder row, so
        a stage re-run or a second worker never sends a duplicate.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO notifications (task_id, stage, notifier, ts) "
                "VALUES (?, ?, ?, ?)",
                (task_id, stage, notifier, time.time()),
            )
            return cur.rowcount == 1

    def finish_notification(
        self, task_id: int, stage: str, notifier: str, ok: bool, detail: str
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE notifications SET ok=?, detail=? "
                "WHERE task_id=? AND stage=? AND notifier=?",
                (1 if ok else 0, str(detail)[:2000], task_id, stage, notifier),
            )

    # ---------------------------------------------------------- audit trails

    def add_event(self, task_id: int, kind: str, detail: str = "") -> None:
        with self._conn() as conn:
            self._event(conn, task_id, kind, detail)

    def add_run(
        self,
        task_id: int,
        stage: str,
        agent: str,
        started_at: float,
        finished_at: float,
        ok: bool,
        output: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO runs (task_id, stage, agent, started_at, finished_at, ok, output) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    stage,
                    agent,
                    started_at,
                    finished_at,
                    1 if ok else 0,
                    # Keep the tail: an agent's conclusion and verification are
                    # at the end of its reply, and the card shows this text.
                    ("…" + output[-3999:]) if len(output) > 4000 else output,
                ),
            )

    def stage_entered_at(self, task_id: int, stage: str) -> float:
        """When the task last entered ``stage`` (0 when it never logged one)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(ts) FROM events WHERE task_id=? AND kind='stage' AND detail=?",
                (task_id, stage),
            ).fetchone()
            return float(row[0] or 0)

    def events_for(self, task_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY id ASC", (task_id,)
            )
            return [dict(row) for row in rows]

    def runs_for(self, task_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE task_id=? ORDER BY id ASC", (task_id,)
            )
            return [dict(row) for row in rows]

    def task_detail(self, task_id: int) -> dict | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        return {
            "task": task,
            "approvals": self.approvals_for(task_id),
            "events": self.events_for(task_id),
            "runs": self.runs_for(task_id),
        }