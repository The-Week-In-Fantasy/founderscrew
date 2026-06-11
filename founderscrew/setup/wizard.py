import os
import sys
import httpx
import questionary
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.markdown import Markdown
from founderscrew.config import settings
from founderscrew.setup.github_device_flow import GitHubDeviceFlow
from founderscrew.setup.auth_helpers import CLIAuthHelper
from founderscrew.setup.tool_detector import ToolDetector

console = Console()

class SetupWizard:
    """Guided interactive 5-step CLI setup wizard for Founders.crew."""

    def __init__(self):
        self.github_token = None
        self.selected_repo = None
        self.preferred_tool = "claude"

    def run(self) -> None:
        """Run the interactive wizard."""
        console.clear()
        console.print(Panel.fit(
            "[bold cyan]🚀 Welcome to Founders.crew — Virtual DevOps Team[/bold cyan]\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Let's get your AI DevOps team configured. This will take about 3 minutes.",
            border_style="cyan"
        ))

        # Step 1: GitHub Connection
        self.step_github()

        # Step 2: Google Cloud Connection
        self.step_gcloud()

        # Step 3: Coding Tools Configuration
        self.step_coding_tools()

        # Step 4: Repository Selection
        self.step_repository()

        # Step 5: Review & Save
        self.step_review_and_save()

    def _print_pat_instructions(self) -> None:
        console.print("\n[bold yellow]🔑 How to generate a GitHub Personal Access Token (PAT):[/bold yellow]")
        console.print("1. Visit: [bold underline blue]https://github.com/settings/tokens[/bold underline blue]")
        console.print("2. Click [bold]Generate new token[/bold] -> [bold]Generate new token (classic)[/bold]")
        console.print("3. Choose an expiration date (e.g. 90 days)")
        console.print("4. Select the following permission scopes:")
        console.print("   - [green]✓ repo[/green] (Required: to read/write code and create branches)")
        console.print("   - [green]✓ admin:repo_hook[/green] (Required: to create issue trigger webhooks automatically)")
        console.print("5. Click [bold]Generate token[/bold], copy it, and paste it below:\n")

    def step_github(self) -> None:
        console.print("\n[bold magenta]Step 1 of 5: GitHub Connection[/bold magenta]")
        console.print("─────────────────────────────────")
        
        choice = questionary.select(
            "How would you like to connect to GitHub?",
            choices=[
                "Open browser to authorize (Recommended)",
                "I already have a Personal Access Token (PAT)",
                "Skip for now"
            ]
        ).ask()

        if choice == "Open browser to authorize (Recommended)":
            flow = GitHubDeviceFlow()
            try:
                self.github_token = flow.run()
                CLIAuthHelper.setup_github(self.github_token)
            except Exception as e:
                console.print(f"[red]OAuth login failed: {e}. Falling back to token entry...[/red]")
                self._print_pat_instructions()
                self.github_token = questionary.password("Please enter your GitHub Personal Access Token (PAT):").ask()
                CLIAuthHelper.setup_github(self.github_token)
        elif choice == "I already have a Personal Access Token (PAT)":
            self._print_pat_instructions()
            self.github_token = questionary.password("Please enter your GitHub Personal Access Token (PAT):").ask()
            CLIAuthHelper.setup_github(self.github_token)
        else:
            console.print("[yellow]Skipping GitHub configuration.[/yellow]")

    def step_gcloud(self) -> None:
        console.print("\n[bold magenta]Step 2 of 5: Google Cloud / Gemini API[/bold magenta]")
        console.print("─────────────────────────────────────────")
        CLIAuthHelper.setup_gcloud()

    def step_coding_tools(self) -> None:
        console.print("\n[bold magenta]Step 3 of 5: Coding Tools Configuration[/bold magenta]")
        console.print("────────────────────────────────────────")
        
        # Detect tools
        with console.status("[cyan]Scanning PATH for installed CLI coding tools...[/cyan]"):
            tools = ToolDetector.detect_all()

        table = Table(title="Detected CLI Tools")
        table.add_column("Tool", style="cyan")
        table.add_column("Installed", style="green")
        table.add_column("Version", style="yellow")
        table.add_column("Status / Details", style="white")

        for name, details in tools.items():
            inst_str = "✅ Yes" if details["installed"] else "❌ No"
            auth_str = "Authenticated" if details["authenticated"] else "Requires Auth"
            if not details["installed"]:
                auth_str = "-"
            table.add_row(
                name.upper(),
                inst_str,
                details["version"] or "-",
                details["details"] or auth_str
            )
        console.print(table)

        # Select preferred tool
        available_tools = [name for name, details in tools.items() if details["installed"] and name not in ["git", "gcloud"]]
        
        if not available_tools:
            console.print("[yellow]⚠️  No coding CLIs (claude, cursor, etc.) detected. Using API fallback mode.[/yellow]")
            self.preferred_tool = questionary.select(
                "Select preferred coding tool (will run in API mode):",
                choices=["claude", "gemini", "codex"]
            ).ask()
            settings.set("coding_tools.mode", "api")
            if self.preferred_tool == "claude":
                CLIAuthHelper.setup_claude()
            elif self.preferred_tool == "gemini":
                CLIAuthHelper.setup_gemini_cli()
            elif self.preferred_tool == "codex":
                CLIAuthHelper.setup_codex()
        else:
            self.preferred_tool = questionary.select(
                "Select your preferred coding tool for the Builder agent:",
                choices=available_tools
            ).ask()
            settings.set("coding_tools.mode", "cli")
            
            # Auth preferred tool if needed
            if self.preferred_tool == "claude":
                CLIAuthHelper.setup_claude()
            elif self.preferred_tool == "cursor":
                CLIAuthHelper.setup_cursor()
            elif self.preferred_tool == "gemini":
                CLIAuthHelper.setup_gemini_cli()
            elif self.preferred_tool == "codex":
                CLIAuthHelper.setup_codex()

        settings.set("coding_tools.preferred", self.preferred_tool)
        # Select fallback
        fallbacks = ["gemini", "claude", "codex"]
        if self.preferred_tool in fallbacks:
            fallbacks.remove(self.preferred_tool)
        fallback = questionary.select("Select fallback coding tool if preferred fails:", choices=fallbacks).ask()
        settings.set("coding_tools.fallback", fallback)
        settings.save()

    def step_repository(self) -> None:
        console.print("\n[bold magenta]Step 4 of 5: Target Repository Selector[/bold magenta]")
        console.print("──────────────────────────────────────────")
        
        token = settings.get("github.token")
        repo_choices = []
        
        if token:
            with console.status("[cyan]Fetching your GitHub repositories...[/cyan]"):
                try:
                    headers = {
                        "Authorization": f"token {token}",
                        "Accept": "application/vnd.github.v3+json"
                    }
                    response = httpx.get("https://api.github.com/user/repos?per_page=100&sort=updated", headers=headers)
                    if response.status_code == 200:
                        repos = response.json()
                        repo_choices = [r["full_name"] for r in repos]
                except Exception:
                    pass

        if repo_choices:
            repo_choices.append("Input repository manually")
            self.selected_repo = questionary.select(
                "Select target repository:",
                choices=repo_choices
            ).ask()
            
            if self.selected_repo == "Input repository manually":
                self.selected_repo = questionary.text(
                    "Enter repository in owner/repo format (e.g. facebook/react):"
                ).ask()
        else:
            self.selected_repo = questionary.text(
                "Enter repository in owner/repo format (e.g. facebook/react):"
            ).ask()

        if self.selected_repo:
            settings.set("github.repository", self.selected_repo)
            settings.save()
            
            # Setup Webhook and Label options
            console.print(f"[green]Selected Repository: {self.selected_repo}[/green]")
            setup_label = questionary.confirm("Create the 'crew:ready' issue label on GitHub if it doesn't exist?").ask()
            if setup_label:
                self.create_label_on_github()

    def create_label_on_github(self) -> None:
        token = settings.get("github.token")
        repo = settings.get("github.repository")
        if not token or not repo:
            return
            
        with console.status(f"[cyan]Creating label 'crew:ready' in {repo}...[/cyan]"):
            try:
                headers = {
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.v3+json"
                }
                label_data = {
                    "name": "crew:ready",
                    "color": "7F00FF",
                    "description": "Signals Founders.crew to pick up this issue"
                }
                response = httpx.post(f"https://api.github.com/repos/{repo}/labels", headers=headers, json=label_data)
                if response.status_code == 201:
                    console.print("[green]✅ 'crew:ready' label created successfully![/green]")
                elif response.status_code == 422: # Already exists
                    console.print("[green]✅ 'crew:ready' label already exists.[/green]")
                else:
                    console.print(f"[yellow]⚠️  Failed to create label: {response.text}[/yellow]")
            except Exception as e:
                console.print(f"[yellow]⚠️  Error connecting to GitHub API: {e}[/yellow]")

    def step_review_and_save(self) -> None:
        console.print("\n[bold magenta]Step 5 of 5: Review & Confirm[/bold magenta]")
        console.print("──────────────────────────────────")
        
        table = Table(title="Founders.crew Configuration Summary")
        table.add_column("Setting", style="cyan")
        table.add_column("Configured Value", style="green")
        
        table.add_row("GitHub Repository", settings.get("github.repository") or "[red]Not configured[/red]")
        table.add_row("GitHub Trigger Label", settings.get("github.trigger_label"))
        table.add_row("Preferred Coding Tool", settings.get("coding_tools.preferred"))
        table.add_row("Fallback Coding Tool", settings.get("coding_tools.fallback"))
        table.add_row("Coding Mode", settings.get("coding_tools.mode"))
        table.add_row("Planning Model", settings.get("agents.planning_model"))
        table.add_row("Fast Model", settings.get("agents.fast_model"))
        table.add_row("Dashboard Port", str(settings.get("dashboard.port")))
        
        console.print(table)
        
        confirm = questionary.confirm("Save these settings and complete setup?").ask()
        if confirm:
            settings.save()
            console.print("\n[bold green]🎉 Setup completed successfully![/bold green]")
            console.print("Configuration saved to ~/.founderscrew/config.yaml")
            console.print("\nYou can now start the DevOps team using:")
            console.print("👉 [bold cyan]founders-crew start[/bold cyan]\n")
        else:
            console.print("[yellow]Setup cancelled. Settings were not saved permanently.[/yellow]")
