# 🏗️ Founders.crew — Technical Architecture Details

This document outlines the detailed architecture and components of the Founders.crew AI Virtual DevOps Team.

---

## 1. System Layers

Founders.crew consists of four major operational layers:

### A. The Core Infrastructure Layer
- **Layered Config (`config.py`)**: Resolves configurations sequentially: built-in defaults -> user config (`~/.founderscrew/config.yaml`) -> local `.env` -> environment variables. Implements OS-level credential management via `keyring`.
- **State Store (`state/store.py`)**: Abstract persistence layer. Local environments use a SQLite database. Cloud production environments use Google Cloud Firestore to maintain state in stateless containers.

### B. The Multi-Agent Layer
Managed using **Google Agent Development Kit (ADK)**:
- **TriageAgent**: Uses Gemini 2.5 Flash to quickly classify issues and locate targets.
- **PlannerAgent**: Uses Gemini 2.5 Pro to formulate implementation steps and test strategies.
- **BuilderAgent**: Orchestrates code writing using the pluggable `CodingToolAdapter`.
- **TesterAgent**: Runs automated test suites and captures screenshots.
- **ReviewerAgent**: Analyzes diff changes and integrates CodeRabbit review comment loops.
- **QAAgent**: Compares visual outputs to find UI regressions. Exposed as an A2A service.
- **DeployerAgent**: Manages PRs, branches, and merges.

### C. The Interface Layer (Dashboard)
- **FastAPI Web App (`dashboard/app.py`)**: Serves dashboard pages, config settings forms, manual approvals, and webhook endpoints on a single port.
- **Jinja2 + HTMX**: Renders responsive dark-themed visuals. HTMX polls the server to update the workflow step timeline dynamically as agents make progress, avoiding connection leaks.

### D. The A2A Interoperability Layer
- Exposes a JSON-RPC 2.0 endpoint at `/api/v1/a2a/qa` mapping parameters to the QA Agent.
- Serves an Agent Card at `/.well-known/agent-card.json` containing metadata, models, and capabilities.

---

## 2. Event-Driven Workflow State Machine

Traditional sequential agents block execution until complete. Because DevOps workflows include hours/days of human gates (e.g. waiting for plan reviews or visual approvals), a blocking loop fails in serverless environments. 

Founders.crew resolves this by implementing a **state-machine orchestrator**:

```
[Issue Labeled] 
      │
      ▼
   [Triage] ──► [Planning] ──► [Post Plan to GitHub] ──► [AWAIT_PLAN_APPROVAL] (Suspended)
                                                                │
                                    ┌───────────────────────────┘
                                    ▼ (Human comment 'approve')
   [Building] ──► [Testing] ──► [Reviewing] ──► [QA visual checks] ──► [AWAIT_QA_APPROVAL] (Suspended)
                                                                             │
                                         ┌───────────────────────────────────┘
                                         ▼ (Human click 'Approve QA')
   [Deploying (PR Open)] ──► [AWAIT_PR_APPROVAL] ──► [PR Merged] ──► [MERGED / DONE]
```

At each suspended gate, the server stops execution and saves state. When a webhook comment or manual click occurs, the Orchestrator reloads state from the database, restores progress, and fires the next agent step.
