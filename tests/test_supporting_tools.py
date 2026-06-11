import pytest
from unittest.mock import MagicMock, patch
from founderscrew.tools.coderabbit_tools import get_coderabbit_suggestions
from founderscrew.tools.search_tools import google_search

def test_coderabbit_suggestions_empty():
    """Verify empty list is returned when there are no coderabbit comments."""
    with patch("founderscrew.tools.coderabbit_tools.get_github_client") as mock_getter:
        mock_client = MagicMock()
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        
        mock_getter.return_value = mock_client
        mock_client.get_repo.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr
        
        # Configure empty comments
        mock_pr.get_review_comments.return_value = []
        mock_pr.get_issue_comments.return_value = []
        
        res = get_coderabbit_suggestions("owner/repo", 1)
        assert res == []

def test_coderabbit_suggestions_found():
    """Verify suggestions are parsed when comments have coderabbit in author or body."""
    with patch("founderscrew.tools.coderabbit_tools.get_github_client") as mock_getter:
        mock_client = MagicMock()
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        
        mock_getter.return_value = mock_client
        mock_client.get_repo.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr
        
        # Configure review comment
        mock_comment = MagicMock()
        mock_comment.user.login = "coderabbitai"
        mock_comment.body = "You should simplify this loop"
        mock_comment.path = "main.py"
        mock_comment.line = 10
        mock_comment.id = 999
        mock_pr.get_review_comments.return_value = [mock_comment]
        
        # Configure general PR comment
        mock_general = MagicMock()
        mock_general.user.login = "some-user"
        mock_general.body = "Review done by CodeRabbit is excellent"
        mock_general.id = 888
        mock_pr.get_issue_comments.return_value = [mock_general]
        
        res = get_coderabbit_suggestions("owner/repo", 1)
        assert len(res) == 2
        assert res[0]["type"] == "review_comment"
        assert res[0]["file"] == "main.py"
        assert res[0]["body"] == "You should simplify this loop"
        assert res[1]["type"] == "general_comment"
        assert "CodeRabbit" in res[1]["body"]

def test_google_search_mock():
    """Verify fallback mock searches return useful details when api is not configured."""
    res = google_search("Explain Pydantic v2 changes")
    assert "Pydantic v2 Migration Guide" in res
    assert "model_dump()" in res
    
    res_generic = google_search("some random topic")
    assert "some random topic" in res_generic
    assert "Mock search result" in res_generic
