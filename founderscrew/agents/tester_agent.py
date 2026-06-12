import re
from pathlib import Path
from typing import Dict, Any
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from founderscrew.tools import run_safe_shell_command, github_clone_or_pull, capture_screenshot
from founderscrew.config import settings

_TEST_FILE_EXT = re.compile(r"\.(m?[jt]sx?|py)$")

def _resolve_test_paths(command: str, workdir: str) -> str:
    """Rewrites test file paths in the command that don't exist on disk.

    The Builder sometimes reports a test_command whose path doesn't match where
    the test file was actually saved (e.g. 'tests/issue_1.spec.js' vs
    'tests/integration/issue_1.spec.js'), which makes runners like Playwright
    and Jest fail with 'No tests found'. Locate the file by basename instead.
    """
    root = Path(workdir)
    resolved = []
    for tok in command.split():
        normalized = tok.replace("\\", "/")
        if ("/" in normalized and _TEST_FILE_EXT.search(normalized)
                and not (root / normalized).exists()):
            name = Path(normalized).name
            matches = [
                p for p in root.rglob(name)
                if "node_modules" not in p.parts and ".git" not in p.parts
            ]
            if matches:
                tok = matches[0].relative_to(root).as_posix()
        resolved.append(tok)
    return " ".join(resolved)

def run_test_command(command: str, repository: str) -> Dict[str, Any]:
    """Runs automated test command in the repository workspace.
    
    Args:
        command: Test command to run (e.g. 'pytest' or 'npm test').
        repository: Repository owner/name (e.g. 'owner/repo').
    """
    try:
        workdir = github_clone_or_pull(repository)
        
        # Auto-install dependencies based on the project type if needed
        if command.startswith("npm") or (Path(workdir) / "package.json").exists():
            if not (Path(workdir) / "node_modules").exists():
                install_res = run_safe_shell_command("npm install", workdir)
                if not install_res["success"]:
                    return {
                        "success": False,
                        "stdout": install_res["stdout"],
                        "stderr": f"Dependency installation failed (npm install):\n{install_res['stderr']}",
                        "returncode": install_res["returncode"]
                    }
        elif command.startswith("pytest") or (Path(workdir) / "requirements.txt").exists():
            # Check for a virtualenv, or just run pip install
            # For simplicity, just run pip install -r requirements.txt if it exists
            if (Path(workdir) / "requirements.txt").exists():
                install_res = run_safe_shell_command("pip install -r requirements.txt", workdir)
                if not install_res["success"]:
                    return {
                        "success": False,
                        "stdout": install_res["stdout"],
                        "stderr": f"Dependency installation failed (pip install):\n{install_res['stderr']}",
                        "returncode": install_res["returncode"]
                    }

        # Isolation environment: forces Playwright to spin up its own Vite
        # dev server on an isolated port instead of reusing the user's live one
        test_env = {
            "CI": "1",
            "PORT": "3001",
            "PLAYWRIGHT_BASE_URL": "http://localhost:3001",
        }

        import logging
        log = logging.getLogger("founderscrew.tester")

        try:
            test_timeout = int(settings.get("testing.timeout_seconds", 600) or 600)
        except (TypeError, ValueError):
            test_timeout = 600

        resolved_command = _resolve_test_paths(command, workdir)
        if resolved_command != command:
            log.info(f"Rewrote test command path(s): '{command}' -> '{resolved_command}'")
        res = run_safe_shell_command(resolved_command, workdir, timeout=test_timeout, extra_env=test_env)

        # Rescue pass: if the runner found no test files, retry with a loose
        # name filter (treated as a regex by Playwright/Jest) instead of an
        # exact path, since the Builder may have misreported the path entirely.
        combined = f"{res['stdout']}\n{res['stderr']}"
        if not res["success"] and "no tests found" in combined.lower():
            issue_match = re.search(r"issue[_-]?\d+", resolved_command, re.IGNORECASE)
            if issue_match and resolved_command.split()[0] in ("npx", "npm"):
                base = " ".join(t for t in resolved_command.split() if not _TEST_FILE_EXT.search(t))
                retry_command = f"{base} {issue_match.group(0)}"
                log.info(f"No tests found; retrying with name filter: '{retry_command}'")
                retry_res = run_safe_shell_command(retry_command, workdir, timeout=test_timeout, extra_env=test_env)
                retry_combined = f"{retry_res['stdout']}\n{retry_res['stderr']}"
                if "no tests found" not in retry_combined.lower():
                    res = retry_res

        log.info(f"Captured STDOUT: {res['stdout'][:500]}")
        log.info(f"Captured STDERR: {res['stderr'][:500]}")
        return res
    except Exception as e:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Error resolving workspace or installing dependencies: {e}",
            "returncode": -1
        }

def get_tester_agent() -> LlmAgent:
    """Returns the tester agent instance."""
    return LlmAgent(
        name="TesterAgent",
        description="Autonomous testing agent that executes test suites and validates runtime execution.",
        model=settings.get("agents.fast_model", "gemini-3.1-flash-lite"),
        instruction="""You are a Quality Assurance Automation Engineer.
Your job is to run tests in the repository and report outcomes.

To perform this job:
1. Run test commands using run_test_command.
2. If there are browser tests or local endpoints to validate, capture screens using capture_screenshot.
3. Review logs or errors if tests fail.

Return a structured markdown JSON block containing:
- passed: boolean (true if all tests passed, false if any failed)
- output: The EXACT raw stdout/stderr text returned by the run_test_command tool. DO NOT summarize it. DO NOT say 'No output captured' unless the tool literally returns an empty string.
- failures: list of failing test descriptions if any
""",
        tools=[
            FunctionTool(run_test_command),
            FunctionTool(capture_screenshot)
        ],
        output_key="test_result"
    )
