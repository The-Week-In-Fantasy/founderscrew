import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch, AsyncMock
from founderscrew.webhook.server import router

app = FastAPI()
app.include_router(router)
client = TestClient(app)

def test_webhook_github_missing_header():
    resp = client.post("/webhook/github", json={})
    assert resp.status_code == 400
    assert "Missing X-GitHub-Event" in resp.json()["detail"]

def test_webhook_github_issues_ignored_label():
    resp = client.post(
        "/webhook/github",
        json={"action": "labeled", "label": {"name": "not-ready"}},
        headers={"X-GitHub-Event": "issues"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}

@patch("founderscrew.webhook.server.settings.get", return_value="crew:ready")
@patch("founderscrew.webhook.server.orchestrator.handle_issue_labeled")
def test_webhook_github_issues_labeled_trigger(mock_handle, mock_settings_get):
    resp = client.post(
        "/webhook/github",
        json={
            "action": "labeled",
            "label": {"name": "crew:ready"},
            "issue": {"number": 100},
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "founder-bob"}
        },
        headers={"X-GitHub-Event": "issues"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "triggered"
    assert resp.json()["session_id"] == "owner_repo_100"
    mock_handle.assert_called_once_with("owner/repo", 100, "founder-bob")

@patch("founderscrew.webhook.server.orchestrator.handle_comment_created")
def test_webhook_github_comment_created(mock_handle):
    resp = client.post(
        "/webhook/github",
        json={
            "action": "created",
            "comment": {"user": {"login": "founder-bob"}, "body": "approve"},
            "issue": {"number": 100},
            "repository": {"full_name": "owner/repo"}
        },
        headers={"X-GitHub-Event": "issue_comment"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "processing_comment"}
    mock_handle.assert_called_once_with("owner/repo", 100, "approve", "founder-bob")

def test_a2a_qa_endpoint_invalid_jsonrpc():
    resp = client.post(
        "/api/v1/a2a/qa",
        json={"jsonrpc": "1.0", "method": "execute_qa", "params": {}, "id": 1}
    )
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32600

def test_a2a_qa_endpoint_method_not_found():
    resp = client.post(
        "/api/v1/a2a/qa",
        json={"jsonrpc": "2.0", "method": "invalid_method", "params": {}, "id": 1}
    )
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32601

@patch("founderscrew.webhook.server.Runner")
@patch("founderscrew.webhook.server.get_qa_agent")
def test_a2a_qa_endpoint_success(mock_get_agent, mock_runner_class):
    mock_runner = MagicMock()
    mock_runner_class.return_value = mock_runner
    
    mock_event = MagicMock()
    mock_event.output = '```json\n{"passed": true, "similarity_percentage": 98.5, "observations": "Looks excellent"}\n```'
    mock_event.error_code = None
    
    # run_async returns an async generator
    async def mock_generator(*args, **kwargs):
        yield mock_event
        
    mock_runner.run_async.return_value = mock_generator()
    
    resp = client.post(
        "/api/v1/a2a/qa",
        json={"jsonrpc": "2.0", "method": "execute_qa", "params": {"url": "http://localhost:8000"}, "id": 100}
    )
    
    assert resp.status_code == 200
    assert resp.json()["jsonrpc"] == "2.0"
    assert resp.json()["id"] == 100
    assert resp.json()["result"]["passed"] is True
    assert resp.json()["result"]["similarity_percentage"] == 98.5
    assert resp.json()["result"]["observations"] == "Looks excellent"
