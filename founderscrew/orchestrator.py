import os
import re
import json
import asyncio
import logging
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
    QAReportModel
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
from founderscrew.tools.shell_tools import start_dev_server, stop_dev_server
from founderscrew.tools.screenshot_tools import capture_screenshot
from founderscrew.tools.repo_profile import get_repo_memory, add_repo_lesson, format_repo_memory
from founderscrew.tools.model_routing import filter_available_tiers, apply_provider_env
from pathlib import Path

class Orchestrator:
    """Orchestrates events and state transitions for the Founders.crew DevOps agent workflow."""

    def __init__(self):
        self.store = StateStore()
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
                logger.warning(f"Agent {agent.name} failed with model {model_tier} (Tier {i+1}). Error: {error_msg}")
                last_error = error_msg
                
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
        await self._run_from_triage(state)

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
                "title": state.issue.title,
                "body": state.issue.body,
                "creator": state.issue.creator,
                "labels": state.issue.labels,
                "comments": [],
                "repo_context": repo_memory
            }
            triage_data = await self._run_agent_json(get_triage_agent, session_id, issue_details)

            if not triage_data:
                raise ValueError("Triage agent output could not be parsed as JSON.")

            state.issue.classification = triage_data.get("classification", "bug")
            state.issue.complexity = triage_data.get("complexity", "medium")
            state.issue.affected_files = triage_data.get("affected_files", [])
            state.status = WorkflowStatus.PLANNING
            self.store.save_state(state)
        except Exception as e:
            self._fail(state, "triage", f"Triage stage failed: {e}")
            github_add_comment(repo_name, issue_number, f"❌ Triage failed: {e}")
            return

        await self._run_from_planning(state)

    async def _run_from_planning(self, state: WorkflowStateModel) -> None:
        session_id = state.session_id
        repo_name = state.issue.repository
        issue_number = state.issue.number
        
        # 5. Execute Planning
        try:
            planner_input = {
                "issue": state.issue.model_dump(),
                "repo_context": self._repo_memory_text(repo_name)
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
                
            comment_body += (
                f"\n---\n"
                f"👉 **Founder Approval Required:** Please reply with **approve** or **lgtm** to begin building."
            )
            github_add_comment(repo_name, issue_number, comment_body)

        except Exception as e:
            self._fail(state, "planning", f"Planning stage failed: {e}")
            github_add_comment(repo_name, issue_number, f"❌ Planning failed: {e}")

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

                github_add_comment(repo_name, issue_number, "⚡ Plan approved! Starting the coding and testing cycle...")
                # Trigger build-test-review flow asynchronously
                asyncio.create_task(self.run_build_test_review_flow(state))

            # 2. Handle QA Approval Gate
            elif state.status == WorkflowStatus.AWAIT_QA_APPROVAL:
                logger.info(f"QA report approved by {commenter} for issue {issue_number}")
                state.qa_report.approved = True
                state.status = WorkflowStatus.DEPLOYING
                self.store.save_state(state)

                github_add_comment(repo_name, issue_number, "🚀 QA Approved! Opening the Pull Request...")
                # Trigger deploy step
                asyncio.create_task(self.run_deploy_step(state))

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
        self.store.save_state(state)
        return passed, output

    async def _builder_fix(self, state: WorkflowStateModel, instruction_text: str) -> None:
        """Sends a corrective instruction to the Builder and pushes the result."""
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
        if build_data and build_data.get("test_command"):
            state.test_command = build_data.get("test_command")
            self.store.save_state(state)
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
                if build_data and build_data.get("test_command"):
                    state.test_command = build_data.get("test_command")

                # Persist the work to the feature branch before the human gates
                self._push_workspace_progress(state, "wip")

                state.status = WorkflowStatus.TESTING
                self.store.save_state(state)
                start_at = "testing"
            except Exception as e:
                self._fail(state, "building", f"Building failed: {e}")
                github_add_comment(repo_name, issue_number, f"❌ Building failed: {e}")
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
                while True:
                    passed, output = await self._execute_tests(state)
                    if passed:
                        break
                    last_failure = output
                    if attempt >= max_fix_attempts:
                        raise RuntimeError(
                            f"Automated test execution failed after {attempt + 1} attempt(s):\n{output or 'No detail output.'}"
                        )
                    attempt += 1
                    github_add_comment(
                        repo_name, issue_number,
                        f"🔧 Tests failed (fix attempt {attempt}/{max_fix_attempts}). "
                        f"Founders.crew is feeding the failure back to the Builder to self-heal..."
                    )
                    fix_instruction = (
                        f"The previous code change failed its automated tests. Analyze the test output below, "
                        f"find the root cause, and fix the code (and/or the test if the test itself is wrong).\n"
                        f"Original goal: {state.plan.summary if state.plan else state.issue.title}\n"
                        f"Test command: {state.test_command or 'project default'}\n"
                        f"Test output:\n{output[:3000]}\n"
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

                state.status = WorkflowStatus.REVIEWING
                self.store.save_state(state)
                start_at = "reviewing"
            except Exception as e:
                self._fail(state, "testing", f"Testing failed: {e}")
                github_add_comment(repo_name, issue_number, f"❌ Testing failed: {e}")
                return

        # 4. Execute Reviewing (ReviewerAgent) — advisory, but auto-fixable
        # findings are applied and re-tested
        if start_at == "reviewing":
            review_data = {}
            try:
                review_data = await self._run_agent_json(
                    get_reviewer_agent, session_id,
                    state.test_results.model_dump() if state.test_results else {}
                )
            except Exception as e:
                logger.warning(f"Reviewer agent failed: {e}. Proceeding directly to QA.")

            recommendations = review_data.get("recommendations") or []
            auto_fixable = review_data.get("auto_fixable") or []
            review_passed = review_data.get("passed", True)

            if recommendations:
                rec_lines = "\n".join(f"- {str(r)}" for r in recommendations[:10])
                try:
                    github_add_comment(
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
                        github_add_comment(repo_name, issue_number, f"❌ Testing failed after reviewer auto-fixes:\n```\n{output[:1000]}\n```")
                        return
                except Exception as e:
                    logger.warning(f"Reviewer auto-fix pass failed: {e}. Proceeding to QA with original change.")

            state.status = WorkflowStatus.QA
            self.store.save_state(state)
            start_at = "qa"

        # 5. Execute QA (QAAgent) against a real locally-running dev server
        if start_at == "qa":
            try:
                qa_data = {}
                screenshot_path = None
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
                        # Capture the screenshot deterministically (mock fallback
                        # disallowed — a generated placeholder must never pass as
                        # evidence) and attach the actual image so the multimodal
                        # QA agent judges what the page really looks like
                        shots_dir = Path.home() / ".founderscrew" / "screenshots"
                        shots_dir.mkdir(parents=True, exist_ok=True)
                        candidate = shots_dir / f"{session_id}_qa.png"
                        captured = await asyncio.to_thread(capture_screenshot, str(qa_url), str(candidate), False)

                        if captured:
                            screenshot_path = str(candidate)
                            qa_data = await self._run_agent_json(
                                get_qa_agent, session_id,
                                {
                                    "url": str(qa_url),
                                    "issue_title": state.issue.title,
                                    "note": (
                                        "The screenshot of the rendered page is attached to this message as an image. "
                                        "Inspect it directly and describe concretely what is visible. "
                                        "No reference image is available; evaluate the page on its own."
                                    )
                                },
                                image_paths=[screenshot_path]
                            )
                        else:
                            qa_data = {
                                "passed": False,
                                "observations": f"Could not capture a screenshot of {qa_url}. Please verify the UI manually before approving."
                            }
                    else:
                        qa_data = {
                            "passed": True,
                            "similarity_percentage": 100.0,
                            "observations": "No runnable web UI detected for visual QA; screenshot verification skipped."
                        }
                finally:
                    stop_dev_server(server_proc)

                if not qa_data:
                    # Degrade gracefully: let the human gate decide instead of failing the run
                    qa_data = {
                        "passed": False,
                        "observations": "QA agent could not produce a structured report; please verify the UI manually before approving."
                    }

                state.qa_report = QAReportModel(
                    passed=qa_data.get("passed", True),
                    summary=qa_data.get("observations", "No visual issues detected during QA check."),
                    screenshots=[screenshot_path] if screenshot_path else [],
                    approved=False
                )
                state.status = WorkflowStatus.AWAIT_QA_APPROVAL
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
                    f"👉 **Founder Approval Required:** Please reply with **approve** or **lgtm** to deploy and open Pull Request."
                )
                github_add_comment(repo_name, issue_number, qa_body)

            except Exception as e:
                self._fail(state, "qa", f"QA stage failed: {e}")
                github_add_comment(repo_name, issue_number, f"❌ QA stage failed: {e}")

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
            # Expose accumulative data
            pr_data = {
                "branch_name": state.branch_name,
                "repository": repo_name,
                "issue_number": issue_number,
                "plan_summary": state.plan.summary
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
            github_add_comment(repo_name, issue_number, success_body)

        except Exception as e:
            self._fail(state, "deploy", f"Deployment failed: {e}")
            github_add_comment(repo_name, issue_number, f"❌ Deploy stage failed: {e}")

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

        github_add_comment(repo_name, issue_number, f"🔄 Retrying {stage.title()} stage...")

        if stage == "triage":
            asyncio.create_task(self._run_from_triage(state))
        elif stage == "planning":
            asyncio.create_task(self._run_from_planning(state))
        elif stage == "deploy":
            asyncio.create_task(self.run_deploy_step(state))
        else:
            asyncio.create_task(self.run_build_test_review_flow(state, start_at=stage))

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
        
        github_add_comment(repo_name, issue_number, f"📝 Plan revision requested with feedback:\n> {feedback}\n\n🔄 Re-running Planner Agent...")
        asyncio.create_task(self._run_from_planning(state))

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
            
        new_status, restart_fn = stage_map[target_stage]
        state.status = new_status
        self.store.save_state(state)
        
        github_add_comment(repo_name, issue_number, f"🔄 Restarting workflow from **{target_stage.title()}** stage...")
        asyncio.create_task(restart_fn(state))

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
