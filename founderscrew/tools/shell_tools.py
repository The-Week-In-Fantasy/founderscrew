import re
import os
import sys
import time
import logging
import subprocess
import urllib.request
from typing import List, Dict, Any, Union, Optional, Tuple
from founderscrew.config import settings

logger = logging.getLogger("founderscrew.shell")

# Safe commands regex list
SAFE_COMMAND_PATTERNS = [
    r"^pytest(?:\s+.*)?$",
    r"^python\s+-m\s+pytest(?:\s+.*)?$",
    r"^npm\s+(?:test|run\s+test)(?:\s+.*)?$",
    r"^npm\s+run\s+(?:lint|typecheck|type-check|check)(?:\s+.*)?$",
    r"^npm\s+(?:install|ci)(?:\s+.*)?$",
    r"^npx\s+(?:playwright\s+(?:test|install)(?:\s+.*)?|jest|vitest|tsc\s+--noEmit)(?:\s+.*)?$",
    r"^ruff\s+check(?:\s+.*)?$",
    r"^node\s+.*$",
    r"^pip\s+install\s+-e\s+\.(?:\s+.*)?$",
    r"^pip\s+install\s+-r\s+.*$",
    r"^python\s+--version$",
    r"^git\s+status$"
]

def is_safe_command(command_str: str) -> bool:
    """Validates if a command string is in the safe list."""
    cmd = command_str.strip()
    # Block command chaining/injection characters
    for char in ["&", ";", "|", "`", "$", "\n"]:
        if char in cmd:
            return False
            
    for pattern in SAFE_COMMAND_PATTERNS:
        if re.match(pattern, cmd):
            return True
    return False

def run_safe_shell_command(command: Union[str, List[str]], cwd: str, timeout: int = 60, extra_env: dict = None) -> Dict[str, Any]:
    """Safely executes a shell command within a timeout and captures output.
    
    Args:
        command: Command string or list of args
        cwd: Directory to run the command in
        timeout: Execution timeout in seconds
        extra_env: Additional environment variables to inject into the subprocess
    """
    if isinstance(command, list):
        command_str = " ".join(command)
    else:
        command_str = command
        
    if not is_safe_command(command_str):
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Command blocked by safety policy: '{command_str}'. Only standard test runners (pytest, npm test) and status checks are permitted.",
            "returncode": -1
        }
    
    # Build environment: inherit host env and merge any extra vars
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
        
    try:
        result = subprocess.run(
            command_str,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            timeout=timeout,
            shell=True,
            env=env
        )
        # Strip ANSI escape codes
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        clean_stdout = ansi_escape.sub('', result.stdout)
        clean_stderr = ansi_escape.sub('', result.stderr)
        
        return {
            "success": result.returncode == 0,
            "stdout": clean_stdout,
            "stderr": clean_stderr,
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired as te:
        return {
            "success": False,
            "stdout": te.stdout or "",
            "stderr": f"Command timed out after {timeout} seconds. Output: {te.stderr or ''}",
            "returncode": -2
        }
    except Exception as e:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Failed to execute command: {e}",
            "returncode": -3
        }

def start_dev_server(
    workdir: str,
    command: Optional[str] = None,
    port: int = 3001,
    boot_timeout: int = 90,
    render_settle_seconds: Optional[float] = None,
) -> Tuple[Optional[subprocess.Popen], Optional[str]]:
    """Starts the project's dev server so QA can screenshot a real running app.

    Returns (process, url) once the server answers HTTP, or (None, None) if it
    never came up within boot_timeout seconds.
    """
    command = command or f"npx vite --port {port} --strictPort"
    url = f"http://localhost:{port}"
    env = os.environ.copy()
    env.update({"CI": "1", "PORT": str(port), "BROWSER": "none"})
    if render_settle_seconds is None:
        try:
            render_settle_seconds = float(settings.get("qa.dev_server_ready_delay_seconds", 5) or 0)
        except (TypeError, ValueError):
            render_settle_seconds = 5
    try:
        proc = subprocess.Popen(
            command, shell=True, cwd=workdir, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception as e:
        logger.warning(f"Failed to launch dev server '{command}': {e}")
        return None, None

    deadline = time.monotonic() + boot_timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            logger.warning(f"Dev server '{command}' exited early with code {proc.returncode}.")
            return None, None
        try:
            with urllib.request.urlopen(url, timeout=2):
                settle_deadline = time.monotonic() + max(0.0, render_settle_seconds)
                while time.monotonic() < settle_deadline:
                    if proc.poll() is not None:
                        logger.warning(f"Dev server '{command}' exited during render settle with code {proc.returncode}.")
                        return None, None
                    time.sleep(min(0.25, settle_deadline - time.monotonic()))
                return proc, url
        except Exception:
            time.sleep(2)

    logger.warning(f"Dev server '{command}' did not answer on {url} within {boot_timeout}s.")
    stop_dev_server(proc)
    return None, None

def stop_dev_server(proc: Optional[subprocess.Popen]) -> None:
    """Stops a dev server started with start_dev_server, including child processes."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            # shell=True wraps the server in cmd.exe; kill the whole tree
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception as e:
        logger.warning(f"Failed to stop dev server (pid {proc.pid}): {e}")
