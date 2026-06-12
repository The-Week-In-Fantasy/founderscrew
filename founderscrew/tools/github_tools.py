import os
import subprocess
from pathlib import Path
from typing import List, Dict, Optional, Any
from github import Github
from founderscrew.config import settings

def get_github_client() -> Github:
    """Returns an authenticated Github client instance."""
    token = settings.get("github.token") or os.getenv("GITHUB_TOKEN")
    if not token:
        raise ValueError(
            "GitHub token not found. Please run 'founderscrew setup' or set the GITHUB_TOKEN environment variable."
        )
    return Github(token)

_BOT_LOGIN: Optional[str] = None
_BOT_LOGIN_FETCHED = False

def github_get_bot_login() -> Optional[str]:
    """Returns the login of the authenticated bot account (cached after first call).

    Used to ignore the crew's own GitHub comments so the approval gates can
    never be triggered by text the bot itself posted.
    """
    global _BOT_LOGIN, _BOT_LOGIN_FETCHED
    if not _BOT_LOGIN_FETCHED:
        _BOT_LOGIN_FETCHED = True
        try:
            _BOT_LOGIN = get_github_client().get_user().login
        except Exception:
            _BOT_LOGIN = None
    return _BOT_LOGIN

def github_get_issue(repo_name: str, issue_number: int) -> Dict[str, Any]:
    """Retrieves details of a specific GitHub issue.
    
    Args:
        repo_name: The repository owner/name (e.g. 'octocat/hello-world')
        issue_number: The number of the issue to fetch
    """
    g = get_github_client()
    repo = g.get_repo(repo_name)
    issue = repo.get_issue(issue_number)
    
    comments = []
    for comment in issue.get_comments():
        comments.append({
            "id": comment.id,
            "user": comment.user.login,
            "body": comment.body,
            "created_at": comment.created_at.isoformat()
        })
        
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body or "",
        "creator": issue.user.login,
        "labels": [label.name for label in issue.labels],
        "state": issue.state,
        "comments": comments,
        "created_at": issue.created_at.isoformat()
    }

def github_list_repo_files(repo_name: str, path: str = "", ref: Optional[str] = None) -> List[str]:
    """Recursively lists all files in a repository or directory path.
    
    Args:
        repo_name: Repository name (e.g. 'owner/repo')
        path: Path within repository (default is root '')
        ref: Git ref (branch name, SHA, tag)
    """
    g = get_github_client()
    repo = g.get_repo(repo_name)
    
    files = []
    try:
        contents = repo.get_contents(path, ref=ref) if ref else repo.get_contents(path)
    except Exception as e:
        if getattr(e, "status", None) == 404:
            return []
        raise e
    
    # contents can be a list or a single ContentFile
    if not isinstance(contents, list):
        contents = [contents]
        
    queue = list(contents)
    while queue:
        file_content = queue.pop(0)
        if file_content.type == "dir":
            # Avoid scanning huge vendor/node_modules/venv directories recursively
            if file_content.name in (".git", "node_modules", ".venv", "__pycache__", "venv"):
                continue
            dir_contents = repo.get_contents(file_content.path, ref=ref) if ref else repo.get_contents(file_content.path)
            if isinstance(dir_contents, list):
                queue.extend(dir_contents)
        else:
            files.append(file_content.path)
            
    return files

def github_get_file_content(repo_name: str, path: str, ref: Optional[str] = None) -> str:
    """Gets the raw content of a file from a repository.
    
    Args:
        repo_name: Repository name (e.g. 'owner/repo')
        path: File path within the repository
        ref: Git branch, SHA, or tag
    """
    g = get_github_client()
    repo = g.get_repo(repo_name)
    try:
        file_content = repo.get_contents(path, ref=ref) if ref else repo.get_contents(path)
        if isinstance(file_content, list):
            return f"Error: Path '{path}' is a directory, not a file."
        return file_content.decoded_content.decode("utf-8", errors="replace")
    except Exception as e:
        if getattr(e, "status", None) == 404:
            return f"Error: File not found at path '{path}'."
        return f"Error retrieving file '{path}': {getattr(e, 'data', {}).get('message', str(e))}"

def github_search_code(repo_name: str, query: str) -> List[Dict[str, Any]]:
    """Searches for code patterns within a specific repository.
    
    Args:
        repo_name: Repository name (e.g. 'owner/repo')
        query: Search term or syntax (e.g. 'def handle_event')
    """
    g = get_github_client()
    # Scopes search to the specified repository
    search_query = f"{query} repo:{repo_name}"
    results = g.search_code(query=search_query)
    
    items = []
    # Limit results to top 15 matches to avoid token bloat
    for i, item in enumerate(results):
        if i >= 15:
            break
        items.append({
            "name": item.name,
            "path": item.path,
            "sha": item.sha
        })
    return items

def github_create_branch(repo_name: str, branch_name: str, base_branch: str = "main") -> str:
    """Creates a new branch off a base branch.
    
    Args:
        repo_name: Repository name
        branch_name: Name of the new branch to create
        base_branch: Name of the existing branch to branch off of
    """
    if base_branch == "main":
        base_branch = settings.get("github.base_branch", "main")
        
    g = get_github_client()
    repo = g.get_repo(repo_name)
    
    # Get base branch ref and SHA
    base_ref = repo.get_git_ref(f"heads/{base_branch}")
    base_sha = base_ref.object.sha
    
    # Create new ref
    ref_name = f"refs/heads/{branch_name}"
    try:
        repo.create_git_ref(ref=ref_name, sha=base_sha)
        return branch_name
    except Exception as e:
        # If it already exists, just return it
        if "already exists" in str(e).lower():
            return branch_name
        raise e

def github_commit_files(repo_name: str, branch: str, file_changes: Dict[str, str], message: str) -> str:
    """Commits multiple file changes sequentially to a branch.
    
    Args:
        repo_name: Repository name
        branch: Target branch name
        file_changes: Dictionary mapping file paths to their new contents
        message: Commit message
    """
    g = get_github_client()
    repo = g.get_repo(repo_name)
    
    for path, content in file_changes.items():
        try:
            # Check if file already exists in branch to update it
            contents = repo.get_contents(path, ref=branch)
            repo.update_file(
                path=path,
                message=f"Update {path}: {message}",
                content=content,
                sha=contents.sha,
                branch=branch
            )
        except Exception:
            # File doesn't exist yet, create it
            repo.create_file(
                path=path,
                message=f"Create {path}: {message}",
                content=content,
                branch=branch
            )
            
    return repo.get_branch(branch).commit.sha

def github_create_pr(repo_name: str, title: str, body: str, head_branch: str, base_branch: str = "main") -> Dict[str, Any]:
    """Creates a pull request on GitHub.
    
    Args:
        repo_name: Repository name
        title: PR title
        body: PR body description
        head_branch: Branch containing changes
        base_branch: Branch to merge into
    """
    if base_branch == "main":
        base_branch = settings.get("github.base_branch", "main")
        
    g = get_github_client()
    repo = g.get_repo(repo_name)
    
    pr = repo.create_pull(
        title=title,
        body=body,
        head=head_branch,
        base=base_branch
    )
    
    return {
        "number": pr.number,
        "url": pr.html_url,
        "title": pr.title,
        "state": pr.state
    }

def github_add_comment(repo_name: str, issue_number: int, body: str) -> None:
    """Adds a comment to a GitHub issue or pull request.
    
    Args:
        repo_name: Repository name
        issue_number: Issue or PR number
        body: Markdown comment body
    """
    g = get_github_client()
    repo = g.get_repo(repo_name)
    issue = repo.get_issue(issue_number)
    issue.create_comment(body)

def github_merge_pr(repo_name: str, pr_number: int) -> bool:
    """Merges a pull request on GitHub.
    
    Args:
        repo_name: Repository name
        pr_number: Pull request number
    """
    g = get_github_client()
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)
    status = pr.merge()
    return status.merged

# Files Founders.crew itself generates inside workspaces; never treated as
# agent work-in-progress and never committed back to the user's repository.
_WORKSPACE_ARTIFACTS = {".env", "playwright.config.js"}

# Feature branch each repo's workspace should stay on while a workflow is in
# flight. While set, clone_or_pull keeps the workspace on this branch instead
# of resetting to the base branch (which would test/deploy the wrong code).
_ACTIVE_BRANCHES: Dict[str, str] = {}

def set_active_workspace_branch(repo_name: str, branch_name: Optional[str]) -> None:
    """Pins (or unpins, when branch_name is None) the workspace to a feature branch."""
    if branch_name:
        _ACTIVE_BRANCHES[repo_name] = branch_name
    else:
        _ACTIVE_BRANCHES.pop(repo_name, None)

def github_prepare_workspace_branch(repo_name: str, branch_name: str) -> str:
    """Creates/resets the feature branch in the local workspace and pins it active.

    Call after a fresh clone_or_pull so the branch starts from the latest base.
    """
    workdir = _workspace_dir(repo_name)
    if not (workdir / ".git").exists():
        raise RuntimeError(f"No local workspace found for {repo_name}; cannot create branch {branch_name}.")
    res = subprocess.run(
        ["git", "checkout", "-B", branch_name],
        cwd=str(workdir), capture_output=True, text=True
    )
    if res.returncode != 0:
        raise RuntimeError(f"Failed to create workspace branch {branch_name}: {res.stderr or res.stdout}")
    set_active_workspace_branch(repo_name, branch_name)
    return str(workdir.resolve())

def _workspace_dir(repo_name: str) -> Path:
    return Path.home() / ".founderscrew" / "workspaces" / repo_name.replace("/", "_")

def _workspace_has_agent_changes(workdir: Path) -> bool:
    """True if the workspace has uncommitted changes beyond our own artifacts."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(workdir), capture_output=True, text=True
    )
    for line in (status.stdout or "").splitlines():
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if path not in _WORKSPACE_ARTIFACTS:
            return True
    return False

def github_push_workspace(repo_name: str, branch_name: str, commit_message: str) -> Dict[str, Any]:
    """Commits all local workspace changes to a branch and pushes it to GitHub.

    Args:
        repo_name: Repository name (owner/repo)
        branch_name: Branch to commit and push to (created if it doesn't exist)
        commit_message: Commit message describing the changes
    """
    token = settings.get("github.token") or os.getenv("GITHUB_TOKEN")
    workdir = _workspace_dir(repo_name)
    if not (workdir / ".git").exists():
        return {"success": False, "error": f"No local workspace found for {repo_name}. Nothing to push."}

    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=str(workdir), capture_output=True, text=True)

    def _sanitize(text: str) -> str:
        return text.replace(token, "***") if token else text

    _git("checkout", "-B", branch_name)
    _git("add", "-A")
    # Never commit secrets or auto-injected test configuration
    _git("reset", "-q", "--", *_WORKSPACE_ARTIFACTS)

    commit = _git("commit", "-m", commit_message)
    committed = commit.returncode == 0
    if not committed and "nothing to commit" not in (commit.stdout + commit.stderr).lower():
        return {"success": False, "error": f"git commit failed: {_sanitize(commit.stderr or commit.stdout)}"}

    push_url = f"https://{token}@github.com/{repo_name}.git" if token else f"https://github.com/{repo_name}.git"
    push = _git("push", push_url, f"HEAD:refs/heads/{branch_name}")
    if push.returncode != 0:
        return {"success": False, "error": f"git push failed: {_sanitize(push.stderr or push.stdout)}"}

    sha = _git("rev-parse", "HEAD").stdout.strip()
    return {
        "success": True,
        "branch": branch_name,
        "commit_sha": sha,
        "committed_new_changes": committed
    }

def github_clone_or_pull(repo_name: str) -> str:
    """Clones or pulls the repository to a local workspace directory and returns its absolute path.
    
    Args:
        repo_name: Repository name (owner/repo)
    """
    token = settings.get("github.token") or os.getenv("GITHUB_TOKEN")
    base_branch = settings.get("github.base_branch", "main")
    
    workdir = _workspace_dir(repo_name)
    workdir.mkdir(parents=True, exist_ok=True)
    
    # Check if .git folder exists
    if (workdir / ".git").exists():
        if _workspace_has_agent_changes(workdir):
            # The Builder has uncommitted work in progress; a checkout/pull here
            # would clobber it before the Tester/Deployer stages can use it.
            pass
        else:
            active_branch = _ACTIVE_BRANCHES.get(repo_name)
            if active_branch:
                # A workflow is in flight: stay on (or return to) its feature
                # branch rather than resetting to base
                subprocess.run(["git", "fetch", "origin"], cwd=str(workdir), capture_output=True)
                checkout = subprocess.run(["git", "checkout", active_branch], cwd=str(workdir), capture_output=True)
                if checkout.returncode != 0:
                    subprocess.run(["git", "checkout", "-B", active_branch], cwd=str(workdir), capture_output=True)
            else:
                # Ensure we're on the base branch and pull latest changes
                subprocess.run(["git", "checkout", base_branch], cwd=str(workdir), capture_output=True)
                pull = subprocess.run(["git", "pull", "origin", base_branch], cwd=str(workdir), capture_output=True, text=True)
                if pull.returncode != 0:
                    print(f"Warning: git pull for {repo_name} failed; continuing with existing local copy. {pull.stderr}")
    else:
        # Clone repo
        clone_url = f"https://{token}@github.com/{repo_name}.git" if token else f"https://github.com/{repo_name}.git"
        clone = subprocess.run(["git", "clone", "-b", base_branch, clone_url, "."], cwd=str(workdir), capture_output=True, text=True)
        if clone.returncode != 0:
            err = (clone.stderr or clone.stdout or "unknown error")
            if token:
                err = err.replace(token, "***")
            raise RuntimeError(f"Failed to clone {repo_name} (base branch '{base_branch}'): {err}")
        
    # Generate .env file securely from keyring
    env_vars = settings.get("workspace_env", {})
    if env_vars:
        try:
            with open(workdir / ".env", "w", encoding="utf-8") as f:
                for k, v in env_vars.items():
                    f.write(f"{k}={v}\n")
        except Exception as e:
            print(f"Warning: Failed to write workspace .env file: {e}")

    # Inject a self-contained playwright.config.js for isolated testing
    # This ensures Founders.crew never hijacks the user's live dev server
    _inject_playwright_config(workdir)
            
    return str(workdir.resolve())


# Isolated port used by the Founders.crew test runner
_TEST_PORT = 3001

_PLAYWRIGHT_CONFIG_TEMPLATE = f"""\
// Auto-generated by Founders.crew for isolated testing.
// This file is untracked and will be regenerated on every run.
import {{ defineConfig, devices }} from '@playwright/test';
import dotenv from 'dotenv';
dotenv.config();

export default defineConfig({{
  testDir: './tests/integration',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 2,
  reporter: [['list'], ['html', {{ open: 'never' }}]],
  use: {{
    baseURL: 'http://localhost:{_TEST_PORT}',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  }},
  projects: [
    {{ name: 'chromium', use: {{ ...devices['Desktop Chrome'] }} }},
    {{ name: 'Mobile Chrome', use: {{ ...devices['Pixel 5'] }} }},
  ],
  webServer: {{
    command: 'npx vite --port {_TEST_PORT} --strictPort',
    url: 'http://localhost:{_TEST_PORT}',
    reuseExistingServer: false,
    timeout: 120 * 1000,
  }},
}});
"""

def _inject_playwright_config(workdir: Path) -> None:
    """Writes a self-contained playwright.config.js into the workspace."""
    # Only inject if the project actually has Playwright tests
    tests_dir = workdir / "tests" / "integration"
    if not tests_dir.exists():
        return
    try:
        with open(workdir / "playwright.config.js", "w", encoding="utf-8") as f:
            f.write(_PLAYWRIGHT_CONFIG_TEMPLATE)
    except Exception as e:
        print(f"Warning: Failed to inject playwright.config.js: {e}")
