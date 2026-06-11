import os
import time
import webbrowser
import httpx
from rich.console import Console
from founderscrew.config import settings

# Default Client ID for the Founders.crew OAuth App.
# If using a custom GitHub App, this can be overridden.
# We default to a standard public client ID or a placeholder.
DEFAULT_CLIENT_ID = "0177203b57b6960b78d2"  # Example client ID

console = Console()

class GitHubDeviceFlow:
    """Implements GitHub OAuth Device Authorization Flow (RFC 8628)."""

    def __init__(self, client_id: str = None):
        self.client_id = client_id or os.getenv("GITHUB_CLIENT_ID") or settings.get("github.client_id") or DEFAULT_CLIENT_ID
        self.scopes = "repo,workflow,write:discussion,admin:repo_hook"

    def request_device_code(self) -> dict:
        """Step 1: Request device and user codes from GitHub."""
        url = "https://github.com/login/device/code"
        headers = {"Accept": "application/json"}
        payload = {
            "client_id": self.client_id,
            "scope": self.scopes
        }
        
        try:
            response = httpx.post(url, headers=headers, data=payload)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise RuntimeError(
                    "GitHub returned a 404 error. This is because the DEFAULT_CLIENT_ID is a placeholder. "
                    "Please register your own GitHub OAuth App to use device flow, or use the 'Personal Access Token' option."
                )
            raise e

    def poll_for_token(self, device_code: str, interval: int, expires_in: int) -> str:
        """Step 2: Poll GitHub token endpoint until user authorizes or code expires."""
        url = "https://github.com/login/oauth/access_token"
        headers = {"Accept": "application/json"}
        payload = {
            "client_id": self.client_id,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
        }

        start_time = time.time()
        while time.time() - start_time < expires_in:
            response = httpx.post(url, headers=headers, data=payload)
            response.raise_for_status()
            data = response.json()

            if "access_token" in data:
                return data["access_token"]
            
            error = data.get("error")
            if error == "authorization_pending":
                # User has not authorized yet, wait and try again
                time.sleep(interval)
            elif error == "slow_down":
                # We need to increase polling interval
                interval += 5
                time.sleep(interval)
            elif error in ["expired_token", "access_denied"]:
                raise Exception(f"GitHub Auth failed: {error}")
            else:
                raise Exception(f"Unexpected GitHub auth response: {data}")

        raise TimeoutError("GitHub device authorization flow timed out.")

    def run(self) -> str:
        """Orchestrate the full device flow process."""
        try:
            code_data = self.request_device_code()
            user_code = code_data["user_code"]
            verification_uri = code_data["verification_uri"]
            device_code = code_data["device_code"]
            interval = code_data.get("interval", 5)
            expires_in = code_data.get("expires_in", 900)

            console.print("\n[bold cyan]Step 1: GitHub Connection[/bold cyan]")
            console.print("Opening your browser to authorize Founders.crew...")
            console.print(f"If the browser doesn't open automatically, visit:")
            console.print(f"👉 [bold underline blue]{verification_uri}[/bold underline blue]")
            console.print(f"And enter this code: [bold yellow]{user_code}[/bold yellow]\n")

            # Try to open the browser
            try:
                webbrowser.open(verification_uri)
            except Exception:
                pass

            with console.status("[bold green]Waiting for GitHub authorization...[/bold green]", spinner="dots"):
                token = self.poll_for_token(device_code, interval, expires_in)

            return token
        except Exception as e:
            console.print(f"[bold red]GitHub Authentication failed: {e}[/bold red]")
            raise e
