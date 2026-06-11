from typing import Dict, Any
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from founderscrew.tools import run_safe_shell_command, github_clone_or_pull, capture_screenshot
from founderscrew.config import settings

def run_test_command(command: str, repository: str) -> Dict[str, Any]:
    """Runs automated test command in the repository workspace.
    
    Args:
        command: Test command to run (e.g. 'pytest' or 'npm test').
        repository: Repository owner/name (e.g. 'owner/repo').
    """
    try:
        from pathlib import Path
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

        res = run_safe_shell_command(command, workdir, timeout=180, extra_env=test_env)
        import logging
        log = logging.getLogger("founderscrew.tester")
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
        model=settings.get("agents.fast_model", "gemini-2.5-flash"),
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
