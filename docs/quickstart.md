# 🧙 Founders.crew — Quickstart Guide (3-Minute Onboarding)

Welcome to **Founders.crew**! This guide will walk you through setting up your AI-powered Virtual DevOps Team in under 3 minutes, even if you are a non-technical founder or product manager.

---

## 🛠️ Step 1: Install Founders.crew

Ensure you have Python 3.10 or higher installed. Install the package inside a virtual environment to isolate its dependencies:

```bash
git clone https://github.com/your-username/founderscrew.git
cd founderscrew

# Create a virtual environment
python -m venv .venv

# Activate the virtual environment
# On Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# On Windows (CMD):
.\.venv\Scripts\activate.bat
# On macOS/Linux:
source .venv/bin/activate

# Install package in editable mode
pip install -e .
```

---

## 🧙 Step 2: Run the Setup Wizard

The interactive setup wizard will guide you through connecting your tools, GitHub repository, and Google Cloud credentials.

Run this command:
```bash
founders-crew setup
```

### What the wizard handles:
| Step | Action | Why it's easy |
| :--- | :--- | :--- |
| **1. GitHub Connection** | Authenticates your GitHub account | Opens a browser for OAuth authorization. No copying fine-grained Personal Access Tokens (PATs). |
| **2. Google Cloud / Gemini** | Connects to Vertex AI / Gemini | Autodetects `gcloud` login or prompts for a Gemini API key. |
| **3. Coding Tools** | Scans and configures local agents | Autodetects `claude` CLI, `cursor` CLI, and configures API modes/preferences. |
| **4. Target Repository** | Selects your GitHub project | Shows a list of your recent repositories to choose from and autoinstalls the `crew:ready` label. |
| **5. Confirm & Save** | Saves configuration | Reviews the setup and saves it to `~/.founderscrew/config.yaml`. |

---

## 🏥 Step 3: Run the Health Check

Validate that everything is configured correctly:

```bash
founders-crew doctor
```

If any checks fail (indicated by ❌ or ⚠️), follow the troubleshooting tips shown in the command output.

---

## 🚀 Step 4: Start the Team

Launch the Webhook & Dashboard server:

```bash
founders-crew start
```

This starts a server on `http://localhost:8080` (or your configured port):
- **Dashboard**: Track active tasks, see planning details, and review code fixes.
- **Webhook**: Receives issue update notifications from GitHub.

---

## 🤖 How to use your DevOps Team

1. Go to your target repository on GitHub.
2. Create or find an issue you want the team to fix.
3. Add the label **`crew:ready`** to the issue.
4. The team will start working:
   - **Triage**: Classifies the issue and finds affected files.
   - **Planning**: Creates a plan and posts it as a comment on the issue for you to approve (via reaction or comment).
   - **Building**: Automatically writes code using your preferred tool.
   - **Testing**: Runs the tests and captures screenshots.
   - **PR Creation**: Submits a Pull Request with all context attached.
