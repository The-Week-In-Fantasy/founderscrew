# 🎥 Founders.crew — Hackathon Demo Script (≤ 3 Minutes)

This script maps out a 3-minute walkthrough video of Founders.crew for the Google for Startups AI Agents Challenge.

---

## Part 1: Introduction & Zero-Friction Setup (0:00 - 0:45)

* **Visual**: Show the slide or title screen: **Founders.crew — Virtual DevOps Team for Startup Founders**. Show yourself in terminal.
* **Audio/Speech**:
  > "Hi! Most DevOps automation tools are built assuming you are a senior platform engineer comfortable with complex YAML files and environment variables.
  > But what if you are a non-technical founder or a product manager?
  >
  > Enter Founders.crew: an agentic DevOps team powered by Google ADK that autonomously triages GitHub issues, plans fixes, builds code, tests, and deploys — all controlled from a beautiful web dashboard.
  >
  > Onboarding is completely frictionless. Running `founders-crew setup` launches an interactive 5-step wizard. It connects your GitHub via OAuth device flow, configures your Google Cloud project for Gemini 2.5, and detects local editors like Claude Code or Cursor. No copy-pasting API keys into text files required.
  >
  > Let's run `founders-crew doctor` to verify our system health. Everything is green and ready!"

---

## Part 2: The Dashboard & Event-Driven Triage (0:45 - 1:30)

* **Visual**: Launch `founders-crew start` in terminal. Transition to browser showing the beautiful Founders.crew dashboard running locally. Point out the dark mode, cards list.
* **Audio/Speech**:
  > "We start the server and open the web dashboard.
  > On GitHub, a user opens an issue reporting a buggy loop, and applies the label `crew:ready`.
  >
  > Because Cloud Run is stateless, we built an event-driven orchestrator. The GitHub webhook immediately wakes up our Triage Agent. Using Gemini 2.5 Flash, it classifies the issue, scans the codebase to find affected files, and determines complexity.
  >
  > It then hands off to the Planner Agent, powered by Gemini 2.5 Pro. The planner studies the code, drafts a step-by-step fix, and posts the plan directly to the GitHub issue, transitioning the dashboard state to 'Awaiting Plan Approval'."

---

## Part 3: Coding, Testing, and QA (1:30 - 2:30)

* **Visual**: Click 'Approve Plan' button on the dashboard UI, or comment 'approve' on GitHub. The dashboard updates live showing state transition to 'Building' and then 'Testing'.
* **Audio/Speech**:
  > "I can approve the plan right here from the dashboard or by commenting 'lgtm' on GitHub.
  >
  > Once approved, the Builder Agent wakes up. It uses a pluggable Dual-Mode Coding Adapter. Locally, it can invoke Claude or Cursor CLI subprocesses. In headless Cloud Run production, it uses direct Gemini API calls to apply the changes safely.
  >
  > The Tester Agent runs our test suites in a secure subprocess. If a test fails, the builder automatically corrects the code. Here, the tests passed!
  >
  > Next, our QA Agent performs final validation, generating visual screenshots of the application. The system transitions to 'Awaiting QA Approval' with a full report."

---

## Part 4: A2A Interoperability & PR Merge (2:30 - 3:00)

* **Visual**: Point out the QA report observations on the dashboard. Click 'Approve QA'. Show the GitHub Pull Request that was created by the Deployer Agent. Show the QA Agent Card at `/.well-known/agent-card.json`.
* **Audio/Speech**:
  > "For Agent-to-Agent (A2A) interoperability, our QA Agent runs as a distinct endpoint at `/api/v1/a2a/qa`, communicating via standard JSON-RPC 2.0. The builder calls the QA agent over HTTP, adhering to the hackathon's A2A specifications.
  >
  > I approve the QA report, and the Deployer Agent immediately creates a feature branch, commits the verified fixes, and opens a Pull Request on GitHub with the entire build context, plan, and test outcomes.
  >
  > Once merged, the issue is closed.
  >
  > Founders.crew delivers autonomous, human-in-the-loop DevOps for everyone, built natively on Google ADK and Cloud Run. Thanks for watching!"
