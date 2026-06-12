import os
import json
import sqlite3
import logging
from pathlib import Path
from typing import Optional, List
from founderscrew.config import settings
from founderscrew.state.models import WorkflowStateModel

logger = logging.getLogger("founderscrew.store")

class StateStore:
    """Manages persistence of WorkflowStateModel. Supports SQLite for local dev and Firestore for Cloud Run."""
    
    def __init__(self):
        self.backend = os.getenv("FOUNDERSCREW_STORAGE_BACKEND", "sqlite").lower()
        self.sqlite_db_path = Path(settings.get("state.db_path", Path.home() / ".founderscrew" / "state.db"))
        
        if self.backend == "sqlite":
            self.sqlite_db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_sqlite()
        elif self.backend == "firestore":
            self._init_firestore()

    def _init_sqlite(self):
        """Initializes SQLite database schema."""
        conn = sqlite3.connect(str(self.sqlite_db_path))
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_states (
                session_id TEXT PRIMARY KEY,
                issue_number INTEGER,
                status TEXT,
                state_json TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS repo_memory (
                repo_name TEXT PRIMARY KEY,
                record_json TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
        conn.close()

    def _init_firestore(self):
        """Initializes Firestore client. Falls back to SQLite if fails."""
        try:
            from google.cloud import firestore
            project_id = settings.get("google.project_id") or os.getenv("GOOGLE_CLOUD_PROJECT")
            if project_id:
                self.db = firestore.Client(project=project_id)
            else:
                self.db = firestore.Client()
            self.collection_name = os.getenv("FIRESTORE_COLLECTION", "founderscrew_states")
        except Exception as e:
            logger.warning(f"Failed to initialize Firestore: {e}. Falling back to SQLite.")
            self.backend = "sqlite"
            self.sqlite_db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_sqlite()

    def save_state(self, state: WorkflowStateModel) -> None:
        """Saves a state to the active backend."""
        if self.backend == "firestore":
            try:
                doc_ref = self.db.collection(self.collection_name).document(state.session_id)
                doc_ref.set(json.loads(state.model_dump_json()))
                return
            except Exception as e:
                logger.error(f"Error saving to Firestore: {e}. Attempting SQLite fallback.")
                self._save_sqlite(state)
        else:
            self._save_sqlite(state)

    def _save_sqlite(self, state: WorkflowStateModel) -> None:
        """Saves state to local SQLite database."""
        conn = sqlite3.connect(str(self.sqlite_db_path))
        cursor = conn.cursor()
        state_json = state.model_dump_json()
        cursor.execute(
            """
            INSERT INTO workflow_states (session_id, issue_number, status, state_json, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(session_id) DO UPDATE SET
                status=excluded.status,
                state_json=excluded.state_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            (state.session_id, state.issue.number, state.status.value, state_json)
        )
        conn.commit()
        conn.close()

    def load_state(self, session_id: str) -> Optional[WorkflowStateModel]:
        """Loads state by session_id from the active backend."""
        if self.backend == "firestore":
            try:
                doc_ref = self.db.collection(self.collection_name).document(session_id)
                doc = doc_ref.get()
                if doc.exists:
                    return WorkflowStateModel.model_validate(doc.to_dict())
                return self._load_sqlite(session_id)
            except Exception as e:
                logger.error(f"Error loading from Firestore: {e}. Attempting SQLite fallback.")
                return self._load_sqlite(session_id)
        else:
            return self._load_sqlite(session_id)

    def _load_sqlite(self, session_id: str) -> Optional[WorkflowStateModel]:
        """Loads state from SQLite database."""
        conn = sqlite3.connect(str(self.sqlite_db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT state_json FROM workflow_states WHERE session_id = ?",
            (session_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return WorkflowStateModel.model_validate_json(row[0])
        return None

    def list_states(self) -> List[WorkflowStateModel]:
        """Returns a list of all active states sorted by date."""
        if self.backend == "firestore":
            try:
                states = []
                docs = self.db.collection(self.collection_name).stream()
                for doc in docs:
                    states.append(WorkflowStateModel.model_validate(doc.to_dict()))
                return states
            except Exception as e:
                logger.error(f"Error listing from Firestore: {e}. Falling back to SQLite.")
        
        conn = sqlite3.connect(str(self.sqlite_db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT state_json FROM workflow_states ORDER BY updated_at DESC")
        rows = cursor.fetchall()
        conn.close()
        return [WorkflowStateModel.model_validate_json(row[0]) for row in rows]

    def save_repo_memory(self, repo_name: str, record: dict) -> None:
        """Saves a repository's memory record (profile + lessons) to the active backend."""
        if self.backend == "firestore":
            try:
                doc_id = repo_name.replace("/", "__")
                self.db.collection(f"{self.collection_name}_repo_memory").document(doc_id).set(record)
                return
            except Exception as e:
                logger.error(f"Error saving repo memory to Firestore: {e}. Attempting SQLite fallback.")

        conn = sqlite3.connect(str(self.sqlite_db_path))
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO repo_memory (repo_name, record_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(repo_name) DO UPDATE SET
                record_json=excluded.record_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            (repo_name, json.dumps(record))
        )
        conn.commit()
        conn.close()

    def load_repo_memory(self, repo_name: str) -> dict:
        """Loads a repository's memory record. Returns {} when none exists."""
        if self.backend == "firestore":
            try:
                doc_id = repo_name.replace("/", "__")
                doc = self.db.collection(f"{self.collection_name}_repo_memory").document(doc_id).get()
                if doc.exists:
                    return doc.to_dict() or {}
            except Exception as e:
                logger.error(f"Error loading repo memory from Firestore: {e}. Attempting SQLite fallback.")

        conn = sqlite3.connect(str(self.sqlite_db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT record_json FROM repo_memory WHERE repo_name = ?", (repo_name,))
        row = cursor.fetchone()
        conn.close()
        if row:
            try:
                return json.loads(row[0]) or {}
            except Exception:
                return {}
        return {}

    def delete_state(self, session_id: str):
        """Deletes a state by session_id."""
        if self.backend == "firestore":
            try:
                self.db.collection(self.collection_name).document(session_id).delete()
                return
            except Exception as e:
                logger.error(f"Error deleting from Firestore: {e}. Falling back to SQLite.")
                
        conn = sqlite3.connect(str(self.sqlite_db_path))
        cursor = conn.cursor()
        cursor.execute("DELETE FROM workflow_states WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()

