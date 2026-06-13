from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
import sqlite3

import pytest

from founderscrew.worker import run_worker_once
from founderscrew.workflow_queue import WorkflowQueue


def test_sqlite_workflow_queue_claims_and_completes(tmp_path, monkeypatch):
    db_path = tmp_path / "queue.db"
    monkeypatch.setattr("founderscrew.workflow_queue.settings.get", lambda key, default=None: db_path)
    monkeypatch.setenv("FOUNDERSCREW_STORAGE_BACKEND", "sqlite")

    queue = WorkflowQueue()
    job_id = queue.enqueue("owner_repo_1", "triage", {"source": "test"})

    claimed = queue.claim_next()
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.session_id == "owner_repo_1"
    assert claimed.stage == "triage"
    assert claimed.payload == {"source": "test"}

    queue.complete(claimed.id)
    assert queue.claim_next() is None


def test_sqlite_workflow_queue_deletes_session_jobs(tmp_path, monkeypatch):
    db_path = tmp_path / "queue.db"
    monkeypatch.setattr("founderscrew.workflow_queue.settings.get", lambda key, default=None: db_path)
    monkeypatch.setenv("FOUNDERSCREW_STORAGE_BACKEND", "sqlite")

    queue = WorkflowQueue()
    queue.enqueue("session_1", "triage")
    queue.enqueue("session_1", "planning")
    queue.enqueue("session_2", "triage")

    assert queue.delete_session_jobs("session_1") == 2
    claimed = queue.claim_next()
    assert claimed is not None
    assert claimed.session_id == "session_2"


@pytest.mark.anyio
async def test_worker_runs_claimed_job(tmp_path, monkeypatch):
    db_path = tmp_path / "queue.db"
    monkeypatch.setattr("founderscrew.workflow_queue.settings.get", lambda key, default=None: db_path)
    monkeypatch.setenv("FOUNDERSCREW_STORAGE_BACKEND", "sqlite")

    queue = WorkflowQueue()
    queue.enqueue("owner_repo_2", "planning")
    monkeypatch.setattr("founderscrew.worker.settings.get", lambda key, default=None: "")

    orchestrator = MagicMock()
    orchestrator.run_queued_stage = AsyncMock()
    ran = await run_worker_once(orchestrator=orchestrator, queue=queue)

    assert ran is True
    orchestrator.run_queued_stage.assert_awaited_once_with("owner_repo_2", "planning", {})
    assert queue.claim_next() is None


@pytest.mark.anyio
async def test_worker_skips_job_for_different_configured_repo(tmp_path, monkeypatch):
    db_path = tmp_path / "queue.db"
    monkeypatch.setattr("founderscrew.workflow_queue.settings.get", lambda key, default=None: db_path)
    monkeypatch.setenv("FOUNDERSCREW_STORAGE_BACKEND", "sqlite")

    queue = WorkflowQueue()
    queue.enqueue("fake_repo_1", "triage")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM workflow_jobs WHERE status = 'pending'").fetchone()[0] == 1
    monkeypatch.setattr(
        "founderscrew.worker.settings.get",
        lambda key, default=None: "real/repo" if key == "github.repository" else default,
    )

    orchestrator = MagicMock()
    orchestrator.run_queued_stage = AsyncMock()
    orchestrator.store.load_state.return_value = SimpleNamespace(
        issue=SimpleNamespace(repository="fake/repo")
    )
    ran = await run_worker_once(orchestrator=orchestrator, queue=queue)

    assert ran is True
    orchestrator.run_queued_stage.assert_not_awaited()
    assert queue.claim_next() is None
