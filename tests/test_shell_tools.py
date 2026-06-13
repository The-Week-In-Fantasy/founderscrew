import pytest
from contextlib import contextmanager
from founderscrew.tools.shell_tools import is_safe_command, run_safe_shell_command
import founderscrew.tools.shell_tools as shell_tools

def test_is_safe_command():
    # Safe commands
    assert is_safe_command("pytest") is True
    assert is_safe_command("pytest tests/test_login.py") is True
    assert is_safe_command("python -m pytest") is True
    assert is_safe_command("npm test") is True
    assert is_safe_command("npm run lint") is True
    assert is_safe_command("npm run typecheck") is True
    assert is_safe_command("npx tsc --noEmit") is True
    assert is_safe_command("ruff check .") is True
    assert is_safe_command("npx playwright install chromium") is True
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

def test_start_dev_server_waits_for_render_settle(tmp_path, monkeypatch):
    class FakeProc:
        returncode = None

        def poll(self):
            return None

    @contextmanager
    def fake_urlopen(_url, timeout=2):
        yield object()

    sleeps = []
    ticks = iter([0, 0, 0.1, 0.4, 0.6, 1.0])

    monkeypatch.setattr(shell_tools.subprocess, "Popen", lambda *args, **kwargs: FakeProc())
    monkeypatch.setattr(shell_tools.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(shell_tools.time, "monotonic", lambda: next(ticks, 2.0))
    monkeypatch.setattr(shell_tools.time, "sleep", lambda seconds: sleeps.append(seconds))

    proc, url = shell_tools.start_dev_server(
        str(tmp_path),
        command="npm test",
        boot_timeout=10,
        render_settle_seconds=0.5,
    )

    assert proc is not None
    assert url == "http://localhost:3001"
    assert sleeps
