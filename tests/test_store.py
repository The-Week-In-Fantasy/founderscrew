import os
import json
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
from founderscrew.state.models import WorkflowStateModel, IssueContext, WorkflowStatus
from founderscrew.state.store import StateStore

@pytest.fixture
def temp_db_path(tmp_path):
    """Fixture to provide a temporary path for SQLite DB."""
    return tmp_path / "test_state.db"

@pytest.fixture
def dummy_state():
    """Fixture to provide a valid mock WorkflowStateModel."""
    issue = IssueContext(
        number=42,
        title="Test Issue",
        body="This is a test issue body",
        creator="test-user",
        labels=["bug", "crew:ready"],
        repository="owner/repo"
    )
    return WorkflowStateModel(
        session_id="session_123",
        issue=issue,
        status=WorkflowStatus.TRIAGE
    )

def test_sqlite_store_flow(temp_db_path, dummy_state):
    """Verifies SQLite saving, loading, and listing flows."""
    with patch.dict(os.environ, {"FOUNDERSCREW_STORAGE_BACKEND": "sqlite"}):
        with patch("founderscrew.config.settings.get", return_value=temp_db_path):
            store = StateStore()
            
            # Check empty initially
            assert store.load_state("session_123") is None
            
            # Save state
            store.save_state(dummy_state)
            
            # Load state and verify fields
            loaded = store.load_state("session_123")
            assert loaded is not None
            assert loaded.session_id == "session_123"
            assert loaded.issue.number == 42
            assert loaded.status == WorkflowStatus.TRIAGE
            
            # Update state status
            dummy_state.status = WorkflowStatus.PLANNING
            store.save_state(dummy_state)
            
            # Verify updated status
            loaded = store.load_state("session_123")
            assert loaded.status == WorkflowStatus.PLANNING
            
            # List states and check length
            states = store.list_states()
            assert len(states) == 1
            assert states[0].session_id == "session_123"

def test_sqlite_delete_prevents_stale_worker_resurrection(temp_db_path, dummy_state):
    """A deleted dashboard run must not reappear when an in-flight worker saves stale state."""
    with patch.dict(os.environ, {"FOUNDERSCREW_STORAGE_BACKEND": "sqlite"}):
        with patch("founderscrew.config.settings.get", return_value=temp_db_path):
            store = StateStore()
            store.save_state(dummy_state)
            store.delete_state(dummy_state.session_id)

            assert store.load_state(dummy_state.session_id) is None
            assert store.is_state_deleted(dummy_state.session_id) is True

            dummy_state.status = WorkflowStatus.PLANNING
            store.save_state(dummy_state)
            assert store.load_state(dummy_state.session_id) is None

            store.clear_deleted_state(dummy_state.session_id)
            store.save_state(dummy_state)
            assert store.load_state(dummy_state.session_id).status == WorkflowStatus.PLANNING

def test_firestore_store_flow(dummy_state):
    """Verifies Firestore flow using mocked Firestore client."""
    with patch.dict(os.environ, {"FOUNDERSCREW_STORAGE_BACKEND": "firestore"}):
        # Mock the firestore client and import
        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_deleted_collection = MagicMock()
        mock_doc = MagicMock()
        mock_deleted_doc = MagicMock()
        
        mock_client.collection.side_effect = lambda name: mock_deleted_collection if name.endswith("_deleted") else mock_collection
        mock_collection.document.return_value = mock_doc
        mock_deleted_collection.document.return_value = mock_deleted_doc
        
        # When doc.get() is called, return an exists=True mock document
        mock_snapshot = MagicMock()
        mock_snapshot.exists = True
        mock_snapshot.to_dict.return_value = json.loads(dummy_state.model_dump_json())
        mock_doc.get.return_value = mock_snapshot

        mock_deleted_snapshot = MagicMock()
        mock_deleted_snapshot.exists = False
        mock_deleted_doc.get.return_value = mock_deleted_snapshot
        
        # Mock stream() to return the snapshot
        mock_collection.stream.return_value = [mock_snapshot]

        with patch("google.cloud.firestore.Client", return_value=mock_client):
            store = StateStore()
            assert store.backend == "firestore"
            
            # Save state
            store.save_state(dummy_state)
            mock_collection.document.assert_called_with("session_123")
            mock_doc.set.assert_called()
            
            # Load state
            loaded = store.load_state("session_123")
            assert loaded is not None
            assert loaded.session_id == "session_123"
            mock_doc.get.assert_called_once()
            
            # List states
            states = store.list_states()
            assert len(states) == 1
            assert states[0].session_id == "session_123"
            mock_collection.stream.assert_called_once()
