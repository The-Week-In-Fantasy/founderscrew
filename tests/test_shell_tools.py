import pytest
from founderscrew.tools.shell_tools import is_safe_command, run_safe_shell_command

def test_is_safe_command():
    # Safe commands
    assert is_safe_command("pytest") is True
    assert is_safe_command("pytest tests/test_login.py") is True
    assert is_safe_command("python -m pytest") is True
    assert is_safe_command("npm test") is True
    assert is_safe_command("git status") is True
    
    # Unsafe commands
    assert is_safe_command("rm -rf /") is False
    assert is_safe_command("cat /etc/passwd") is False
    assert is_safe_command("curl http://malicious.site") is False
    assert is_safe_command("pytest && rm -rf .") is False

def test_run_safe_shell_command(tmp_path):
    # Unsafe command block
    res = run_safe_shell_command("rm -rf /", str(tmp_path))
    assert res["success"] is False
    assert "safety policy" in res["stderr"]
    
    # Safe command check (should succeed if git is installed or python version)
    res = run_safe_shell_command("python --version", str(tmp_path))
    assert res["success"] is True
    assert "Python" in res["stdout"] or "Python" in res["stderr"]
