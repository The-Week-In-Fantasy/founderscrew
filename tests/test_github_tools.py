import pytest
from unittest.mock import MagicMock, patch
from founderscrew.tools.github_tools import (
    github_get_issue,
    github_list_repo_files,
    github_get_file_content,
    github_search_code,
    github_create_branch,
    github_commit_files,
    github_create_pr,
    github_add_comment,
    github_merge_pr
)

@pytest.fixture
def mock_github():
    with patch("founderscrew.tools.github_tools.get_github_client") as mock_client_getter:
        mock_client = MagicMock()
        mock_client_getter.return_value = mock_client
        yield mock_client

def test_github_get_issue(mock_github):
    mock_repo = MagicMock()
    mock_issue = MagicMock()
    mock_comment = MagicMock()
    
    mock_github.get_repo.return_value = mock_repo
    mock_repo.get_issue.return_value = mock_issue
    mock_issue.get_comments.return_value = [mock_comment]
    
    # Configure issue details
    mock_issue.number = 100
    mock_issue.title = "Mock Issue"
    mock_issue.body = "Body content"
    mock_issue.user.login = "coder"
    mock_label = MagicMock()
    mock_label.name = "bug"
    mock_issue.labels = [mock_label]
    mock_issue.state = "open"
    import datetime
    mock_issue.created_at = datetime.datetime(2026, 1, 1)
    
    # Configure comment details
    mock_comment.id = 12345
    mock_comment.user.login = "reviewer"
    mock_comment.body = "LGTM"
    mock_comment.created_at = datetime.datetime(2026, 1, 2)
    
    res = github_get_issue("owner/repo", 100)
    
    assert res["number"] == 100
    assert res["title"] == "Mock Issue"
    assert res["creator"] == "coder"
    assert res["labels"] == ["bug"]
    assert len(res["comments"]) == 1
    assert res["comments"][0]["body"] == "LGTM"
    mock_github.get_repo.assert_called_with("owner/repo")
    mock_repo.get_issue.assert_called_with(100)

def test_github_get_file_content(mock_github):
    mock_repo = MagicMock()
    mock_content = MagicMock()
    
    mock_github.get_repo.return_value = mock_repo
    mock_repo.get_contents.return_value = mock_content
    mock_content.decoded_content = b"hello world"
    
    res = github_get_file_content("owner/repo", "src/main.py", ref="dev")
    assert res == "hello world"
    mock_repo.get_contents.assert_called_with("src/main.py", ref="dev")

def test_github_add_comment(mock_github):
    mock_repo = MagicMock()
    mock_issue = MagicMock()
    
    mock_github.get_repo.return_value = mock_repo
    mock_repo.get_issue.return_value = mock_issue
    
    github_add_comment("owner/repo", 42, "Looks good!")
    mock_issue.create_comment.assert_called_with("Looks good!")
