from typing import List, Dict, Any
from founderscrew.tools.github_tools import get_github_client

def get_coderabbit_suggestions(repo_name: str, pr_number: int) -> List[Dict[str, Any]]:
    """Polls a GitHub Pull Request for comment feedback left by CodeRabbit.
    
    Looks for comments authored by 'coderabbitai' or containing 'coderabbit' in body/author,
    and returns a structured list of recommendations.
    """
    g = get_github_client()
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)
    
    suggestions = []
    
    # 1. Check PR review comments (line-level comments)
    for comment in pr.get_review_comments():
        author = comment.user.login.lower()
        body = comment.body or ""
        if "coderabbit" in author or "coderabbit" in body.lower():
            suggestions.append({
                "type": "review_comment",
                "file": comment.path,
                "line": comment.line or comment.original_line,
                "body": body,
                "id": comment.id
            })
            
    # 2. Check general PR comments
    for comment in pr.get_issue_comments():
        author = comment.user.login.lower()
        body = comment.body or ""
        if "coderabbit" in author or "coderabbit" in body.lower():
            suggestions.append({
                "type": "general_comment",
                "body": body,
                "id": comment.id
            })
            
    return suggestions
