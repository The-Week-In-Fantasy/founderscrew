import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from founderscrew.config import settings, CONFIG_FILE
from founderscrew.setup.tool_detector import ToolDetector

console = Console()

class Doctor:
    """Diagnoses and validates the health of the Founders.crew installation and dependencies."""

    @classmethod
    def check_github(cls) -> tuple[bool, str]:
        token = settings.get("github.token")
        if not token:
            return False, "No token found in configuration"
        
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        try:
            response = httpx.get("https://api.github.com/user", headers=headers, timeout=5)
            if response.status_code == 200:
                user_data = response.json()
                return True, f"Connected as @{user_data.get('login')}"
            else:
                return False, f"Invalid token (HTTP {response.status_code})"
        except Exception as e:
            return False, f"Connection failed: {e}"

    @classmethod
    def check_google(cls) -> tuple[bool, str]:
        api_key = settings.get("google.api_key")
        project_id = settings.get("google.project_id")
        
        if api_key:
            # Quick check on Vertex/AI Studio API key
            try:
                # Basic test query to Gemini API endpoint
                url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
                response = httpx.get(url, timeout=5)
                if response.status_code == 200:
                    return True, "API Key is valid"
                else:
                    return False, f"Invalid API Key (HTTP {response.status_code})"
            except Exception as e:
                return False, f"Failed API connection: {e}"
        elif project_id:
            return True, f"Google Cloud Project: {project_id} (using ADC)"
        else:
            return False, "No API key or Google Cloud Project ID configured"

    @classmethod
    def diagnose(cls) -> bool:
        """Run all diagnostic checks and display a detailed report."""
        console.print("\n[bold cyan]🏥 Founders.crew Health Check[/bold cyan]")
        console.print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

        warnings = 0
        errors = 0

        # 1. Config Check
        config_ok = CONFIG_FILE.exists()
        if config_ok:
            console.print(f"  [green]✅[/green] [bold]Configuration[/bold]    Saved at {CONFIG_FILE}")
        else:
            console.print(f"  [red]❌[/red] [bold]Configuration[/bold]    Missing! Run 'founders-crew setup'")
            errors += 1

        # 2. GitHub Check
        gh_ok, gh_msg = cls.check_github()
        if gh_ok:
            console.print(f"  [green]✅[/green] [bold]GitHub[/bold]           {gh_msg}")
        else:
            console.print(f"  [red]❌[/red] [bold]GitHub[/bold]           {gh_msg}")
            errors += 1

        # 3. Google API Check
        google_ok, google_msg = cls.check_google()
        if google_ok:
            console.print(f"  [green]✅[/green] [bold]Google Cloud[/bold]     {google_msg}")
        else:
            console.print(f"  [yellow]⚠️[/yellow] [bold]Google Cloud[/bold]     {google_msg} (Gemini fallback only)")
            warnings += 1

        # 4. CLI Tools Scan
        console.print("\n[bold]Checking Local CLI Coding Tools:[/bold]")
        tools = ToolDetector.detect_all()
        for name, details in tools.items():
            if name in ["git", "gcloud"]:
                # Core system tools
                if details["installed"]:
                    console.print(f"  [green]✅[/green] [bold]{name.upper():<10}[/bold]       Installed ({details['version']})")
                else:
                    console.print(f"  [red]❌[/red] [bold]{name.upper():<10}[/bold]       {details['details']}")
                    errors += 1
            else:
                # Coding agents
                if details["installed"]:
                    auth_indicator = "[green]✅[/green]" if details["authenticated"] else "[yellow]⚠️[/yellow]"
                    status_msg = "authenticated" if details["authenticated"] else "unauthenticated"
                    console.print(f"  {auth_indicator} [bold]{name.upper():<10}[/bold]       Installed ({details['version']}) — {status_msg}")
                else:
                    # Not installed is just a warning unless it's the preferred tool
                    is_pref = settings.get("coding_tools.preferred") == name
                    indicator = "[red]❌[/red]" if is_pref else "[yellow]⚠️[/yellow]"
                    if is_pref:
                        console.print(f"  {indicator} [bold]{name.upper():<10}[/bold]       [red]Not installed (but configured as preferred!)[/red]")
                        errors += 1
                    else:
                        console.print(f"  {indicator} [bold]{name.upper():<10}[/bold]       Not installed")
                        warnings += 1

        # Summary Panel
        console.print("")
        if errors > 0:
            console.print(Panel(
                f"[bold red]System is Unhealthy.[/bold red]\n"
                f"Errors: {errors}, Warnings: {warnings}\n\n"
                f"👉 Please resolve the red items above or run [bold cyan]founders-crew setup[/bold cyan] again.",
                border_style="red"
            ))
            return False
        elif warnings > 0:
            console.print(Panel(
                f"[bold yellow]System is Operational with warnings.[/bold yellow]\n"
                f"Errors: {errors}, Warnings: {warnings}\n\n"
                f"You are good to go, but some non-essential features/coding tools are missing.",
                border_style="yellow"
            ))
            return True
        else:
            console.print(Panel(
                "[bold green]System is healthy! All systems operational.[/bold green]\n"
                "You are ready to launch Founders.crew.",
                border_style="green"
            ))
            return True
