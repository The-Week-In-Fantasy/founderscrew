import os
from pathlib import Path
import yaml
from dotenv import load_dotenv
import keyring
import json

# Load environment variables from .env if present
load_dotenv(override=True)

USER_HOME_DIR = Path.home()
CONFIG_DIR = USER_HOME_DIR / ".founderscrew"
CONFIG_FILE = CONFIG_DIR / "config.yaml"

# Default configuration dictionary
DEFAULT_CONFIG = {
    "github": {
        "repository": "",
        "trigger_label": "crew:ready",
        "token": "",
        "base_branch": "main",
    },
    "coding_tools": {
        "preferred": "claude",
        "fallback": "gemini",
        "mode": "cli",
        "tier1": "claude",
        "tier2": "cursor",
        "tier3": "gemini",
    },
    "agents": {
        "planning_model": "gemini-2.5-pro",
        "fast_model": "gemini-2.5-flash",
        "max_retries": 2,
        "fast_tier1": "gemini-2.5-flash",
        "fast_tier2": "gemini-2.5-pro",
        "fast_tier3": "openai/gpt-4o-mini",
        "planning_tier1": "gemini-2.5-pro",
        "planning_tier2": "gemini-2.5-flash",
        "planning_tier3": "anthropic/claude-3-5-sonnet",
    },
    "dashboard": {
        "port": 8080,
        "theme": "dark",
    },
    "discord": {
        "webhook_url": "",
    },
    "workspace_env": {}
}

class Config:
    def __init__(self):
        self.config = self._load_config()

    def _load_config(self) -> dict:
        # Start with default config
        cfg = {}
        for section, values in DEFAULT_CONFIG.items():
            cfg[section] = values.copy()

        # Load from ~/.founderscrew/config.yaml if it exists
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    user_cfg = yaml.safe_load(f) or {}
                # Deep merge user config
                for section, values in user_cfg.items():
                    if section in cfg and isinstance(cfg[section], dict) and isinstance(values, dict):
                        cfg[section].update(values)
                    else:
                        cfg[section] = values
            except Exception as e:
                print(f"Warning: Failed to load user config file {CONFIG_FILE}: {e}")

        # Override/augment with credentials from keyring or env
        # GitHub Token
        github_token = os.getenv("GITHUB_TOKEN")
        if not github_token:
            try:
                github_token = keyring.get_password("founderscrew", "github_token")
            except Exception:
                pass
        if github_token:
            cfg["github"]["token"] = github_token

        # Direct env overrides
        if os.getenv("FOUNDERSCREW_REPO"):
            cfg["github"]["repository"] = os.getenv("FOUNDERSCREW_REPO")
        if os.getenv("GITHUB_TRIGGER_LABEL"):
            cfg["github"]["trigger_label"] = os.getenv("GITHUB_TRIGGER_LABEL")
        if os.getenv("GITHUB_BASE_BRANCH"):
            cfg["github"]["base_branch"] = os.getenv("GITHUB_BASE_BRANCH")
        
        # Google API keys / gcloud project (merge env variables or keyring values without overwriting loaded values)
        if "google" not in cfg:
            cfg["google"] = {"api_key": "", "project_id": ""}
            
        google_key = os.getenv("GOOGLE_API_KEY")
        if not google_key:
            try:
                google_key = keyring.get_password("founderscrew", "google_api_key")
            except Exception:
                pass
        if google_key:
            cfg["google"]["api_key"] = google_key

        if os.getenv("GOOGLE_CLOUD_PROJECT"):
            cfg["google"]["project_id"] = os.getenv("GOOGLE_CLOUD_PROJECT")
        elif os.getenv("GCP_PROJECT"):
            cfg["google"]["project_id"] = os.getenv("GCP_PROJECT")
            
        # Support environment overrides/keyring for OpenAI and Anthropic keys
        if "coding_tools" not in cfg:
            cfg["coding_tools"] = {}
            
        openai_key = os.getenv("OPENAI_API_KEY")
        if not openai_key:
            try:
                openai_key = keyring.get_password("founderscrew", "openai_api_key")
            except Exception:
                pass
        if openai_key:
            cfg["coding_tools"]["openai_api_key"] = openai_key

        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if not anthropic_key:
            try:
                anthropic_key = keyring.get_password("founderscrew", "anthropic_api_key")
            except Exception:
                pass
        if anthropic_key:
            cfg["coding_tools"]["anthropic_api_key"] = anthropic_key
        
        # Discord webhook env override
        discord_webhook = os.getenv("DISCORD_WEBHOOK_URL")
        if discord_webhook:
            cfg["discord"]["webhook_url"] = discord_webhook

        # Workspace env from keyring
        try:
            stored_env_json = keyring.get_password("founderscrew", "workspace_env")
            if stored_env_json:
                cfg["workspace_env"] = json.loads(stored_env_json)
        except Exception:
            pass

        return cfg

    def get(self, key_path: str, default=None):
        """Get config value using dot notation, e.g. config.get('github.repository')."""
        parts = key_path.split(".")
        val = self.config
        for part in parts:
            if isinstance(val, dict) and part in val:
                val = val[part]
            else:
                return default
        return val

    def set(self, key_path: str, value) -> None:
        """Set config value using dot notation and update the in-memory dict."""
        parts = key_path.split(".")
        val = self.config
        for part in parts[:-1]:
            if part not in val or not isinstance(val[part], dict):
                val[part] = {}
            val = val[part]
        val[parts[-1]] = value

    def save(self) -> None:
        """Save current configuration to ~/.founderscrew/config.yaml."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Avoid saving raw sensitive tokens to plain text config.yaml if keyring is used
        # We can store the token in the keyring, and other settings in config.yaml
        save_cfg = {}
        for section, values in self.config.items():
            save_cfg[section] = values.copy()

        # Save token to keyring if it exists and keyring is functional
        token = save_cfg.get("github", {}).pop("token", None)
        if token:
            try:
                keyring.set_password("founderscrew", "github_token", token)
            except Exception as e:
                # If keyring is not functional, we fall back to saving token in config.yaml
                save_cfg["github"]["token"] = token

        # Save google api key to keyring
        google_key = save_cfg.get("google", {}).pop("api_key", None)
        if google_key:
            try:
                keyring.set_password("founderscrew", "google_api_key", google_key)
            except Exception:
                if "google" not in save_cfg:
                    save_cfg["google"] = {}
                save_cfg["google"]["api_key"] = google_key

        # Save openai api key to keyring
        openai_key = save_cfg.get("coding_tools", {}).pop("openai_api_key", None)
        if openai_key:
            try:
                keyring.set_password("founderscrew", "openai_api_key", openai_key)
            except Exception:
                if "coding_tools" not in save_cfg:
                    save_cfg["coding_tools"] = {}
                save_cfg["coding_tools"]["openai_api_key"] = openai_key

        # Save anthropic api key to keyring
        anthropic_key = save_cfg.get("coding_tools", {}).pop("anthropic_api_key", None)
        if anthropic_key:
            try:
                keyring.set_password("founderscrew", "anthropic_api_key", anthropic_key)
            except Exception:
                if "coding_tools" not in save_cfg:
                    save_cfg["coding_tools"] = {}
                save_cfg["coding_tools"]["anthropic_api_key"] = anthropic_key

        # Save workspace env to keyring securely
        workspace_env = save_cfg.pop("workspace_env", None)
        if workspace_env is not None:
            try:
                keyring.set_password("founderscrew", "workspace_env", json.dumps(workspace_env))
            except Exception:
                # Fallback to plain text if keyring fails
                save_cfg["workspace_env"] = workspace_env

        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                yaml.safe_dump(save_cfg, f, default_flow_style=False)
        except Exception as e:
            print(f"Error: Failed to save config to {CONFIG_FILE}: {e}")

# Global config instance
settings = Config()

# Initialize logging system
from founderscrew.logging_config import setup_logging
setup_logging()

