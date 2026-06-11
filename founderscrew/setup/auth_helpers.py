import os
import subprocess
import questionary
from rich.console import Console
from founderscrew.setup.tool_detector import ToolDetector
from founderscrew.config import settings

console = Console()

class CLIAuthHelper:
    """Provides helpers for verifying and setting up auth for CLI coding tools and cloud platforms."""

    @staticmethod
    def setup_github(token: str = None) -> bool:
        """Verify GitHub access token by making a quick API call."""
        import httpx
        if not token:
            token = settings.get("github.token")
        if not token:
            console.print("[red]❌ GitHub token not configured.[/red]")
            return False

        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        try:
            response = httpx.get("https://api.github.com/user", headers=headers)
            if response.status_code == 200:
                user_data = response.json()
                console.print(f"[green]✅ Connected successfully to GitHub as @{user_data.get('login')}[/green]")
                settings.set("github.token", token)
                settings.save()
                return True
            else:
                console.print(f"[red]❌ GitHub auth failed (HTTP {response.status_code}): {response.text}[/red]")
        except Exception as e:
            console.print(f"[red]❌ GitHub connection error: {e}[/red]")
        return False

    @staticmethod
    def setup_gcloud() -> bool:
        """Guides user through gcloud CLI auth or GCP credentials setup."""
        status = ToolDetector.detect_gcloud()
        if not status["installed"]:
            console.print("[yellow]⚠️  gcloud CLI is not installed.[/yellow]")
            console.print("To deploy to Google Cloud, install the Google Cloud SDK:")
            console.print("👉 [bold blue]https://cloud.google.com/sdk/docs/install[/bold blue]")
            console.print(
                "\n[bold yellow]💡 Installation Note:[/bold yellow]\n"
                "The Google Cloud SDK is a system-wide utility, [bold]not[/bold] a Python package. You do not\n"
                "need to install it in your virtual environment (`.venv`). You can download and install it\n"
                "globally on your system (e.g. from your desktop).\n\n"
                "[bold cyan]Important:[/bold cyan] Once the installation is complete, you must [bold]restart your terminal[/bold]\n"
                "so that the newly added `gcloud` command is recognized in your environment's PATH.\n"
            )
            
            if questionary.confirm("Would you like to open the Google Cloud SDK installation page in your browser?").ask():
                import webbrowser
                webbrowser.open("https://cloud.google.com/sdk/docs/install")
            
            # Fall back to asking for GOOGLE_API_KEY
            key = questionary.password("Please enter your Google API Key (for Gemini/Vertex AI fallback):").ask()
            if key:
                settings.set("google.api_key", key)
                settings.save()
                console.print("[green]✅ Google API Key saved.[/green]")
                return True
            return False

        if status["authenticated"]:
            console.print(f"[green]✅ {status['details']}[/green]")
            return True

        # Let's run interactive login
        console.print("[cyan]Running 'gcloud auth login' interactively...[/cyan]")
        try:
            # Let the process take over the terminal
            result = subprocess.run(["gcloud", "auth", "login"], check=True)
            if result.returncode == 0:
                # Re-check project ID
                project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
                if not project_id:
                    # Try to get active project
                    proj_res = subprocess.run(["gcloud", "config", "get-value", "project"], capture_output=True, text=True)
                    if proj_res.returncode == 0 and proj_res.stdout.strip():
                        project_id = proj_res.stdout.strip()
                        settings.set("google.project_id", project_id)
                console.print(f"[green]✅ Successfully logged in to Google Cloud! Project: {project_id}[/green]")
                settings.save()
                return True
        except Exception as e:
            console.print(f"[red]❌ gcloud login failed: {e}[/red]")
        return False

    @staticmethod
    def setup_claude() -> bool:
        """Guides the user through Claude Code CLI login or API credentials."""
        status = ToolDetector.detect_claude()
        if not status["installed"]:
            console.print("[yellow]⚠️  Claude Code CLI is not installed.[/yellow]")
            console.print("To install Claude Code, run:")
            console.print("👉 [bold blue]npm install -g @anthropic/claude-code[/bold blue]")
            
            # Fall back to asking for ANTHROPIC_API_KEY
            key = questionary.password("Please enter your Anthropic API Key (for Claude API mode):").ask()
            if key:
                settings.set("coding_tools.mode", "api")
                settings.set("coding_tools.anthropic_api_key", key)
                settings.save()
                console.print("[green]✅ Anthropic API Key saved (using API mode instead of CLI).[/green]")
                return True
            return False

        console.print("[cyan]Claude Code CLI detected. Let's make sure it is authorized.[/cyan]")
        console.print("We will run 'claude auth login' if needed. If already logged in, feel free to skip.")
        if questionary.confirm("Do you want to run Claude auth now?").ask():
            try:
                subprocess.run(["claude", "auth", "login"])
                console.print("[green]✅ Claude CLI check complete.[/green]")
                return True
            except Exception as e:
                console.print(f"[red]❌ Claude CLI login failed: {e}[/red]")
                return False
        return True

    @staticmethod
    def setup_cursor() -> bool:
        """Guides user through Cursor API key setup."""
        status = ToolDetector.detect_cursor()
        if not status["installed"]:
            console.print("[yellow]⚠️  Cursor CLI is not installed.[/yellow]")
            console.print("Cursor CLI allows automated code edits using the Cursor editor.")
            
        key = questionary.password("Please enter your Cursor or OpenAI API Key to enable Cursor API mode (optional):").ask()
        if key:
            settings.set("coding_tools.cursor_api_key", key)
            settings.save()
            console.print("[green]✅ Cursor API Key saved.[/green]")
            return True
        return False

    @staticmethod
    def setup_codex() -> bool:
        """Guides user through OpenAI Codex / GPT API key setup."""
        key = questionary.password("Please enter your OpenAI API Key (required for Codex/GPT mode):").ask()
        if key:
            settings.set("coding_tools.openai_api_key", key)
            settings.save()
            console.print("[green]✅ OpenAI API Key saved.[/green]")
            return True
        return False

    @staticmethod
    def setup_gemini_cli() -> bool:
        """Guides user through Gemini CLI auth or fallback."""
        key = questionary.password("Please enter your Gemini API Key (GOOGLE_API_KEY):").ask()
        if key:
            settings.set("google.api_key", key)
            settings.save()
            console.print("[green]✅ Gemini API Key saved.[/green]")
            return True
        return False
