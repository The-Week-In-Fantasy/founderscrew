import click
from rich.console import Console

console = Console()

@click.group()
def cli():
    """Founders.crew: Virtual DevOps Team manager."""
    pass

@cli.command()
@click.option("--step", type=click.Choice(["github", "gcloud", "tools", "repository"]), help="Run a specific setup step.")
def setup(step):
    """Run the interactive configuration setup wizard."""
    from founderscrew.setup.wizard import SetupWizard
    wizard = SetupWizard()
    
    if step:
        if step == "github":
            wizard.step_github()
        elif step == "gcloud":
            wizard.step_gcloud()
        elif step == "tools":
            wizard.step_coding_tools()
        elif step == "repository":
            wizard.step_repository()
    else:
        wizard.run()

@cli.command()
def doctor():
    """Run system diagnostic health checks."""
    from founderscrew.setup.doctor import Doctor
    Doctor.diagnose()

@cli.command()
@click.option("--port", default=None, type=int, help="Port to run the dashboard/webhook server on.")
@click.option("--headless", is_flag=True, default=False, help="Run without launching browser or interactive outputs.")
@click.option("--reload", is_flag=True, default=False, help="Enable Uvicorn auto-reload for local web development.")
def start(port, headless, reload):
    """Start the Founders.crew Webhook & Dashboard server."""
    import uvicorn
    from founderscrew.config import settings, CONFIG_FILE
    
    # Check if config exists, if not, redirect to setup wizard
    if not CONFIG_FILE.exists():
        console.print("[yellow]⚠️  Configuration file not found. Launching setup wizard first...[/yellow]")
        from founderscrew.setup.wizard import SetupWizard
        SetupWizard().run()
        
    p = port or settings.get("dashboard.port", 8080)
    console.print(f"\n[bold green]🚀 Starting Founders.crew Webhook & Dashboard on http://localhost:{p}[/bold green]")
    console.print("[dim]Agent workflows run in a separate worker process. Start one with: founders-crew worker[/dim]")
    
    # Launch uvicorn server pointing to our dashboard app
    # We will import the app dynamically to prevent early loading issues
    uvicorn.run("founderscrew.dashboard.app:app", host="0.0.0.0", port=p, reload=reload)

@cli.command()
@click.option("--once", is_flag=True, default=False, help="Claim and run a single queued workflow job, then exit.")
@click.option("--poll-interval", default=5.0, type=float, help="Seconds to wait between queue polls.")
@click.option("--lease-seconds", default=3600, type=int, help="Seconds before a running job can be reclaimed.")
def worker(once, poll_interval, lease_seconds):
    """Run the Founders.crew workflow worker."""
    import asyncio
    from founderscrew.worker import run_worker_loop, run_worker_once

    if once:
        ran = asyncio.run(run_worker_once(lease_seconds=lease_seconds))
        if not ran:
            console.print("[yellow]No queued workflow jobs found.[/yellow]")
        return

    console.print("[bold green]⚙️  Starting Founders.crew workflow worker[/bold green]")
    asyncio.run(run_worker_loop(poll_interval=poll_interval, lease_seconds=lease_seconds))

if __name__ == "__main__":
    cli()
