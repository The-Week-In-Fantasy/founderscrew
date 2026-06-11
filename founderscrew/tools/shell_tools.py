import re
import os
import subprocess
from typing import List, Dict, Any, Union

# Safe commands regex list
SAFE_COMMAND_PATTERNS = [
    r"^pytest(?:\s+.*)?$",
    r"^python\s+-m\s+pytest(?:\s+.*)?$",
    r"^npm\s+(?:test|run\s+test)(?:\s+.*)?$",
    r"^npm\s+(?:install|ci)(?:\s+.*)?$",
    r"^npx\s+(?:playwright\s+test|jest|vitest)(?:\s+.*)?$",
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
