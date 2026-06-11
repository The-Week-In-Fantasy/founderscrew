import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch, AsyncMock
from founderscrew.dashboard.app import app
from founderscrew.state.models import WorkflowStatus

client = TestClient(app)

@patch("founderscrew.dashboard.app.store.list_states")
def test_dashboard_home(mock_list):
    mock_list.return_value = []
    resp = client.get("/")
    assert resp.status_code == 200
    assert "DevOps Agents Overview" in resp.text

@patch("founderscrew.dashboard.app.store.load_state")
def test_dashboard_run_detail_not_found(mock_load):
    mock_load.return_value = None
    resp = client.get("/run/session_999")
    assert resp.status_code == 404

@patch("founderscrew.dashboard.app.store.load_state")
def test_dashboard_run_detail_success(mock_load):
    from founderscrew.state.models import WorkflowStateModel, IssueContext
    issue = IssueContext(number=1, title="T", creator="c", repository="o/r")
    state = WorkflowStateModel(
        session_id="sess_123",
        issue=issue,
        status=WorkflowStatus.TRIAGE
    )
    mock_load.return_value = state
    
    resp = client.get("/run/sess_123")
    assert resp.status_code == 200
    assert "Issue #1: T" in resp.text

@patch("founderscrew.dashboard.app.store.load_state")
@patch("founderscrew.webhook.server.orchestrator.handle_comment_created", new_callable=AsyncMock)
def test_dashboard_approve_plan(mock_comment, mock_load):
    from founderscrew.state.models import WorkflowStateModel, IssueContext
    issue = IssueContext(number=42, title="T", creator="c", repository="owner/repo")
    state = WorkflowStateModel(
        session_id="owner_repo_42",
        issue=issue,
        status=WorkflowStatus.AWAIT_PLAN_APPROVAL
    )
    mock_load.return_value = state
    
    # Send approve request
    resp = client.post("/run/owner_repo_42/approve", data={"step_type": "plan"})
    
    # Asserts Redirect
    assert resp.status_code == 303 or resp.status_code == 200
    mock_comment.assert_called_once_with(
        repo_name="owner/repo",
        issue_number=42,
        comment_body="approve",
        commenter="dashboard_user"
    )

def test_dashboard_settings_get():
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Configuration Settings" in resp.text

@patch("founderscrew.config.settings.save")
def test_dashboard_settings_post(mock_save):
    resp = client.post(
        "/settings",
        data={
            "repo": "new/repo",
            "trigger_label": "crew:go",
            "preferred_tool": "cursor",
            "fallback_tool": "gemini",
            "mode": "api",
            "planning_model": "gemini/gemini-2.5-pro",
            "fast_model": "gemini/gemini-2.5-flash"
        }
    )
    assert resp.status_code == 200
    assert "Configuration saved successfully!" in resp.text
    assert mock_save.called
