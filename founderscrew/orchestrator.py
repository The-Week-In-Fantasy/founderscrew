import os
import re
import json
import asyncio
import logging
import subprocess
from urllib.parse import urljoin
from typing import Optional, Dict, Any, List
from google.adk import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService

from founderscrew.config import settings

logger = logging.getLogger("founderscrew.orchestrator")
from founderscrew.state.store import StateStore
from founderscrew.state.models import (
    WorkflowStateModel,
    WorkflowStatus,
    IssueContext,
    ImplementationPlanModel,
    PlanStep,
    TestResultsModel,
    TestOutcome,
    QAReportModel,
    QualityGateResult,
    WorkflowArtifact
)
from founderscrew.agents import (
    get_triage_agent,
    get_planner_agent,
    get_builder_agent,
    get_tester_agent,
    get_reviewer_agent,
    get_qa_agent,
    get_deployer_agent
)
from founderscrew.tools.github_tools import (
    github_get_issue,
    github_add_comment,
    github_clone_or_pull,
    github_push_workspace,
    github_prepare_workspace_branch,
    set_active_workspace_branch,
    github_get_bot_login
)
from founderscrew.tools.shell_tools import run_safe_shell_command, start_dev_server, stop_dev_server
from founderscrew.tools.screenshot_tools import analyze_screenshot, capture_screenshot, diagnose_page_render
from founderscrew.tools.repo_profile import get_repo_memory, add_repo_lesson, format_repo_memory
from founderscrew.tools.route_inference import infer_qa_route_candidates, format_route_candidates
from founderscrew.tools.model_routing import filter_available_tiers, apply_provider_env
from founderscrew.workflow_queue import WorkflowQueue
from pathlib import Path

GENERATED_ARTIFACT_PATHS = [
    ".env",
    "playwright.config.js",
    "current_page.png",
    "current_screenshot.png",
    "local_test_page.png",
]
GENERATED_ARTIFACT_DIRS = [
    ".test_home",
    ".tmp_pytest",
    ".tmp_pytest_cache",
    ".pytest_cache",
    ".founderscrew",
]

DETERMINISTIC_BUILDER_TOOL_FAILURE_MARKERS = [
    "run_coding_tool execution failed",
    "All coding tools and API fallbacks failed",
    "Refusing to send shell-command remediation",
    "SERVICE_DISABLED",
    "cloudaicompanion.googleapis.com",
    "IDEClient] Directory mismatch",
    "No modified files parsed from API response",
]

RATE_LIMIT_FAILURE_MARKERS = [
    "429",
    "RESOURCE_EXHAUSTED",
    "Too Many Requests",
    "rate limit",
    "quota",
]

class Orchestrator:
    """Orchestrates events and state transitions for the Founders.crew DevOps agent workflow."""

    def __init__(self):
        self.store = StateStore()
        self.queue = WorkflowQueue()
        # Locks keyed by session_id (approval gates) and "repo::<name>"
        # (workspace access) to guard against duplicate webhook deliveries and
        # concurrent flows trampling the shared repo workspace
        self._locks: Dict[str, asyncio.Lock] = {}

    def _get_lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def _fail(self, state: WorkflowStateModel, stage: str, message: str) -> None:
        """Marks a workflow failed, recording which stage failed for resumption."""
        state.status = WorkflowStatus.FAILED
        state.failed_stage = stage
        state.error_message = message
        self.store.save_state(state)

    def _repo_memory_text(self, repo_name: str, workdir: Optional[str] = None) -> str:
        """Returns the repo's cached profile + lessons as a prompt block ('' if unavailable)."""
        try:
            return format_repo_memory(get_repo_memory(self.store, repo_name, workdir))
        except Exception as e:
            logger.warning(f"Repo memory unavailable for {repo_name}: {e}")
            return ""

    def _workspace_file_list(self, workdir: Optional[str], limit: int = 300) -> List[str]:
        """Returns a compact local file listing for agents without hitting GitHub APIs."""
        if not workdir:
            return []
        root = Path(workdir)
        skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "coverage"}
        files: List[str] = []
        try:
            for path in root.rglob("*"):
                if len(files) >= limit:
                    break
                if any(part in skip_dirs for part in path.parts):
                    continue
                if path.is_file():
                    files.append(path.relative_to(root).as_posix())
        except Exception as e:
            logger.warning(f"Could not list workspace files for triage: {e}")
        return files

    def _as_bool(self, value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "1", "required"}:
                return True
            if normalized in {"false", "no", "0", "not_required"}:
                return False
        return default

    def _normalize_risk(self, value: Any) -> str:
        risk = str(value or "").strip().lower()
        return risk if risk in {"low", "medium", "high"} else "medium"

    def _infer_ui_qa_required(self, state: WorkflowStateModel, plan_data: Dict[str, Any]) -> bool:
        if "ui_qa_required" in plan_data:
            return self._as_bool(plan_data.get("ui_qa_required"), True)
        ui_markers = ("frontend", "ui", "visual", "page", "component", "css", "react", "vue", "svelte")
        text = f"{state.issue.title} {state.issue.body or ''} {plan_data.get('summary', '')}".lower()
        if any(marker in text for marker in ui_markers):
            return True
        return any(str(f).endswith((".jsx", ".tsx", ".css", ".scss", ".vue", ".svelte")) for f in state.issue.affected_files)

    def _infer_docs_required(self, state: WorkflowStateModel, plan_data: Dict[str, Any]) -> bool:
        if "docs_required" in plan_data:
            return self._as_bool(plan_data.get("docs_required"), False)
        text = f"{state.issue.title} {state.issue.body or ''} {plan_data.get('summary', '')}".lower()
        doc_markers = ("readme", "docs", "documentation", "setup", "install", "configuration", "deployment", "user workflow")
        return any(marker in text for marker in doc_markers)

    def _apply_plan_quality_contract(self, state: WorkflowStateModel, plan_data: Dict[str, Any]) -> None:
        criteria = plan_data.get("acceptance_criteria") or plan_data.get("criteria") or []
        if isinstance(criteria, str):
            criteria = [line.strip("- ").strip() for line in criteria.splitlines() if line.strip()]
        state.acceptance_criteria = [str(item).strip() for item in criteria if str(item).strip()]
        if not state.acceptance_criteria:
            state.acceptance_criteria = [
                f"Resolve GitHub issue #{state.issue.number}: {state.issue.title}",
                "Add or update a targeted automated test that verifies the issue is resolved.",
                "All required quality gates pass before PR creation.",
            ]
        state.risk_level = self._normalize_risk(plan_data.get("risk_level") or state.issue.complexity)
        state.docs_required = self._infer_docs_required(state, plan_data)
        state.ui_qa_required = self._infer_ui_qa_required(state, plan_data)

    def _record_quality_gate(
        self,
        state: WorkflowStateModel,
        name: str,
        command: str,
        passed: bool,
        output: str = "",
        attempt: int = 1,
        artifact_paths: Optional[List[str]] = None,
        remediation_summary: str = "",
    ) -> QualityGateResult:
        gate = QualityGateResult(
            name=name,
            command=command,
            passed=passed,
            output=(output or "")[:4000],
            attempt=attempt,
            artifact_paths=artifact_paths or [],
            remediation_summary=remediation_summary,
        )
        state.quality_gates.append(gate)
        state.quality_gates = state.quality_gates[-40:]
        for path in gate.artifact_paths:
            if path and not any(a.path == path for a in state.artifacts):
                state.artifacts.append(WorkflowArtifact(kind=name, path=path, description=f"{name} artifact"))
        self.store.save_state(state)
        return gate

    def _quality_status(self, state: WorkflowStateModel) -> str:
        if state.status == WorkflowStatus.FAILED:
            return "Failed with diagnosis"
        if state.status in {WorkflowStatus.AWAIT_PR_APPROVAL, WorkflowStatus.MERGED}:
            return "Ready for PR"
        if any(not gate.passed for gate in state.quality_gates[-5:]):
            return "Self-healing"
        if state.status in {WorkflowStatus.AWAIT_PLAN_APPROVAL, WorkflowStatus.AWAIT_QA_APPROVAL}:
            return "Needs founder decision"
        return "Self-healing"

    def _build_final_evidence_summary(self, state: WorkflowStateModel) -> str:
        gate_lines = [
            f"- {gate.name}: {'PASS' if gate.passed else 'FAIL'}"
            + (f" (`{gate.command}`)" if gate.command else "")
            for gate in state.quality_gates[-12:]
        ]
        criteria_lines = [f"- {item}" for item in state.acceptance_criteria]
        artifact_lines = [f"- {artifact.kind}: {artifact.path}" for artifact in state.artifacts[-10:]]
        parts = [
            f"Quality status: {self._quality_status(state)}",
            "Acceptance criteria:\n" + ("\n".join(criteria_lines) if criteria_lines else "- Not recorded"),
            "Quality gates:\n" + ("\n".join(gate_lines) if gate_lines else "- No gates recorded"),
        ]
        if artifact_lines:
            parts.append("Artifacts:\n" + "\n".join(artifact_lines))
        if state.build_summaries:
            parts.append("Build notes:\n" + "\n".join(f"- {s}" for s in state.build_summaries[-6:]))
        return "\n\n".join(parts)

    def _latest_gate_by_name(self, state: WorkflowStateModel) -> Dict[str, QualityGateResult]:
        latest: Dict[str, QualityGateResult] = {}
        for gate in state.quality_gates:
            latest[gate.name] = gate
        return latest

    def _required_gate_failures(self, state: WorkflowStateModel) -> List[str]:
        latest = self._latest_gate_by_name(state)
        required = ["targeted_tests", "lint", "typecheck", "docs", "artifact_hygiene"]
        if state.ui_qa_required:
            required.append("visual_qa")
        missing_or_failed = []
        for name in required:
            gate = latest.get(name)
            if not gate or not gate.passed:
                missing_or_failed.append(name)
        return missing_or_failed

    def _enqueue_stage(self, session_id: str, stage: str, payload: Optional[Dict[str, Any]] = None) -> str:
        job_id = self.queue.enqueue(session_id, stage, payload or {})
        logger.info(f"Queued workflow stage {stage} for {session_id} as job {job_id}.")
        return job_id

    def _add_issue_comment(self, repo_name: str, issue_number: int, body: str) -> bool:
        """Posts an issue comment without letting GitHub failures crash a stage."""
        try:
            github_add_comment(repo_name, issue_number, body)
            return True
        except Exception as e:
            logger.warning(f"Failed to add GitHub comment to {repo_name}#{issue_number}: {e}")
            return False

    def _summarize_agent_error(self, agent_name: str, model_tier: str, error_msg: str) -> str:
        text = (error_msg or "").strip()
        if any(marker.lower() in text.lower() for marker in RATE_LIMIT_FAILURE_MARKERS):
            return (
                f"{agent_name} hit a provider rate limit/quota error on {model_tier}; "
                "falling back to the next configured tier if available."
            )
        if self._is_deterministic_builder_tool_failure(agent_name, text):
            return (
                "Builder stopped on a deterministic coding-tool infrastructure failure. "
                f"Root cause: {self._first_matching_failure_marker(text) or text[:500]}"
            )
        return text[:1000] if text else "No error message provided."

    def _is_deterministic_builder_tool_failure(self, agent_name: str, error_msg: str) -> bool:
        if agent_name != "BuilderAgent":
            return False
        return any(marker in (error_msg or "") for marker in DETERMINISTIC_BUILDER_TOOL_FAILURE_MARKERS)

    def _first_matching_failure_marker(self, error_msg: str) -> str:
        for marker in DETERMINISTIC_BUILDER_TOOL_FAILURE_MARKERS:
            if marker in (error_msg or ""):
                return marker
        return ""

    def _join_qa_url(self, base_url: str, path: str) -> str:
        normalized_path = (path or "/").strip() or "/"
        if normalized_path.startswith("http://") or normalized_path.startswith("https://"):
            return normalized_path
        if not normalized_path.startswith("/"):
            normalized_path = "/" + normalized_path
        return urljoin(base_url.rstrip("/") + "/", normalized_path.lstrip("/"))

    def _plan_files(self, state: WorkflowStateModel) -> List[str]:
        files: List[str] = []
        if state.plan:
            for step in state.plan.steps:
                files.extend(step.files_affected or [])
        return files

    def _is_qa_tooling_or_route_failure(self, qa_issue_text: str) -> bool:
        text = (qa_issue_text or "").lower()
        markers = [
            "unsupported qa navigation route",
            "refused to navigate outside",
            "founderscrew qa visual report",
            "founderscrew qa visual",
            "placeholder",
            "real capture failed",
            "browser tool failed",
        ]
        return any(marker in text for marker in markers)

    def _qa_diagnostic_text(
        self,
        qa_url: str,
        screenshot_analysis: Optional[Dict[str, Any]] = None,
        render_diagnostics: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Formats blank-screen evidence for humans and the BuilderAgent."""
        lines = [f"URL: {qa_url}"]
        if screenshot_analysis:
            lines.extend([
                f"Screenshot blank: {screenshot_analysis.get('is_blank')}",
                f"Screenshot reason: {screenshot_analysis.get('reason') or 'not provided'}",
                f"Screenshot unique colors: {screenshot_analysis.get('unique_color_count')}",
                f"Screenshot dominant color ratio: {screenshot_analysis.get('dominant_color_ratio')}",
                f"Screenshot color variance: {screenshot_analysis.get('color_variance')}",
            ])
        if render_diagnostics:
            lines.extend([
                f"Browser diagnostics ok: {render_diagnostics.get('ok')}",
                f"HTTP status: {render_diagnostics.get('status')}",
                f"Final URL: {render_diagnostics.get('finalUrl')}",
                f"Page title: {render_diagnostics.get('title')}",
                f"Body text length: {render_diagnostics.get('bodyTextLength')}",
            ])
            if render_diagnostics.get("error"):
                lines.append(f"Diagnostic error: {render_diagnostics.get('error')}")
            console_errors = render_diagnostics.get("consoleErrors") or []
            page_errors = render_diagnostics.get("pageErrors") or []
            failed_requests = render_diagnostics.get("failedRequests") or []
            if console_errors:
                lines.append("Console warnings/errors:\n" + "\n".join(f"- {str(item)[:500]}" for item in console_errors[:10]))
            if page_errors:
                lines.append("Page errors:\n" + "\n".join(f"- {str(item)[:500]}" for item in page_errors[:5]))
            if failed_requests:
                lines.append("Failed requests:\n" + "\n".join(f"- {str(item)[:500]}" for item in failed_requests[:10]))
            body_sample = render_diagnostics.get("bodyTextSample")
            if body_sample:
                lines.append(f"Body text sample:\n{str(body_sample)[:1000]}")
        return "\n".join(str(line) for line in lines if line is not None)

    async def run_queued_stage(self, session_id: str, stage: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Worker entrypoint for executing one persisted workflow stage."""
        state = self.store.load_state(session_id)
        if not state:
            logger.warning(f"Queued stage {stage} skipped because {session_id} no longer exists.")
            return

        if stage == "triage":
            await self._run_from_triage(state)
        elif stage == "planning":
            await self._run_from_planning(state)
        elif stage == "deploy":
            await self.run_deploy_step(state)
        elif stage in {"building", "testing", "reviewing", "qa"}:
            await self.run_build_test_review_flow(state, start_at=stage)
        else:
            raise ValueError(f"Unknown workflow stage: {stage}")

    async def _run_agent(self, agent_getter, session_id: str, input_data: Any,
                         image_paths: Optional[List[str]] = None) -> str:
        """Helper to instantiate and run an ADK agent via ADK Runner, trying primary, secondary, and tertiary model tiers if needed."""
        temp_agent = agent_getter()
        agent_key = temp_agent.name.lower().replace("agent", "")
        is_fast_agent = agent_key in ["triage", "tester", "qa", "deployer"]
        
        t1_default = settings.get("agents.fast_model" if is_fast_agent else "agents.planning_model", "gemini-3.1-flash-lite" if is_fast_agent else "gemini-3.5-flash")
        
        tier1 = settings.get(f"agents.{agent_key}.tier1", settings.get("agents.fast_tier1" if is_fast_agent else "agents.planning_tier1", t1_default))
        tier2 = settings.get(f"agents.{agent_key}.tier2", settings.get("agents.fast_tier2" if is_fast_agent else "agents.planning_tier2", None))
        tier3 = settings.get(f"agents.{agent_key}.tier3", settings.get("agents.fast_tier3" if is_fast_agent else "agents.planning_tier3", None))
        
        tiers = [t for t in [tier1, tier2, tier3] if t]
        if not tiers:
            tiers = [t1_default]
            
        valid_tiers = filter_available_tiers(tiers)

        if not valid_tiers:
            raise RuntimeError(f"Agent {temp_agent.name} failed because all configured model tiers lacked their required API keys.")
            
        last_error = None
        for i, model_tier in enumerate(valid_tiers):
            logger.info(f"Attempting execution of {temp_agent.name} using model {model_tier} (Tier {i+1}/{len(tiers)})...")
            agent = agent_getter()
            adk_model = model_tier
            if adk_model.startswith("gemini/"):
                # Strip provider prefix for ADK's native Gemini runner
                agent.model = adk_model.replace("gemini/", "")
            elif "/" in adk_model:
                # Non-Gemini providers (anthropic/..., openai/...) must go
                # through LiteLLM — ADK's native registry only resolves Gemini
                from google.adk.models.lite_llm import LiteLlm
                agent.model = LiteLlm(model=adk_model)
            else:
                agent.model = adk_model
            
            # Export configured credentials for ADK/LiteLLM resolution
            apply_provider_env()

            session_service = InMemorySessionService()
            runner = Runner(agent=agent, session_service=session_service, app_name="founders-crew", auto_create_session=True)
            
            # Build the user message content from the input data, attaching any
            # images so multimodal agents (e.g. QA) can actually inspect them
            from google.genai import types as genai_types
            parts = [genai_types.Part.from_text(text=str(input_data))]
            for img in (image_paths or []):
                try:
                    parts.append(genai_types.Part.from_bytes(
                        data=Path(img).read_bytes(),
                        mime_type="image/png"
                    ))
                except Exception as img_err:
                    logger.warning(f"Could not attach image {img} to agent message: {img_err}")
            user_message = genai_types.Content(role="user", parts=parts)
            
            output = ""
            failed = False
            error_msg = ""
            try:
                async for event in runner.run_async(
                    user_id="orchestrator",
                    session_id=session_id,
                    new_message=user_message,
                ):
                    # Extract text from event.content.parts (the actual LLM response)
                    # event.output is often None because ADK clears it when message_as_output is True
                    if (
                        event.content
                        and event.content.parts
                        and not event.partial
                        and event.author != "user"
                    ):
                        for part in event.content.parts:
                            if part.text:
                                output += part.text
                    if event.error_code is not None:
                        failed = True
                        error_msg = event.error_message
                        break
            except Exception as e:
                failed = True
                error_msg = str(e)
                
            if not failed:
                logger.info(f"Agent {agent.name} succeeded with model {model_tier} (Tier {i+1})!")
                return output
            else:
                summary = self._summarize_agent_error(agent.name, model_tier, error_msg)
                logger.warning(f"Agent {agent.name} failed with model {model_tier} (Tier {i+1}). Error: {summary}")
                last_error = summary
                if self._is_deterministic_builder_tool_failure(agent.name, error_msg):
                    raise RuntimeError(
                        f"Agent {agent.name} stopped after a deterministic tool failure. "
                        f"{summary}"
                    )
                
        raise RuntimeError(f"Agent {temp_agent.name} failed all {len(tiers)} model tiers. Last error: {last_error}")

    def _parse_json_from_output(self, output: str) -> Dict[str, Any]:
        """Robustly parses a JSON block from agent text output."""
        match = re.search(r"```json\s*(.*?)\s*```", output, re.DOTALL)
        if match:
            json_str = match.group(1).strip()
        else:
            json_str = output.strip()
            
        # Clean up common LLM JSON hallucination (invalid single-quote escapes)
        json_str = json_str.replace(r"\'", "'")

        try:
            return json.loads(json_str)
        except Exception as e:
            # Fall back to the outermost {...} span — agents often wrap the
            # JSON in prose despite instructions
            start, end = json_str.find("{"), json_str.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(json_str[start:end + 1])
                except Exception:
                    pass
            logger.error(f"Failed to parse JSON from agent output: {e}. Output was: {output}")
            return {}

    async def _run_agent_json(self, agent_getter, session_id: str, input_data: Any,
                              image_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        """Runs an agent and parses its JSON output, self-healing once on failure.

        If the agent responds with prose instead of JSON (e.g. asks a
        clarifying question), it is re-prompted with its own output and told to
        make assumptions and emit only the JSON block.
        """
        output = await self._run_agent(agent_getter, session_id, input_data, image_paths=image_paths)
        data = self._parse_json_from_output(output)
        if data:
            return data

        logger.warning(f"Agent output was not parseable JSON; issuing corrective re-prompt. Output was: {str(output)[:300]}")
        corrective = (
            f"{input_data}\n\n---\n"
            f"Your previous response could not be parsed as JSON. It was:\n{str(output)[:2000]}\n\n"
            "You are operating unattended: do NOT ask questions or request more information. "
            "Make reasonable assumptions, note them in the output, and respond with ONLY the "
            "required JSON object inside a ```json fenced block."
        )
        output = await self._run_agent(agent_getter, session_id, corrective, image_paths=image_paths)
        return self._parse_json_from_output(output)

    async def handle_issue_labeled(self, repo_name: str, issue_number: int, sender: str) -> None:
        """Triggered when issue is labeled 'crew:ready'. Initiates Triage and Planning."""
        session_id = f"{repo_name.replace('/', '_')}_{issue_number}"
        logger.info(f"Initializing Founders.crew session: {session_id}")
        self.store.clear_deleted_state(session_id)
        
        # 1. Fetch issue details
        try:
            issue_details = github_get_issue(repo_name, issue_number)
        except Exception as e:
            logger.error(f"Error fetching issue details: {e}")
            return

        # 2. Build initial state
        issue_ctx = IssueContext(
            number=issue_number,
            title=issue_details["title"],
            body=issue_details["body"],
            creator=issue_details["creator"],
            labels=issue_details["labels"],
            repository=repo_name
        )
        
        state = WorkflowStateModel(
            session_id=session_id,
            issue=issue_ctx,
            status=WorkflowStatus.TRIAGE
        )
        self.store.save_state(state)
        self._enqueue_stage(session_id, "triage")

    async def _run_from_triage(self, state: WorkflowStateModel) -> None:
        session_id = state.session_id
        repo_name = state.issue.repository
        issue_number = state.issue.number
        
        # 3. Clone repository local copy and refresh the repo profile from it
        workdir = None
        try:
            workdir = github_clone_or_pull(repo_name)
        except Exception as e:
            logger.warning(f"Local clone failed: {e}")
        repo_memory = self._repo_memory_text(repo_name, workdir)

        # 4. Execute Triage
        try:
            issue_details = {
                "number": state.issue.number,
                "repository": repo_name,
                "title": state.issue.title,
                "body": state.issue.body,
                "creator": state.issue.creator,
                "labels": state.issue.labels,
                "comments": [],
                "repo_context": repo_memory,
                "repo_files": self._workspace_file_list(workdir),
            }
            triage_data = await self._run_agent_json(get_triage_agent, session_id, issue_details)

            if not triage_data:
                raise ValueError("Triage agent output could not be parsed as JSON.")

            state.issue.classification = triage_data.get("classification", "bug")
            if state.issue.classification == "enhancement":
                state.issue.classification = "minor_enhancement"
            state.issue.complexity = triage_data.get("complexity", "medium")
            state.issue.affected_files = triage_data.get("affected_files", [])
            state.status = WorkflowStatus.PLANNING
            self.store.save_state(state)
        except Exception as e:
            self._fail(state, "triage", f"Triage stage failed: {e}")
            self._add_issue_comment(repo_name, issue_number, f"❌ Triage failed: {e}")
            return

        await self._run_from_planning(state)

    async def _run_from_planning(self, state: WorkflowStateModel) -> None:
        session_id = state.session_id
        repo_name = state.issue.repository
        issue_number = state.issue.number
        
        # 5. Execute Planning — pre-read affected files so the planner
        # always has code context regardless of whether the LLM calls tools
        try:
            affected_file_contents = {}
            if state.issue.affected_files:
                try:
                    workdir = github_clone_or_pull(repo_name)
                    for f in state.issue.affected_files[:10]:
                        fpath = Path(workdir) / f
                        if fpath.exists() and fpath.is_file():
                            try:
                                content = fpath.read_text(encoding='utf-8', errors='replace')
                                # Truncate large files to keep the prompt manageable
                                if len(content) > 6000:
                                    content = content[:6000] + '\n... [truncated]'
                                affected_file_contents[f] = content
                            except Exception:
                                pass
                except Exception as e:
                    logger.warning(f"Could not pre-read affected files for planner: {e}")

            file_context = ""
            if affected_file_contents:
                file_context = "\nAFFECTED FILE CONTENTS (pre-read for reference):\n"
                for fname, content in affected_file_contents.items():
                    file_context += f"\n--- {fname} ---\n{content}\n"

            planner_input = {
                "issue": state.issue.model_dump(),
                "repo_context": self._repo_memory_text(repo_name),
                "affected_file_contents": file_context,
            }
            plan_data = await self._run_agent_json(get_planner_agent, session_id, planner_input)

            if not plan_data:
                raise ValueError("Planner agent output could not be parsed as JSON.")
                
            steps = []
            for step in plan_data.get("steps", []):
                steps.append(PlanStep(
                    step_number=step.get("step_number"),
                    description=step.get("description"),
                    files_affected=step.get("files_affected", []),
                    status="pending"
                ))
                
            state.plan = ImplementationPlanModel(
                summary=plan_data.get("summary", "No summary provided"),
                steps=steps,
                approved=False
            )
            self._apply_plan_quality_contract(state, plan_data)
            state.status = WorkflowStatus.AWAIT_PLAN_APPROVAL
            self.store.save_state(state)
            
            # Post plan to GitHub
            comment_body = (
                f"### 📋 Founders.crew Implementation Plan\n\n"
                f"{state.plan.summary}\n\n"
                f"#### Proposed Steps:\n"
            )
            for step in state.plan.steps:
                files_str = ", ".join([f"`{f}`" for f in step.files_affected])
                comment_body += f"- **Step {step.step_number}**: {step.description} (Affects: {files_str})\n"
            criteria = "\n".join(f"- {criterion}" for criterion in state.acceptance_criteria)
            comment_body += (
                f"\n#### Definition of Done\n"
                f"{criteria}\n\n"
                f"#### Quality Contract\n"
                f"- Risk level: `{state.risk_level}`\n"
                f"- UI QA required: `{state.ui_qa_required}`\n"
                f"- Docs required: `{state.docs_required}`\n"
                f"- Required gates: targeted tests, lint if configured, type-check if configured, docs check, artifact hygiene, code review"
                f"{', visual QA' if state.ui_qa_required else ''}\n"
            )
            if state.risk_level == "high" or state.issue.classification == "not_safe_for_autonomy":
                comment_body += (
                    "\n> ⚠️ This issue was classified as high risk or not safe for autonomy. "
                    "Founders.crew will wait for explicit founder approval before any implementation work.\n"
                )
                
            comment_body += (
                f"\n---\n"
                f"👉 **Founder Approval Required:** Please reply with **approve** or **lgtm** to begin building."
            )
            self._add_issue_comment(repo_name, issue_number, comment_body)

        except Exception as e:
            self._fail(state, "planning", f"Planning stage failed: {e}")
            self._add_issue_comment(repo_name, issue_number, f"❌ Planning failed: {e}")

    def _is_authorized_approver(self, state: WorkflowStateModel, commenter: str) -> bool:
        """Only the issue creator, repo owner, or configured approvers may approve."""
        if not commenter:
            return False
        if commenter == "dashboard_user":
            return True  # dashboard actions sit behind the dashboard's own auth
        allowed = settings.get("github.authorized_approvers", [])
        if isinstance(allowed, str):
            allowed = [a.strip() for a in allowed.split(",") if a.strip()]
        if not isinstance(allowed, (list, tuple, set)):
            allowed = []
        repo_owner = state.issue.repository.split("/")[0]
        return commenter in allowed or commenter == state.issue.creator or commenter == repo_owner

    async def handle_comment_created(self, repo_name: str, issue_number: int, comment_body: str, commenter: str) -> None:
        """Triggered when comment is added. Checks for approvals at gated stages."""
        # Never let the crew's own comments trip the approval gates — the
        # bot's "reply with **approve**" instructions contain the keyword
        try:
            if commenter and commenter == github_get_bot_login():
                return
        except Exception:
            pass

        # Approval must be stated up-front, not merely mentioned ("I don't
        # approve" must not deploy)
        comment_clean = (comment_body or "").strip().lower()
        is_approval = bool(re.match(r"^(approve|approved|lgtm)\b", comment_clean))
        if not is_approval:
            return

        session_id = f"{repo_name.replace('/', '_')}_{issue_number}"
        # Session lock: duplicate webhook deliveries or rapid double comments
        # must not trigger the same transition twice
        async with self._get_lock(session_id):
            state = self.store.load_state(session_id)
            if not state:
                return

            if not self._is_authorized_approver(state, commenter):
                logger.info(f"Ignoring approval from unauthorized commenter '{commenter}' on {session_id}.")
                return

            # 1. Handle Plan Approval Gate
            if state.status == WorkflowStatus.AWAIT_PLAN_APPROVAL:
                logger.info(f"Plan approved by {commenter} for issue {issue_number}")
                state.plan.approved = True
                state.status = WorkflowStatus.BUILDING
                self.store.save_state(state)

                self._add_issue_comment(repo_name, issue_number, "⚡ Plan approved! Starting the coding and testing cycle...")
                self._enqueue_stage(session_id, "building")

            # 2. Handle QA Approval Gate
            elif state.status == WorkflowStatus.AWAIT_QA_APPROVAL:
                logger.info(f"QA report approved by {commenter} for issue {issue_number}")
                state.qa_report.approved = True
                state.status = WorkflowStatus.DEPLOYING
                self.store.save_state(state)

                self._add_issue_comment(repo_name, issue_number, "🚀 QA Approved! Opening the Pull Request...")
                self._enqueue_stage(session_id, "deploy")

    async def _execute_tests(self, state: WorkflowStateModel) -> tuple:
        """Runs the Tester agent and records results on the state. Returns (passed, output)."""
        # Use targeted test command if available, then the repo profile's
        # known test command, then a heuristic fallback
        test_cmd = state.test_command
        if not test_cmd:
            try:
                memory = get_repo_memory(self.store, state.issue.repository)
                test_cmd = (memory.get("profile") or {}).get("test_command")
            except Exception:
                test_cmd = None
        if not test_cmd:
            test_cmd = "pytest"
            for f in state.issue.affected_files:
                if f.endswith(".js") or f.endswith(".ts") or f.endswith(".json"):
                    test_cmd = "npm test"
                    break

        test_data = await self._run_agent_json(
            get_tester_agent, state.session_id,
            {"command": test_cmd, "repository": state.issue.repository}
        )
        if not test_data:
            raise ValueError("Tester agent output could not be parsed as JSON.")

        passed = bool(test_data.get("passed", False))
        output = test_data.get("output", "") or ""
        state.test_results = TestResultsModel(
            passed=passed,
            outcomes=[TestOutcome(test_name=test_cmd, passed=passed, output=output)]
        )
        self._record_quality_gate(
            state,
            "targeted_tests",
            test_cmd,
            passed,
            output,
            attempt=len([g for g in state.quality_gates if g.name == "targeted_tests"]) + 1,
        )
        self.store.save_state(state)
        return passed, output

    def _repo_profile(self, state: WorkflowStateModel, workdir: Optional[str] = None) -> Dict[str, Any]:
        try:
            memory = get_repo_memory(self.store, state.issue.repository, workdir)
            return memory.get("profile") or {}
        except Exception as e:
            logger.warning(f"Could not load repo profile for quality gates: {e}")
            return {}

    def _quality_commands(self, state: WorkflowStateModel, workdir: str) -> Dict[str, Optional[str]]:
        profile = self._repo_profile(state, workdir)
        return {
            "lint": profile.get("lint_command"),
            "typecheck": profile.get("typecheck_command"),
        }

    def _run_command_quality_gate(
        self,
        state: WorkflowStateModel,
        name: str,
        command: Optional[str],
        workdir: str,
        attempt: int = 1,
        remediation_summary: str = "",
    ) -> QualityGateResult:
        if not command:
            return self._record_quality_gate(
                state,
                name,
                "",
                True,
                f"No {name} command discovered in repository profile; gate skipped.",
                attempt,
                remediation_summary=remediation_summary,
            )
        try:
            timeout = int(settings.get("quality.gate_timeout_seconds", 600) or 600)
        except (TypeError, ValueError):
            timeout = 600
        result = run_safe_shell_command(command, workdir, timeout=timeout)
        output = "\n".join(part for part in [result.get("stdout", ""), result.get("stderr", "")] if part)
        return self._record_quality_gate(
            state,
            name,
            command,
            bool(result.get("success")),
            output or f"Command exited with code {result.get('returncode')}",
            attempt,
            remediation_summary=remediation_summary,
        )

    def _run_docs_quality_gate(
        self,
        state: WorkflowStateModel,
        workdir: str,
        attempt: int = 1,
        remediation_summary: str = "",
    ) -> QualityGateResult:
        profile = self._repo_profile(state, workdir)
        docs_paths = profile.get("docs_paths") or ["README.md", "docs"]
        docs_changed = [
            path for path in state.modified_files
            if path.lower().endswith((".md", ".mdx", ".rst"))
            or any(path == docs_path or path.startswith(f"{docs_path}/") for docs_path in docs_paths)
        ]
        if not state.docs_required:
            return self._record_quality_gate(
                state,
                "docs",
                "",
                True,
                "Docs not required for this issue based on planning contract.",
                attempt,
                artifact_paths=docs_changed,
                remediation_summary=remediation_summary,
            )
        passed = bool(docs_changed)
        output = (
            "Documentation changes detected: " + ", ".join(docs_changed)
            if passed
            else "Docs were marked required, but no README/docs/markdown changes were recorded."
        )
        return self._record_quality_gate(
            state,
            "docs",
            "",
            passed,
            output,
            attempt,
            artifact_paths=docs_changed,
            remediation_summary=remediation_summary,
        )

    def _run_artifact_quality_gate(
        self,
        state: WorkflowStateModel,
        workdir: str,
        attempt: int = 1,
        remediation_summary: str = "",
    ) -> QualityGateResult:
        generated_paths = GENERATED_ARTIFACT_PATHS + GENERATED_ARTIFACT_DIRS
        try:
            result = subprocess.run(
                ["git", "ls-files", "--", *generated_paths],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=20,
            )
            tracked = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        except Exception as e:
            tracked = []
            return self._record_quality_gate(
                state,
                "artifact_hygiene",
                "git ls-files -- generated artifacts",
                False,
                f"Could not inspect tracked generated artifacts: {e}",
                attempt,
                remediation_summary=remediation_summary,
            )
        passed = not tracked
        output = (
            "No generated local artifacts are tracked."
            if passed
            else "Generated local artifacts are tracked and must be removed from the commit: " + ", ".join(tracked)
        )
        return self._record_quality_gate(
            state,
            "artifact_hygiene",
            "git ls-files -- generated artifacts",
            passed,
            output,
            attempt,
            artifact_paths=tracked,
            remediation_summary=remediation_summary,
        )

    def _is_generated_artifact_path(self, path: str) -> bool:
        normalized = (path or "").replace("\\", "/").strip()
        if (
            not normalized
            or normalized.startswith("/")
            or normalized == ".."
            or normalized.startswith("../")
            or "/../" in normalized
        ):
            return False
        if normalized in GENERATED_ARTIFACT_PATHS:
            return True
        return any(normalized == d or normalized.startswith(f"{d}/") for d in GENERATED_ARTIFACT_DIRS)

    def _untrack_generated_artifacts(self, workdir: str, paths: List[str]) -> tuple[bool, str]:
        unique_paths = []
        for path in paths:
            normalized = (path or "").replace("\\", "/").strip()
            if normalized and normalized not in unique_paths:
                unique_paths.append(normalized)

        unsafe = [path for path in unique_paths if not self._is_generated_artifact_path(path)]
        if unsafe:
            return False, "Refusing to untrack non-generated artifact paths: " + ", ".join(unsafe)
        if not unique_paths:
            return False, "Artifact hygiene remediation had no generated artifact paths to untrack."

        try:
            result = subprocess.run(
                ["git", "rm", "--cached", "--", *unique_paths],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception as e:
            return False, f"Failed to untrack generated artifacts: {e}"

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            detail = stderr or stdout or f"git rm exited with code {result.returncode}"
            return False, f"Failed to untrack generated artifacts: {detail}"
        detail = stdout or "Generated artifacts removed from git index."
        return True, f"Untracked generated artifacts from git index: {', '.join(unique_paths)}\n{detail}"

    async def _run_quality_gate_with_self_heal(
        self,
        state: WorkflowStateModel,
        name: str,
        run_gate,
        workdir: str,
    ) -> bool:
        try:
            max_attempts = int(settings.get("quality.max_gate_retries", 1) or 1)
        except (TypeError, ValueError):
            max_attempts = 1
        attempt = 1
        remediation_summary = ""
        while True:
            gate = run_gate(attempt, remediation_summary)
            if gate.passed:
                return attempt > 1
            if attempt > max_attempts:
                message = (
                    f"Quality gate '{name}' failed after {attempt} attempt(s).\n"
                    f"Command: {gate.command or 'internal check'}\n"
                    f"Output:\n{gate.output or 'No output'}"
                )
                state.final_evidence_summary = self._build_final_evidence_summary(state)
                self._fail(state, name, message)
                self._add_issue_comment(
                    state.issue.repository,
                    state.issue.number,
                    f"❌ Quality gate failed: **{name}**\n```\n{message[:2500]}\n```",
                )
                return False
            attempt += 1
            remediation_summary = f"Remediated failed {name} gate on attempt {attempt}."
            if name == "artifact_hygiene":
                ok, summary = self._untrack_generated_artifacts(workdir, gate.artifact_paths)
                remediation_summary = summary
                if not ok:
                    message = (
                        f"Quality gate '{name}' could not self-heal generated artifacts.\n"
                        f"Command: git rm --cached -- generated artifacts\n"
                        f"Output:\n{summary}"
                    )
                    state.final_evidence_summary = self._build_final_evidence_summary(state)
                    self._fail(state, name, message)
                    self._add_issue_comment(
                        state.issue.repository,
                        state.issue.number,
                        f"❌ Quality gate failed: **{name}**\n```\n{message[:2500]}\n```",
                    )
                    return False
                self._add_issue_comment(
                    state.issue.repository,
                    state.issue.number,
                    f"🔧 Quality gate **{name}** self-healed generated artifacts without changing working files.",
                )
                continue
            instruction = (
                f"The quality gate '{name}' failed. Fix the root cause, keep the original issue goal intact, "
                f"and update code/tests/docs only as needed.\n\n"
                f"Acceptance criteria:\n" + "\n".join(f"- {c}" for c in state.acceptance_criteria) + "\n\n"
                f"Failed command/check: {gate.command or name}\n"
                f"Gate output:\n{(gate.output or '')[:3000]}\n\n"
                f"Prior build context:\n{self._format_build_context(state)}"
            )
            self._add_issue_comment(
                state.issue.repository,
                state.issue.number,
                f"🔧 Quality gate **{name}** failed. Founders.crew is sending it back to Builder for self-healing...",
            )
            await self._builder_fix(state, instruction)
            workdir = github_clone_or_pull(state.issue.repository)

    async def _run_quality_gates(self, state: WorkflowStateModel) -> bool:
        workdir = github_clone_or_pull(state.issue.repository)
        commands = self._quality_commands(state, workdir)
        remediated = False
        gates = [
            ("lint", lambda attempt, summary: self._run_command_quality_gate(state, "lint", commands.get("lint"), workdir, attempt, summary)),
            ("typecheck", lambda attempt, summary: self._run_command_quality_gate(state, "typecheck", commands.get("typecheck"), workdir, attempt, summary)),
            ("docs", lambda attempt, summary: self._run_docs_quality_gate(state, workdir, attempt, summary)),
            ("artifact_hygiene", lambda attempt, summary: self._run_artifact_quality_gate(state, workdir, attempt, summary)),
        ]
        for name, run_gate in gates:
            healed = await self._run_quality_gate_with_self_heal(state, name, run_gate, workdir)
            if state.status == WorkflowStatus.FAILED:
                return False
            remediated = remediated or healed
        if remediated:
            passed, output = await self._execute_tests(state)
            if not passed:
                self._fail(state, "testing", f"Testing failed after quality gate remediation:\n{output[:3000]}")
                return False
        state.final_evidence_summary = self._build_final_evidence_summary(state)
        self.store.save_state(state)
        return True

    def _record_build_result(self, state: WorkflowStateModel, build_data: Dict[str, Any], label: str) -> None:
        """Persists Builder output so later self-heal/review/deploy stages keep context."""
        if not build_data:
            return

        raw_files = build_data.get("modified_files") or []
        if isinstance(raw_files, str):
            raw_files = [raw_files]
        for file_name in raw_files:
            file_text = str(file_name).strip()
            if file_text and file_text not in state.modified_files:
                state.modified_files.append(file_text)

        summary = str(build_data.get("summary") or "").strip()
        if summary:
            entry = f"{label}: {summary}"[:1200]
            if entry not in state.build_summaries:
                state.build_summaries.append(entry)
                state.build_summaries = state.build_summaries[-12:]

        test_command = str(build_data.get("test_command") or "").strip()
        if test_command:
            state.test_command = test_command

        self.store.save_state(state)

    def _format_build_context(self, state: WorkflowStateModel) -> str:
        """Formats prior Builder work for self-healing agents."""
        lines = []
        if state.modified_files:
            lines.append("Files already modified in this workflow:")
            lines.extend(f"- {file_name}" for file_name in state.modified_files[:30])
        if state.build_summaries:
            lines.append("Builder summaries so far:")
            lines.extend(f"- {summary}" for summary in state.build_summaries[-8:])
        if state.test_failure_history:
            lines.append("Recent test failure history:")
            lines.extend(f"- {failure}" for failure in state.test_failure_history[-5:])
        return "\n".join(lines)

    async def _builder_fix(self, state: WorkflowStateModel, instruction_text: str) -> None:
        """Sends a corrective instruction to the Builder and pushes the result."""
        build_context = self._format_build_context(state)
        if build_context:
            instruction_text = (
                f"{instruction_text}\n\n"
                f"Existing workflow change context (do not contradict or overwrite without cause):\n"
                f"{build_context}"
            )
        repo_memory = self._repo_memory_text(state.issue.repository)
        if repo_memory:
            instruction_text = f"{instruction_text}\n\n{repo_memory}"
        input_builder = {
            "instruction": instruction_text,
            "files": state.issue.affected_files,
            "repository": state.issue.repository
        }
        build_out = await self._run_agent(get_builder_agent, state.session_id, input_builder)
        build_data = self._parse_json_from_output(build_out)
        self._record_build_result(state, build_data, "Fix pass")
        self._push_workspace_progress(state, "fix")

    def _push_workspace_progress(self, state: WorkflowStateModel, label: str) -> None:
        """Commits and pushes workspace changes to the feature branch (best effort).

        Persisting work to origin right after each build step means nothing is
        lost if the workspace is reset while a human approval gate is pending.
        """
        if not state.branch_name:
            return
        try:
            res = github_push_workspace(
                state.issue.repository,
                state.branch_name,
                f"founderscrew {label}: issue #{state.issue.number} - {state.issue.title[:60]}"
            )
            if not res.get("success"):
                logger.warning(f"Workspace push failed: {res.get('error')}")
        except Exception as e:
            logger.warning(f"Workspace push failed: {e}")

    async def run_build_test_review_flow(self, state: WorkflowStateModel, start_at: str = "building") -> None:
        """Runs the build-test-review loop. Serialized per repository workspace."""
        async with self._get_lock(f"repo::{state.issue.repository}"):
            await self._run_build_test_review_flow(state, start_at)

    async def _run_build_test_review_flow(self, state: WorkflowStateModel, start_at: str = "building") -> None:
        session_id = state.session_id
        repo_name = state.issue.repository
        issue_number = state.issue.number

        # Re-pin the workspace to this workflow's branch (registry is process-local
        # and empty after restarts/resumes)
        if state.branch_name:
            set_active_workspace_branch(repo_name, state.branch_name)

        # 1. Create Feature Branch
        if start_at == "building":
            branch_name = f"founderscrew/fix-issue-{issue_number}"
            state.branch_name = branch_name
            self.store.save_state(state)
            try:
                github_clone_or_pull(repo_name)  # sync workspace to latest base first
                github_prepare_workspace_branch(repo_name, branch_name)
            except Exception as e:
                logger.warning(f"Workspace branch preparation failed: {e}")

        # 2. Execute Building (BuilderAgent)
        if start_at == "building":
            try:
                build_instruction = f"Apply plan steps to resolve the issue: {state.plan.summary}\nSteps:\n"
                for step in state.plan.steps:
                    build_instruction += f"- Step {step.step_number}: {step.description}\n"
                if state.plan.feedback:
                    build_instruction += (
                        "\nFounder feedback that must be addressed in this pass:\n"
                        f"{state.plan.feedback}\n"
                    )
                build_instruction += (
                    f"\nIMPORTANT: You MUST also write a brief, targeted automated test that specifically verifies this issue is resolved. "
                    f"For JavaScript/TypeScript projects save it as tests/integration/issue_{issue_number}_test.spec.js; "
                    f"for Python projects save it as tests/test_issue_{issue_number}.py. "
                    f"Do not rely on the global regression suite. "
                    f"The test_command you report MUST reference the exact path where the test file was saved.\n"
                )
                repo_memory = self._repo_memory_text(repo_name)
                if repo_memory:
                    build_instruction += f"\n{repo_memory}\n"

                input_builder = {
                    "instruction": build_instruction,
                    "files": state.issue.affected_files,
                    "repository": repo_name
                }

                build_out = await self._run_agent(get_builder_agent, session_id, input_builder)

                # Extract targeted test command
                build_data = self._parse_json_from_output(build_out)
                self._record_build_result(state, build_data, "Initial build")

                # Persist the work to the feature branch before the human gates
                self._push_workspace_progress(state, "wip")

                state.status = WorkflowStatus.TESTING
                self.store.save_state(state)
                start_at = "testing"
            except Exception as e:
                self._fail(state, "building", f"Building failed: {e}")
                self._add_issue_comment(repo_name, issue_number, f"❌ Building failed: {e}")
                return

        # 3. Execute Testing (TesterAgent) with self-healing fix loop:
        # failures are fed back to the Builder up to agents.max_retries times
        if start_at == "testing":
            try:
                try:
                    max_fix_attempts = int(settings.get("agents.max_retries", 2) or 2)
                except (TypeError, ValueError):
                    max_fix_attempts = 2

                attempt = 0
                last_failure = ""
                error_history = []  # Accumulate ALL error outputs across attempts
                while True:
                    passed, output = await self._execute_tests(state)
                    if passed:
                        break
                    last_failure = output
                    error_history.append(f"Attempt {attempt + 1}: {output[:1500]}")
                    state.test_failure_history.append(f"Attempt {attempt + 1}: {output[:1500]}")
                    state.test_failure_history = state.test_failure_history[-10:]
                    self.store.save_state(state)
                    if attempt >= max_fix_attempts:
                        raise RuntimeError(
                            f"Automated test execution failed after {attempt + 1} attempt(s):\n{output or 'No detail output.'}"
                        )
                    attempt += 1
                    self._add_issue_comment(
                        repo_name, issue_number,
                        f"🔧 Tests failed (fix attempt {attempt}/{max_fix_attempts}). "
                        f"Founders.crew is feeding the failure back to the Builder to self-heal..."
                    )
                    # Build fix instruction with full error history so the builder
                    # doesn't oscillate between fixes that break each other
                    history_block = ""
                    if len(error_history) > 1:
                        history_block = (
                            "\nPREVIOUS FIX ATTEMPTS AND THEIR ERRORS (do NOT re-introduce old bugs):\n"
                            + "\n---\n".join(error_history[:-1])
                            + "\n---\n"
                        )
                    fix_instruction = (
                        f"The previous code change failed its automated tests. Analyze the test output below, "
                        f"find the root cause, and fix the code (and/or the test if the test itself is wrong).\n"
                        f"Original goal: {state.plan.summary if state.plan else state.issue.title}\n"
                        f"Test command: {state.test_command or 'project default'}\n"
                        f"{history_block}"
                        f"CURRENT test failure (attempt {attempt}):\n{output[:3000]}\n"
                    )
                    await self._builder_fix(state, fix_instruction)

                if attempt > 0:
                    # Remember the gotcha so future issues on this repo don't rediscover it
                    add_repo_lesson(self.store, repo_name, {
                        "issue": issue_number,
                        "summary": (
                            f"Tests failed initially but were self-healed after {attempt} Builder fix attempt(s). "
                            f"Original failure: {last_failure[:200]}"
                        )
                    })

                if not await self._run_quality_gates(state):
                    return

                state.status = WorkflowStatus.REVIEWING
                self.store.save_state(state)
                start_at = "reviewing"
            except Exception as e:
                self._fail(state, "testing", f"Testing failed: {e}")
                self._add_issue_comment(repo_name, issue_number, f"❌ Testing failed: {e}")
                return

        # 4. Execute Reviewing (ReviewerAgent) — advisory, but auto-fixable
        # findings are applied and re-tested
        if start_at == "reviewing":
            review_data = {}
            try:
                # Gather the changed files list from the plan
                review_files = list(set(
                    f for s in (state.plan.steps if state.plan else [])
                    for f in s.files_affected
                ))
                review_files = sorted(set(review_files + state.modified_files))
                # Grab the git diff from the workspace for real code context
                workspace_diff = ""
                try:
                    workdir = github_clone_or_pull(repo_name)
                    import subprocess as _sp
                    diff_result = _sp.run(
                        ["git", "diff", "HEAD~1", "--", "."],
                        cwd=workdir, capture_output=True, text=True, timeout=30
                    )
                    if diff_result.returncode == 0 and diff_result.stdout.strip():
                        workspace_diff = diff_result.stdout[:8000]
                except Exception as diff_err:
                    logger.warning(f"Could not capture git diff for reviewer: {diff_err}")

                review_input = {
                    "issue_title": state.issue.title,
                    "issue_body": (state.issue.body or "")[:3000],
                    "plan_summary": state.plan.summary if state.plan else "",
                    "files_changed": review_files,
                    "build_summaries": state.build_summaries,
                    "test_failure_history": state.test_failure_history,
                    "code_diff": workspace_diff,
                    "test_results": state.test_results.model_dump() if state.test_results else {},
                    "repository": repo_name,
                }
                review_data = await self._run_agent_json(
                    get_reviewer_agent, session_id,
                    review_input
                )
            except Exception as e:
                logger.warning(f"Reviewer agent failed: {e}. Proceeding directly to QA.")

            recommendations = review_data.get("recommendations") or []
            auto_fixable = review_data.get("auto_fixable") or []
            review_passed = review_data.get("passed", True)

            if recommendations:
                rec_lines = "\n".join(f"- {str(r)}" for r in recommendations[:10])
                try:
                    self._add_issue_comment(
                        repo_name, issue_number,
                        f"### 🧐 Founders.crew Code Review\n\n"
                        f"{'✅ Approved' if review_passed else '⚠️ Changes recommended'}\n\n{rec_lines}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to post review comment: {e}")

            if not review_passed and auto_fixable:
                try:
                    fix_text = "Apply the following code review fixes:\n" + "\n".join(f"- {str(r)}" for r in auto_fixable[:10])
                    await self._builder_fix(state, fix_text)
                    passed, output = await self._execute_tests(state)
                    if not passed:
                        self._fail(state, "testing", f"Testing failed: tests broke after applying reviewer fixes:\n{output[:1500]}")
                        self._add_issue_comment(repo_name, issue_number, f"❌ Testing failed after reviewer auto-fixes:\n```\n{output[:1000]}\n```")
                        return
                    if not await self._run_quality_gates(state):
                        return
                except Exception as e:
                    self._fail(state, "reviewing", f"Reviewer auto-fix pass failed: {e}")
                    self._add_issue_comment(repo_name, issue_number, f"❌ Reviewer auto-fix pass failed: {e}")
                    return
            elif not review_passed:
                message = "Code review found issues that were not marked auto-fixable:\n" + "\n".join(
                    f"- {str(r)}" for r in recommendations[:10]
                )
                state.final_evidence_summary = self._build_final_evidence_summary(state)
                self._fail(state, "reviewing", message)
                self._add_issue_comment(repo_name, issue_number, f"❌ Code review requires founder/product attention:\n```\n{message[:2000]}\n```")
                return

            state.status = WorkflowStatus.QA
            self.store.save_state(state)
            start_at = "qa"

        # 5. Execute QA (QAAgent) against a real locally-running dev server
        if start_at == "qa":
            try:
                if not state.ui_qa_required:
                    state.qa_report = QAReportModel(
                        passed=True,
                        summary="Visual QA was not required for this non-UI workflow based on the approved quality contract.",
                        screenshots=[],
                        approved=True,
                    )
                    self._record_quality_gate(
                        state,
                        "visual_qa",
                        "",
                        True,
                        state.qa_report.summary,
                    )
                    state.status = WorkflowStatus.DEPLOYING
                    self.store.save_state(state)
                    self._enqueue_stage(session_id, "deploy")
                    return

                qa_data = {}
                screenshot_path = None
                try:
                    max_visual_fix_attempts = int(settings.get("qa.max_visual_fix_attempts", 1) or 1)
                except (TypeError, ValueError):
                    max_visual_fix_attempts = 1
                visual_fix_attempt = 0

                while True:
                    server_proc = None
                    try:
                        qa_url = settings.get("qa.target_url") or None
                        workdir = github_clone_or_pull(repo_name)
                        if not qa_url and (Path(workdir) / "package.json").exists():
                            dev_cmd = settings.get("qa.dev_server_command") or None
                            if not dev_cmd:
                                try:
                                    memory = get_repo_memory(self.store, repo_name)
                                    dev_cmd = (memory.get("profile") or {}).get("dev_server_command")
                                except Exception:
                                    dev_cmd = None
                            server_proc, qa_url = await asyncio.to_thread(start_dev_server, workdir, dev_cmd)

                        if qa_url:
                            route_candidates = infer_qa_route_candidates(
                                workdir,
                                issue_title=state.issue.title,
                                issue_body=state.issue.body or "",
                                affected_files=state.issue.affected_files,
                                changed_files=state.modified_files + self._plan_files(state),
                            )
                            qa_target_path = route_candidates[0]["path"] if route_candidates else "/"
                            qa_capture_url = self._join_qa_url(str(qa_url), qa_target_path)
                            route_candidate_text = format_route_candidates(route_candidates)
                            qa_allowed_paths = [candidate["path"] for candidate in route_candidates] or [qa_target_path]
                            logger.info(
                                "QA route target for %s: %s (candidates: %s)",
                                session_id,
                                qa_capture_url,
                                route_candidate_text.replace("\n", " | "),
                            )
                            # Capture the screenshot deterministically (mock fallback
                            # disallowed — a generated placeholder must never pass as
                            # evidence) and attach the actual image so the multimodal
                            # QA agent judges what the page really looks like.
                            shots_dir = Path.home() / ".founderscrew" / "screenshots"
                            shots_dir.mkdir(parents=True, exist_ok=True)
                            candidate = shots_dir / f"{session_id}_qa.png"
                            captured = await asyncio.to_thread(
                                capture_screenshot,
                                str(qa_capture_url),
                                str(candidate),
                                False,
                                workdir,
                            )

                            if not captured:
                                render_diagnostics = await asyncio.to_thread(
                                    diagnose_page_render,
                                    str(qa_capture_url),
                                    workdir,
                                )
                                diagnostic_text = self._qa_diagnostic_text(str(qa_capture_url), None, render_diagnostics)
                                message = (
                                    f"QA could not capture a real screenshot of {qa_capture_url}. "
                                    f"The workflow stopped before founder approval.\n\n{diagnostic_text}"
                                )
                                state.qa_report = QAReportModel(
                                    passed=False,
                                    summary=message,
                                    screenshots=[],
                                    approved=False,
                                )
                                self._fail(state, "qa", message)
                                self._add_issue_comment(repo_name, issue_number, f"❌ QA screenshot capture failed:\n```\n{message[:2500]}\n```")
                                return

                            screenshot_path = str(candidate)
                            screenshot_analysis = await asyncio.to_thread(analyze_screenshot, screenshot_path)
                            if screenshot_analysis.get("is_blank"):
                                render_diagnostics = await asyncio.to_thread(
                                    diagnose_page_render,
                                    str(qa_capture_url),
                                    workdir,
                                )
                                diagnostic_text = self._qa_diagnostic_text(
                                    str(qa_capture_url),
                                    screenshot_analysis,
                                    render_diagnostics,
                                )
                                if visual_fix_attempt < max_visual_fix_attempts:
                                    visual_fix_attempt += 1
                                    stop_dev_server(server_proc)
                                    server_proc = None
                                    self._add_issue_comment(
                                        repo_name,
                                        issue_number,
                                        f"🔧 QA detected a blank or low-information screenshot "
                                        f"(visual fix attempt {visual_fix_attempt}/{max_visual_fix_attempts}). "
                                        f"Founders.crew is diagnosing and sending it back to Builder before asking for approval."
                                    )
                                    fix_instruction = (
                                        "The QA screenshot is blank or visually unusable. Diagnose the frontend render path "
                                        "and fix the issue so the app shows meaningful UI at the QA URL.\n\n"
                                        f"Original goal: {state.plan.summary if state.plan else state.issue.title}\n"
                                        f"Browser and screenshot diagnostics:\n{diagnostic_text[:5000]}\n"
                                    )
                                    await self._builder_fix(state, fix_instruction)
                                    passed, output = await self._execute_tests(state)
                                    if not passed:
                                        message = (
                                            "QA blank-screen remediation changed the workspace, but automated tests failed:\n"
                                            f"{output[:3000]}"
                                        )
                                        state.qa_report = QAReportModel(
                                            passed=False,
                                            summary=message,
                                            screenshots=[screenshot_path],
                                            approved=False,
                                        )
                                        self._fail(state, "testing", message)
                                        self._add_issue_comment(repo_name, issue_number, f"❌ QA remediation failed tests:\n```\n{output[:1500]}\n```")
                                        return
                                    continue

                                message = (
                                    "QA captured a blank or low-information screenshot after remediation attempts. "
                                    "The workflow stopped before founder approval.\n\n"
                                    f"{diagnostic_text}"
                                )
                                state.qa_report = QAReportModel(
                                    passed=False,
                                    summary=message,
                                    screenshots=[screenshot_path],
                                    approved=False,
                                )
                                self._fail(state, "qa", message)
                                self._add_issue_comment(repo_name, issue_number, f"❌ QA blank-screen check failed:\n```\n{message[:2500]}\n```")
                                return

                            # Build rich context for issue-aware QA testing
                            plan_steps_text = ""
                            files_changed = []
                            if state.plan:
                                plan_steps_text = "\n".join(
                                    f"  Step {s.step_number}: {s.description} (files: {', '.join(s.files_affected)})"
                                    for s in state.plan.steps
                                )
                                for s in state.plan.steps:
                                    files_changed.extend(s.files_affected)
                            files_changed = list(set(files_changed))

                            test_summary = ""
                            if state.test_results:
                                test_summary = f"Tests {'PASSED' if state.test_results.passed else 'FAILED'}."
                                if state.test_results.outcomes:
                                    test_summary += " Results: " + "; ".join(
                                        f"{o.test_name}: {'✓' if o.passed else '✗'}"
                                        for o in state.test_results.outcomes[:10]
                                    )

                            shots_dir = Path.home() / ".founderscrew" / "screenshots" / session_id
                            shots_dir.mkdir(parents=True, exist_ok=True)

                            qa_input = {
                                "url": str(qa_url),
                                "qa_target_url": str(qa_capture_url),
                                "qa_target_path": qa_target_path,
                                "qa_allowed_paths": qa_allowed_paths,
                                "qa_route_candidates": route_candidate_text,
                                "issue_title": state.issue.title,
                                "issue_body": state.issue.body or "(no description provided)",
                                "plan_summary": state.plan.summary if state.plan else "(no plan available)",
                                "plan_steps": plan_steps_text or "(no steps available)",
                                "files_changed": ", ".join(files_changed) if files_changed else "(unknown)",
                                "test_results": test_summary or "(no test results)",
                                "output_dir": str(shots_dir),
                                "workdir": workdir,
                                "note": (
                                    "A static screenshot of the rendered page is attached as an image for initial reference. "
                                    "However, you MUST use capture_interactive_screenshot to perform targeted testing — "
                                    "start with qa_target_path and the inferred route candidates, interact with the specific page/component mentioned in the issue "
                                    "(click, hover, scroll), and take screenshots at each step to verify the fix. "
                                    "Do NOT navigate to unrelated routes such as /dashboard unless the inferred route candidates explicitly include them. "
                                    "The browser tool will reject navigation outside qa_route_candidates. "
                                    "If the component is not visible on the allowed routes, report a QA route/render blocker instead of guessing a new route. "
                                    "Do NOT just evaluate the attached static screenshot and call it done."
                                )
                            }
                            old_allowed_paths = os.environ.get("FOUNDERSCREW_QA_ALLOWED_PATHS")
                            old_target_path = os.environ.get("FOUNDERSCREW_QA_TARGET_PATH")
                            os.environ["FOUNDERSCREW_QA_ALLOWED_PATHS"] = json.dumps(qa_allowed_paths)
                            os.environ["FOUNDERSCREW_QA_TARGET_PATH"] = qa_target_path
                            try:
                                qa_data = await self._run_agent_json(
                                    get_qa_agent,
                                    session_id,
                                    qa_input,
                                    image_paths=[screenshot_path],
                                )
                            finally:
                                if old_allowed_paths is None:
                                    os.environ.pop("FOUNDERSCREW_QA_ALLOWED_PATHS", None)
                                else:
                                    os.environ["FOUNDERSCREW_QA_ALLOWED_PATHS"] = old_allowed_paths
                                if old_target_path is None:
                                    os.environ.pop("FOUNDERSCREW_QA_TARGET_PATH", None)
                                else:
                                    os.environ["FOUNDERSCREW_QA_TARGET_PATH"] = old_target_path
                            if qa_data and not qa_data.get("passed", True):
                                qa_issue_text = "\n\n".join(
                                    str(qa_data.get(key) or "")
                                    for key in ("test_plan", "observations", "issues_found")
                                    if qa_data.get(key)
                                )
                                if self._is_qa_tooling_or_route_failure(qa_issue_text):
                                    message = (
                                        "QA could not verify the issue because the browser/tooling route was invalid or produced non-real evidence. "
                                        "The workflow stopped instead of asking Builder to alter production routes for QA.\n\n"
                                        f"Inferred QA routes:\n{route_candidate_text}\n\n"
                                        f"QA findings:\n{qa_issue_text[:3000]}"
                                    )
                                    state.qa_report = QAReportModel(
                                        passed=False,
                                        summary=message,
                                        screenshots=[screenshot_path] if screenshot_path else [],
                                        approved=False,
                                    )
                                    self._record_quality_gate(
                                        state,
                                        "visual_qa",
                                        "interactive Playwright QA",
                                        False,
                                        message,
                                        artifact_paths=[screenshot_path] if screenshot_path else [],
                                    )
                                    state.final_evidence_summary = self._build_final_evidence_summary(state)
                                    self._fail(state, "qa", message)
                                    self._add_issue_comment(repo_name, issue_number, f"❌ QA tooling/route verification failed:\n```\n{message[:2500]}\n```")
                                    return
                                if visual_fix_attempt < max_visual_fix_attempts:
                                    visual_fix_attempt += 1
                                    stop_dev_server(server_proc)
                                    server_proc = None
                                    self._add_issue_comment(
                                        repo_name,
                                        issue_number,
                                        f"🔧 QA found issue-specific verification problems "
                                        f"(visual fix attempt {visual_fix_attempt}/{max_visual_fix_attempts}). "
                                        f"Founders.crew is sending the findings back to Builder before PR creation."
                                    )
                                    await self._builder_fix(
                                        state,
                                        "The QA agent could not verify the issue-specific fix. "
                                        "Resolve the QA findings below, preserve the original implementation goal, "
                                        "and update targeted tests if needed.\n\n"
                                        f"Acceptance criteria:\n" + "\n".join(f"- {c}" for c in state.acceptance_criteria) + "\n\n"
                                        f"QA findings:\n{qa_issue_text[:4000]}",
                                    )
                                    passed, output = await self._execute_tests(state)
                                    if not passed:
                                        message = (
                                            "QA remediation changed the workspace, but automated tests failed:\n"
                                            f"{output[:3000]}"
                                        )
                                        self._fail(state, "testing", message)
                                        self._add_issue_comment(repo_name, issue_number, f"❌ QA remediation failed tests:\n```\n{output[:1500]}\n```")
                                        return
                                    continue
                                message = (
                                    "QA could not verify the issue-specific fix after remediation attempts.\n\n"
                                    f"{qa_issue_text}"
                                )
                                state.qa_report = QAReportModel(
                                    passed=False,
                                    summary=message,
                                    screenshots=[screenshot_path] if screenshot_path else [],
                                    approved=False,
                                )
                                self._record_quality_gate(
                                    state,
                                    "visual_qa",
                                    "interactive Playwright QA",
                                    False,
                                    message,
                                    artifact_paths=[screenshot_path] if screenshot_path else [],
                                )
                                state.final_evidence_summary = self._build_final_evidence_summary(state)
                                self._fail(state, "qa", message)
                                self._add_issue_comment(repo_name, issue_number, f"❌ QA verification failed:\n```\n{message[:2500]}\n```")
                                return
                        else:
                            qa_data = {
                                "passed": True,
                                "similarity_percentage": 100.0,
                                "observations": "No runnable web UI detected for visual QA; screenshot verification skipped."
                            }
                        break
                    finally:
                        stop_dev_server(server_proc)

                if not qa_data:
                    # The visual evidence exists, but the QA agent failed to
                    # structure its response. Keep the human gate for the
                    # captured non-blank screenshot instead of losing the run.
                    qa_data = {
                        "passed": False,
                        "observations": "QA agent could not produce a structured report; please verify the UI manually before approving."
                    }

                # Build enriched summary from the new QA agent output
                qa_summary_parts = []
                if qa_data.get("test_plan"):
                    qa_summary_parts.append(f"**Test Plan:** {qa_data['test_plan']}")
                if qa_data.get("observations"):
                    qa_summary_parts.append(f"**Observations:** {qa_data['observations']}")
                if qa_data.get("issues_found") and qa_data["issues_found"] != "None":
                    qa_summary_parts.append(f"**Issues Found:** {qa_data['issues_found']}")
                qa_summary = "\n\n".join(qa_summary_parts) if qa_summary_parts else qa_data.get("observations", "No visual issues detected during QA check.")

                # Collect all screenshots: the static one plus any interactive ones
                all_screenshots = [screenshot_path] if screenshot_path else []
                interactive_shots_dir = Path.home() / ".founderscrew" / "screenshots" / session_id
                if interactive_shots_dir.exists():
                    for img in sorted(interactive_shots_dir.glob("*.png")):
                        if str(img) not in all_screenshots:
                            all_screenshots.append(str(img))

                state.qa_report = QAReportModel(
                    passed=qa_data.get("passed", True),
                    summary=qa_summary,
                    screenshots=all_screenshots,
                    approved=True
                )
                self._record_quality_gate(
                    state,
                    "visual_qa",
                    "interactive Playwright QA",
                    bool(state.qa_report.passed),
                    state.qa_report.summary,
                    artifact_paths=all_screenshots,
                )
                state.final_evidence_summary = self._build_final_evidence_summary(state)
                state.status = WorkflowStatus.DEPLOYING
                self.store.save_state(state)

                # Post QA report to issue
                screenshot_note = (
                    f"📸 Screenshot captured — view it on the dashboard run page (`/run/{session_id}`).\n\n"
                    if screenshot_path else ""
                )
                qa_body = (
                    f"### 🔎 Founders.crew QA Report\n\n"
                    f"Test results: {'✅ Passed' if state.test_results.passed else '❌ Failed'}\n"
                    f"Visual check: {'✅ Passed' if state.qa_report.passed else '⚠️ Visual warning'}\n\n"
                    f"**Visual Observations:**\n{state.qa_report.summary}\n\n"
                    f"{screenshot_note}"
                    f"---\n"
                    f"✅ QA passed. Founders.crew is opening the Pull Request for final human review."
                )
                self._add_issue_comment(repo_name, issue_number, qa_body)
                self._enqueue_stage(session_id, "deploy")

            except Exception as e:
                self._fail(state, "qa", f"QA stage failed: {e}")
                self._add_issue_comment(repo_name, issue_number, f"❌ QA stage failed: {e}")

    async def run_deploy_step(self, state: WorkflowStateModel) -> None:
        """Runs the final deployer agent to create PR. Serialized per repository workspace."""
        async with self._get_lock(f"repo::{state.issue.repository}"):
            await self._run_deploy_step(state)

    async def _run_deploy_step(self, state: WorkflowStateModel) -> None:
        session_id = state.session_id
        repo_name = state.issue.repository
        issue_number = state.issue.number

        if state.branch_name:
            set_active_workspace_branch(repo_name, state.branch_name)

        try:
            missing_or_failed = self._required_gate_failures(state)
            if missing_or_failed:
                message = (
                    "PR creation blocked because required quality gates are missing or failing: "
                    + ", ".join(missing_or_failed)
                )
                state.final_evidence_summary = self._build_final_evidence_summary(state)
                self._fail(state, "deploy", message)
                self._add_issue_comment(repo_name, issue_number, f"❌ {message}")
                return

            # Collect all evidence for a rich PR body
            plan_steps_text = ""
            files_changed = []
            if state.plan:
                plan_steps_text = "\n".join(
                    f"- Step {s.step_number}: {s.description} ({', '.join(s.files_affected)})"
                    for s in state.plan.steps
                )
                files_changed = list(set(
                    f for s in state.plan.steps for f in s.files_affected
                ))
            files_changed = sorted(set(files_changed + state.modified_files))

            build_evidence = ""
            if state.build_summaries:
                build_evidence = "\n".join(f"- {summary}" for summary in state.build_summaries[-10:])

            test_evidence = ""
            if state.test_results:
                test_evidence = f"Tests {'PASSED ✅' if state.test_results.passed else 'FAILED ❌'}"
                if state.test_results.outcomes:
                    test_evidence += "\n" + "\n".join(
                        f"  - {o.test_name}: {'✅' if o.passed else '❌'}"
                        + (f"\n    Output: {(o.output or '').strip()[:500]}" if (o.output or "").strip() else "")
                        for o in state.test_results.outcomes[:10]
                    )
            if state.test_failure_history:
                test_evidence += "\nPrior self-healed failures:\n" + "\n".join(
                    f"  - {failure}" for failure in state.test_failure_history[-5:]
                )

            qa_evidence = ""
            if state.qa_report:
                qa_evidence = (
                    f"QA Visual Check: {'PASSED ✅' if state.qa_report.passed else 'NEEDS REVIEW ⚠️'}\n"
                    f"{state.qa_report.summary[:2000]}"
                )

            quality_evidence = state.final_evidence_summary or self._build_final_evidence_summary(state)
            acceptance_evidence = "\n".join(f"- [x] {criterion}" for criterion in state.acceptance_criteria)
            deployment_notes = (
                "Pull request is ready for final human review and normal repository deployment flow. "
                "Founders.crew did not auto-merge or auto-deploy this change."
            )

            pr_data = {
                "branch_name": state.branch_name,
                "repository": repo_name,
                "issue_number": issue_number,
                "plan_summary": state.plan.summary if state.plan else "",
                "plan_steps": plan_steps_text,
                "files_changed": files_changed,
                "acceptance_criteria": acceptance_evidence,
                "build_evidence": build_evidence,
                "test_evidence": test_evidence,
                "qa_evidence": qa_evidence,
                "quality_evidence": quality_evidence,
                "docs_status": "Required" if state.docs_required else "Not required",
                "deployment_notes": deployment_notes,
                "issue_title": state.issue.title,
            }
            
            deploy_data = await self._run_agent_json(get_deployer_agent, session_id, pr_data)

            if not deploy_data:
                raise ValueError("Deployer agent output could not be parsed as JSON.")

            pr_url = deploy_data.get("pr_url", "")
            state.pr_url = pr_url
            state.status = WorkflowStatus.AWAIT_PR_APPROVAL
            self.store.save_state(state)

            # Workflow no longer needs the workspace pinned to the feature branch
            set_active_workspace_branch(repo_name, None)

            # Record the completed work as episodic memory for future issues
            add_repo_lesson(self.store, repo_name, {
                "issue": issue_number,
                "summary": (
                    f"Resolved via branch {state.branch_name}: "
                    f"{(state.plan.summary if state.plan else state.issue.title)[:160]} "
                    f"(files: {', '.join(state.issue.affected_files[:5]) or 'n/a'}; "
                    f"test: {state.test_command or 'project default'})"
                )
            })

            success_body = (
                f"🎉 **Founders.crew has opened a Pull Request!**\n\n"
                f"Review the code changes and merge the PR here:\n"
                f"👉 **PR Link:** {pr_url or 'Created'}"
            )
            self._add_issue_comment(repo_name, issue_number, success_body)

        except Exception as e:
            self._fail(state, "deploy", f"Deployment failed: {e}")
            self._add_issue_comment(repo_name, issue_number, f"❌ Deploy stage failed: {e}")

    # Maps a failed stage name to (resume status, marker text in legacy error messages)
    _STAGE_RESUME_MAP = {
        "triage": (WorkflowStatus.TRIAGE, "Triage stage failed"),
        "planning": (WorkflowStatus.PLANNING, "Planning stage failed"),
        "building": (WorkflowStatus.BUILDING, "Building failed"),
        "testing": (WorkflowStatus.TESTING, "Testing failed"),
        "qa": (WorkflowStatus.QA, "QA stage failed"),
        "deploy": (WorkflowStatus.DEPLOYING, "Deployment failed"),
    }

    async def resume_failed_workflow(self, session_id: str) -> None:
        """Resumes a failed workflow from the stage where it failed."""
        state = self.store.load_state(session_id)
        if not state or state.status != WorkflowStatus.FAILED:
            return

        repo_name = state.issue.repository
        issue_number = state.issue.number

        # Prefer the explicitly recorded failed stage; fall back to matching
        # legacy error message text for states persisted before failed_stage existed
        stage = state.failed_stage
        if stage not in self._STAGE_RESUME_MAP:
            err = state.error_message or ""
            stage = next(
                (name for name, (_, marker) in self._STAGE_RESUME_MAP.items() if marker in err),
                "triage"
            )

        state.error_message = None
        state.failed_stage = None
        state.status = self._STAGE_RESUME_MAP[stage][0]
        self.store.save_state(state)

        self._add_issue_comment(repo_name, issue_number, f"🔄 Retrying {stage.title()} stage...")

        if stage == "triage":
            self._enqueue_stage(session_id, "triage")
        elif stage == "planning":
            self._enqueue_stage(session_id, "planning")
        elif stage == "deploy":
            self._enqueue_stage(session_id, "deploy")
        else:
            self._enqueue_stage(session_id, stage)

    async def replan_with_feedback(self, session_id: str, feedback: str) -> None:
        """Re-runs the PlannerAgent with user feedback appended to the original issue context."""
        state = self.store.load_state(session_id)
        if not state:
            return
            
        repo_name = state.issue.repository
        issue_number = state.issue.number
        
        # Append user feedback to the issue context for the planner
        original_body = state.issue.body or ""
        state.issue.body = (
            f"{original_body}\n\n"
            f"---\n"
            f"**Founder Feedback on Previous Plan:**\n"
            f"{feedback}"
        )
        
        # Reset plan and status
        state.plan = None
        state.status = WorkflowStatus.PLANNING
        state.error_message = None
        state.failed_stage = None
        self.store.save_state(state)
        
        self._add_issue_comment(repo_name, issue_number, f"📝 Plan revision requested with feedback:\n> {feedback}\n\n🔄 Re-running Planner Agent...")
        self._enqueue_stage(session_id, "planning")

    async def reject_stage_with_feedback(self, session_id: str, target_stage: str, feedback: str) -> None:
        """Records founder feedback for a stage and queues the appropriate rework."""
        feedback = (feedback or "").strip()
        if not feedback:
            feedback = "No additional feedback provided."
        target_stage = (target_stage or "").strip().lower()

        if target_stage in {"plan", "planning"}:
            await self.replan_with_feedback(session_id, feedback)
            return

        state = self.store.load_state(session_id)
        if not state:
            return

        repo_name = state.issue.repository
        issue_number = state.issue.number
        valid_stages = {"triage", "building", "testing", "reviewing", "qa", "deploy"}
        if target_stage not in valid_stages:
            return

        feedback_block = (
            f"\n\n---\n"
            f"**Founder Feedback on {target_stage.title()} Stage:**\n"
            f"{feedback}"
        )
        state.issue.body = f"{state.issue.body or ''}{feedback_block}"
        if state.plan:
            previous_feedback = (state.plan.feedback or "").strip()
            state.plan.feedback = (
                f"{previous_feedback}\n\n{target_stage.title()} stage feedback:\n{feedback}"
                if previous_feedback
                else f"{target_stage.title()} stage feedback:\n{feedback}"
            )
        if state.qa_report and target_stage == "qa":
            state.qa_report.feedback = feedback
            state.qa_report.approved = False

        state.error_message = None
        state.failed_stage = None
        state.pr_number = None
        state.pr_url = None

        if target_stage == "triage":
            state.plan = None
            state.test_results = None
            state.qa_report = None
            state.status = WorkflowStatus.TRIAGE
            queued_stage = "triage"
        elif not state.plan:
            state.test_results = None
            state.qa_report = None
            state.status = WorkflowStatus.PLANNING
            queued_stage = "planning"
        else:
            # Founder rejection usually means the implementation needs another
            # pass. Route through Builder so testing, review, and QA all rerun.
            state.test_results = None
            state.qa_report = None
            state.status = WorkflowStatus.BUILDING
            queued_stage = "building"

        self.store.save_state(state)
        self._add_issue_comment(
            repo_name,
            issue_number,
            f"📝 Founder feedback received for **{target_stage.title()}** stage:\n"
            f"> {feedback}\n\n"
            f"🔄 Reworking from **{queued_stage.title()}** stage..."
        )
        self._enqueue_stage(session_id, queued_stage, {"feedback_stage": target_stage})

    async def restart_from_stage(self, session_id: str, target_stage: str) -> None:
        """Restarts a workflow from a specific stage, regardless of current state."""
        state = self.store.load_state(session_id)
        if not state:
            return
            
        repo_name = state.issue.repository
        issue_number = state.issue.number
        state.error_message = None
        state.failed_stage = None

        stage_map = {
            "triage": (WorkflowStatus.TRIAGE, self._restart_triage),
            "planning": (WorkflowStatus.PLANNING, self._restart_planning),
            "building": (WorkflowStatus.BUILDING, self._restart_building),
            "testing": (WorkflowStatus.TESTING, self._restart_testing),
            "qa": (WorkflowStatus.QA, self._restart_qa),
            "deploy": (WorkflowStatus.DEPLOYING, self._restart_deploy),
        }
        
        if target_stage not in stage_map:
            return
            
        new_status, _restart_fn = stage_map[target_stage]
        if target_stage == "triage":
            state.plan = None
            state.test_results = None
            state.qa_report = None
        elif target_stage == "planning":
            state.plan = None
            state.test_results = None
            state.qa_report = None
        elif target_stage == "building":
            state.test_results = None
            state.qa_report = None
        elif target_stage == "testing":
            state.qa_report = None
        state.status = new_status
        self.store.save_state(state)
        
        self._add_issue_comment(repo_name, issue_number, f"🔄 Restarting workflow from **{target_stage.title()}** stage...")
        self._enqueue_stage(session_id, target_stage)

    async def _restart_triage(self, state: WorkflowStateModel) -> None:
        state.plan = None
        state.test_results = None
        state.qa_report = None
        self.store.save_state(state)
        await self._run_from_triage(state)

    async def _restart_planning(self, state: WorkflowStateModel) -> None:
        state.plan = None
        state.test_results = None
        state.qa_report = None
        self.store.save_state(state)
        await self._run_from_planning(state)

    async def _restart_building(self, state: WorkflowStateModel) -> None:
        state.test_results = None
        state.qa_report = None
        self.store.save_state(state)
        await self.run_build_test_review_flow(state, start_at="building")

    async def _restart_testing(self, state: WorkflowStateModel) -> None:
        state.qa_report = None
        self.store.save_state(state)
        await self.run_build_test_review_flow(state, start_at="testing")

    async def _restart_qa(self, state: WorkflowStateModel) -> None:
        self.store.save_state(state)
        await self.run_build_test_review_flow(state, start_at="qa")

    async def _restart_deploy(self, state: WorkflowStateModel) -> None:
        self.store.save_state(state)
        await self.run_deploy_step(state)
