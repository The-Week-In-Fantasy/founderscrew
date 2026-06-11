import shutil
import subprocess
from typing import Dict, Any, Optional

class ToolDetector:
    """Detects CLI tools on the user's system and checks installation/auth status."""

    @staticmethod
    def check_command(cmd: str) -> Optional[str]:
        """Find command in PATH and return its absolute path if found."""
        return shutil.which(cmd)

    @classmethod
    def get_version(cls, cmd: str, args: list[str] = ["--version"]) -> Optional[str]:
        """Run command with version arguments and return the first line of output."""
        path = cls.check_command(cmd)
        if not path:
            return None
        try:
            result = subprocess.run([cmd] + args, capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                output = result.stdout.strip() or result.stderr.strip()
                return output.split("\n")[0] if output else "Unknown version"
        except Exception:
            pass
        return "Unknown version"

    @classmethod
    def detect_git(cls) -> Dict[str, Any]:
        path = cls.check_command("git")
        version = cls.get_version("git") if path else None
        return {
            "installed": path is not None,
            "path": path,
            "version": version,
            "authenticated": True if path else False, # Git doesn't have a single 'auth' state, it's repo-dependent
            "details": "Required for cloning, committing, and pushing code changes." if path else "Git is not installed. Please install Git."
        }

    @classmethod
    def detect_gcloud(cls) -> Dict[str, Any]:
        path = cls.check_command("gcloud")
        version = cls.get_version("gcloud", ["--version"]) if path else None
        
        authenticated = False
        details = "Google Cloud SDK. Required if deploying to Cloud Run."
        if path:
            try:
                # Check active accounts
                result = subprocess.run(["gcloud", "auth", "list", "--format=value(account)"], capture_output=True, text=True, timeout=3)
                if result.returncode == 0 and result.stdout.strip():
                    authenticated = True
                    details = f"Authenticated as: {result.stdout.strip()}"
                else:
                    details = "gcloud is installed, but no active account is authenticated."
            except Exception:
                details = "gcloud is installed, but check_auth failed."

        return {
            "installed": path is not None,
            "path": path,
            "version": version,
            "authenticated": authenticated,
            "details": details
        }

    @classmethod
    def detect_claude(cls) -> Dict[str, Any]:
        # The claude CLI tool (Claude Code) is run with 'claude'
        path = cls.check_command("claude")
        version = cls.get_version("claude", ["--version"]) if path else None
        
        authenticated = False
        details = "Anthropic's Claude Code CLI. Enables advanced agentic coding."
        if path:
            # We can run a quick check, but claude code doesn't have a fast check-auth command.
            # Usually, we can check if ~/.anthropic/config.json exists or similar, or assume it needs auth.
            # For simplicity, if we run claude --help it might tell us. 
            # We will default authenticated to True for now, and handle re-auth interactively.
            authenticated = True
            details = "Claude Code CLI detected."

        return {
            "installed": path is not None,
            "path": path,
            "version": version,
            "authenticated": authenticated,
            "details": details
        }

    @classmethod
    def detect_cursor(cls) -> Dict[str, Any]:
        # Cursor CLI is typically run as 'cursor'
        path = cls.check_command("cursor")
        version = cls.get_version("cursor", ["--version"]) if path else None
        return {
            "installed": path is not None,
            "path": path,
            "version": version,
            "authenticated": True if path else False, # Typically reads from env/global config
            "details": "Cursor editor CLI. Used for auto-editing code files."
        }

    @classmethod
    def detect_codex(cls) -> Dict[str, Any]:
        path = cls.check_command("codex")
        version = cls.get_version("codex", ["--version"]) if path else None
        return {
            "installed": path is not None,
            "path": path,
            "version": version,
            "authenticated": True if path else False,
            "details": "OpenAI Codex CLI."
        }

    @classmethod
    def detect_gemini(cls) -> Dict[str, Any]:
        path = cls.check_command("gemini")
        version = cls.get_version("gemini", ["--version"]) if path else None
        return {
            "installed": path is not None,
            "path": path,
            "version": version,
            "authenticated": True if path else False,
            "details": "Gemini developer CLI."
        }

    @classmethod
    def detect_all(cls) -> Dict[str, Dict[str, Any]]:
        """Run detection on all supported CLI tools."""
        return {
            "git": cls.detect_git(),
            "gcloud": cls.detect_gcloud(),
            "claude": cls.detect_claude(),
            "cursor": cls.detect_cursor(),
            "codex": cls.detect_codex(),
            "gemini": cls.detect_gemini()
        }
