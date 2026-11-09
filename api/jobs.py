"""Persistent background ingestion queue.

One job == one PDF. Jobs live in SQLite (on a volume) so they survive API
restarts; a single worker thread drains the queue one job at a time (the
local LLM is the bottleneck, so parallelism wouldn't help). Each job
checkpoints after every chunk - including the running location/scene hints
- so a restart resumes mid-document instead of starting over.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    mtime REAL NOT NULL,
    document_type TEXT NOT NULL,
    detected_type TEXT,
    game_system TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|error|cancelled
    chunks_done INTEGER NOT NULL DEFAULT 0,
    chunks_total INTEGER,
    chunk_errors INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    hints TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
"""


class JobStore:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            try:  # DBs created before auto-detection existed
                c.execute("ALTER TABLE jobs ADD COLUMN detected_type TEXT")
            except sqlite3.OperationalError:
                pass
            # a crash mid-job leaves 'running' rows; put them back in the queue
            c.execute("UPDATE jobs SET status='queued' WHERE status='running'")

    def _conn(self):
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def enqueue(self, path: str, document_type: str, game_system: str, force: bool = False) -> Optional[int]:
        """Queue a PDF. Skips it (returns None) if it's already queued/running,
        or already done at the same mtime and `force` is false."""
        mtime = Path(path).stat().st_mtime
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT status, mtime, document_type, game_system FROM jobs WHERE path=? ORDER BY id DESC LIMIT 1",
                (path,),
            ).fetchone()
            if row and row["status"] in ("queued", "running"):
                return None
            if (row and not force and row["status"] == "done" and row["mtime"] == mtime
                    and row["document_type"] == document_type and row["game_system"] == game_system):
                return None
            now = time.time()
            cur = c.execute(
                "INSERT INTO jobs (path, mtime, document_type, game_system, created, updated) VALUES (?,?,?,?,?,?)",
                (path, mtime, document_type, game_system, now, now),
            )
            return cur.lastrowid

    def list(self, status: Optional[str] = None, limit: int = 200) -> list:
        q, args = "SELECT * FROM jobs", []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args)]

    def get(self, job_id: int) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(r) if r else None

    def update(self, job_id: int, **fields) -> None:
        fields["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def cancel(self, job_id: int) -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE jobs SET status='cancelled', updated=? WHERE id=? AND status IN ('queued','running')",
                (time.time(), job_id),
            )
            return cur.rowcount > 0

    def retry(self, job_id: int) -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE jobs SET status='queued', updated=? WHERE id=? AND status IN ('error','cancelled')",
                (time.time(), job_id),
            )
            return cur.rowcount > 0

    def claim_next(self) -> Optional[dict]:
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not r:
                return None
            c.execute("UPDATE jobs SET status='running', updated=? WHERE id=?", (time.time(), r["id"]))
            return dict(r)


class Worker(threading.Thread):
    """`process(job, store)` runs one job to completion, checkpointing via
    store.update and polling store.get(id)['status'] for cancellation."""

    def __init__(self, store: JobStore, process: Callable[[dict, JobStore], None]):
        super().__init__(daemon=True, name="ingest-worker")
        self.store, self.process = store, process

    def run(self):
        while True:
            job = self.store.claim_next()
            if not job:
                time.sleep(2)
                continue
            try:
                self.process(job, self.store)
                if self.store.get(job["id"])["status"] == "running":
                    self.store.update(job["id"], status="done")
            except Exception as e:  # job-level failure (unreadable PDF, etc.)
                self.store.update(job["id"], status="error", last_error=str(e)[:2000])
