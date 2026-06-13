import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from founderscrew.config import settings

logger = logging.getLogger("founderscrew.workflow_queue")


@dataclass
class WorkflowJob:
    id: str
    session_id: str
    stage: str
    payload: Dict[str, Any]
    attempts: int = 0
    max_attempts: int = 3


class WorkflowQueue:
    """Persistent queue for separating web requests from agent execution."""

    def __init__(self):
        self.backend = os.getenv("FOUNDERSCREW_STORAGE_BACKEND", "sqlite").lower()
        self.sqlite_db_path = Path(settings.get("state.db_path", Path.home() / ".founderscrew" / "state.db"))
        if self.backend == "firestore":
            self._init_firestore()
        else:
            self.backend = "sqlite"
            self.sqlite_db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_sqlite()

    def _init_sqlite(self) -> None:
        conn = sqlite3.connect(str(self.sqlite_db_path))
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_jobs (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                available_at REAL NOT NULL,
                locked_until REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_error TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_jobs_claim
            ON workflow_jobs (status, available_at, created_at)
            """
        )
        conn.commit()
        conn.close()

    def _init_firestore(self) -> None:
        try:
            from google.cloud import firestore

            project_id = settings.get("google.project_id") or os.getenv("GOOGLE_CLOUD_PROJECT")
            self.db = firestore.Client(project=project_id) if project_id else firestore.Client()
            base_collection = os.getenv("FIRESTORE_COLLECTION", "founderscrew_states")
            self.collection_name = f"{base_collection}_workflow_jobs"
        except Exception as e:
            logger.warning(f"Failed to initialize Firestore queue: {e}. Falling back to SQLite.")
            self.backend = "sqlite"
            self.sqlite_db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_sqlite()

    def enqueue(
        self,
        session_id: str,
        stage: str,
        payload: Optional[Dict[str, Any]] = None,
        max_attempts: int = 3,
    ) -> str:
        if self.backend == "firestore":
            return self._enqueue_firestore(session_id, stage, payload or {}, max_attempts)
        return self._enqueue_sqlite(session_id, stage, payload or {}, max_attempts)

    def _enqueue_sqlite(self, session_id: str, stage: str, payload: Dict[str, Any], max_attempts: int) -> str:
        now = time.time()
        conn = sqlite3.connect(str(self.sqlite_db_path))
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id FROM workflow_jobs
            WHERE session_id = ? AND stage = ? AND status IN ('pending', 'running')
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (session_id, stage),
        )
        row = cursor.fetchone()
        if row:
            conn.close()
            return row[0]

        job_id = str(uuid.uuid4())
        cursor.execute(
            """
            INSERT INTO workflow_jobs (
                id, session_id, stage, payload_json, status, attempts,
                max_attempts, available_at, locked_until, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, NULL, ?, ?)
            """,
            (job_id, session_id, stage, json.dumps(payload), max_attempts, now, now, now),
        )
        conn.commit()
        conn.close()
        return job_id

    def _enqueue_firestore(self, session_id: str, stage: str, payload: Dict[str, Any], max_attempts: int) -> str:
        now = time.time()
        existing = (
            self.db.collection(self.collection_name)
            .where("session_id", "==", session_id)
            .where("stage", "==", stage)
            .where("status", "in", ["pending", "running"])
            .limit(1)
            .stream()
        )
        for doc in existing:
            return doc.id

        job_id = str(uuid.uuid4())
        self.db.collection(self.collection_name).document(job_id).set(
            {
                "id": job_id,
                "session_id": session_id,
                "stage": stage,
                "payload": payload,
                "status": "pending",
                "attempts": 0,
                "max_attempts": max_attempts,
                "available_at": now,
                "locked_until": None,
                "created_at": now,
                "updated_at": now,
                "last_error": None,
            }
        )
        return job_id

    def claim_next(self, lease_seconds: int = 3600) -> Optional[WorkflowJob]:
        if self.backend == "firestore":
            return self._claim_next_firestore(lease_seconds)
        return self._claim_next_sqlite(lease_seconds)

    def _claim_next_sqlite(self, lease_seconds: int) -> Optional[WorkflowJob]:
        now = time.time()
        locked_until = now + lease_seconds
        conn = sqlite3.connect(str(self.sqlite_db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                """
                UPDATE workflow_jobs
                SET status = 'pending', locked_until = NULL, updated_at = ?
                WHERE status = 'running' AND locked_until IS NOT NULL AND locked_until < ?
                """,
                (now, now),
            )
            cursor.execute(
                """
                SELECT * FROM workflow_jobs
                WHERE status = 'pending' AND available_at <= ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (now,),
            )
            row = cursor.fetchone()
            if not row:
                conn.commit()
                return None
            cursor.execute(
                """
                UPDATE workflow_jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    locked_until = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (locked_until, now, row["id"]),
            )
            conn.commit()
            return WorkflowJob(
                id=row["id"],
                session_id=row["session_id"],
                stage=row["stage"],
                payload=json.loads(row["payload_json"] or "{}"),
                attempts=int(row["attempts"]) + 1,
                max_attempts=int(row["max_attempts"]),
            )
        finally:
            conn.close()

    def _claim_next_firestore(self, lease_seconds: int) -> Optional[WorkflowJob]:
        now = time.time()
        locked_until = now + lease_seconds
        stale = (
            self.db.collection(self.collection_name)
            .where("status", "==", "running")
            .where("locked_until", "<", now)
            .limit(10)
            .stream()
        )
        for doc in stale:
            doc.reference.update({"status": "pending", "locked_until": None, "updated_at": now})

        pending = (
            self.db.collection(self.collection_name)
            .where("status", "==", "pending")
            .where("available_at", "<=", now)
            .order_by("available_at")
            .order_by("created_at")
            .limit(1)
            .stream()
        )
        for doc in pending:
            data = doc.to_dict() or {}
            attempts = int(data.get("attempts") or 0) + 1
            doc.reference.update(
                {
                    "status": "running",
                    "attempts": attempts,
                    "locked_until": locked_until,
                    "updated_at": now,
                }
            )
            return WorkflowJob(
                id=doc.id,
                session_id=data["session_id"],
                stage=data["stage"],
                payload=data.get("payload") or {},
                attempts=attempts,
                max_attempts=int(data.get("max_attempts") or 3),
            )
        return None

    def complete(self, job_id: str) -> None:
        if self.backend == "firestore":
            self.db.collection(self.collection_name).document(job_id).update(
                {"status": "completed", "locked_until": None, "updated_at": time.time()}
            )
            return
        self._update_sqlite_status(job_id, "completed")

    def fail(self, job: WorkflowJob, error: str, retry_delay_seconds: int = 60) -> None:
        now = time.time()
        status = "failed" if job.attempts >= job.max_attempts else "pending"
        available_at = now if status == "failed" else now + retry_delay_seconds
        if self.backend == "firestore":
            self.db.collection(self.collection_name).document(job.id).update(
                {
                    "status": status,
                    "available_at": available_at,
                    "locked_until": None,
                    "updated_at": now,
                    "last_error": error[:2000],
                }
            )
            return

        conn = sqlite3.connect(str(self.sqlite_db_path))
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE workflow_jobs
            SET status = ?, available_at = ?, locked_until = NULL,
                updated_at = ?, last_error = ?
            WHERE id = ?
            """,
            (status, available_at, now, error[:2000], job.id),
        )
        conn.commit()
        conn.close()

    def delete_session_jobs(self, session_id: str) -> int:
        """Deletes all queue rows for a workflow session. Returns deleted count when known."""
        if self.backend == "firestore":
            deleted = 0
            docs = (
                self.db.collection(self.collection_name)
                .where("session_id", "==", session_id)
                .stream()
            )
            for doc in docs:
                doc.reference.delete()
                deleted += 1
            return deleted

        conn = sqlite3.connect(str(self.sqlite_db_path))
        cursor = conn.cursor()
        cursor.execute("DELETE FROM workflow_jobs WHERE session_id = ?", (session_id,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return int(deleted or 0)

    def _update_sqlite_status(self, job_id: str, status: str) -> None:
        conn = sqlite3.connect(str(self.sqlite_db_path))
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE workflow_jobs
            SET status = ?, locked_until = NULL, updated_at = ?
            WHERE id = ?
            """,
            (status, time.time(), job_id),
        )
        conn.commit()
        conn.close()
