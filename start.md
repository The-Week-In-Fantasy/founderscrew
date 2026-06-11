# 🚀 Founders.crew — Virtual DevOps Team

An agentic multi-agent DevOps team built on Google ADK + A2A Protocol that autonomously triages GitHub issues, plans fixes, writes code, tests, and creates PRs — with human-in-the-loop approval at every critical stage.

---

## Key Features

- **Wizard-based Setup**: Easy 5-step configuration wizard.
- **7-Agent DevOps Team**: Hierarchical coordination using Google ADK.
- **Pluggable Coding Tools**: Runs tasks using local CLIs (Claude, Cursor, Codex, Gemini) or API modes.
- **Interactive Dashboard**: Premium dark-theme web dashboard with real-time SSE updates and screenshots.
- **A2A Protocol Ready**: Dispatches A2A JSON-RPC messages and advertises capacities with Agent Cards.

---

## Project Structure

- `pyproject.toml` — Python project configuration and dependencies.
- `founderscrew/` — Source package containing the orchestrator, setup wizard, individual agent logic, tools, state models, webhook server, and dashboard UI.
- `docs/` — Comprehensive architecture and quickstart guides.

---

## Quickstart

To install and run:

```bash
pip install -e .
founders-crew setup
founders-crew start
```

For detailed setup instructions, see [quickstart.md](file:///g:/yerDev/docs/quickstart.md).
