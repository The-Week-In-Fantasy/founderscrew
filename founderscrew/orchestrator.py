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
    github_clone_or_pull
)

class Orchestrator:
    """Orchestrates events and state transitions for the Founders.crew DevOps agent workflow."""

    def __init__(self):
        self.store = StateStore()

    async def _run_agent(self, agent_getter, session_id: str, input_data: Any) -> str:
        """Helper to instantiate and run an ADK agent via ADK Runner, trying primary, secondary, and tertiary model tiers if needed."""
        temp_agent = agent_getter()
        agent_key = temp_agent.name.lower().replace("agent", "")
        is_fast_agent = agent_key in ["triage", "tester", "qa", "deployer"]
        
        t1_default = settings.get("agents.fast_model" if is_fast_agent else "agents.planning_model", "gemini-2.5-flash" if is_fast_agent else "gemini-2.5-pro")
        
        tier1 = settings.get(f"agents.{agent_key}.tier1", settings.get("agents.fast_tier1" if is_fast_agent else "agents.planning_tier1", t1_default))
        tier2 = settings.get(f"agents.{agent_key}.tier2", settings.get("agents.fast_tier2" if is_fast_agent else "agents.planning_tier2", None))
        tier3 = settings.get(f"agents.{agent_key}.tier3", settings.get("agents.fast_tier3" if is_fast_agent else "agents.planning_tier3", None))
        
        tiers = [t for t in [tier1, tier2, tier3] if t]
        if not tiers:
            tiers = [t1_default]
            
        valid_tiers = []
        for model_tier in tiers:
            if "openai" in model_tier.lower() and not (settings.get("coding_tools.openai_api_key") or os.environ.get("OPENAI_API_KEY")):
                logger.info(f"Skipping agent tier {model_tier} because OPENAI_API_KEY is not set.")
                continue
            if ("anthropic" in model_tier.lower() or "claude" in model_tier.lower()) and not (settings.get("coding_tools.anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")):
                logger.info(f"Skipping agent tier {model_tier} because ANTHROPIC_API_KEY is not set.")
                continue
            if "gemini" in model_tier.lower() and not (settings.get("google.api_key") or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")):
                logger.info(f"Skipping agent tier {model_tier} because GOOGLE_API_KEY is not set.")
                continue
            valid_tiers.append(model_tier)
            
        if not valid_tiers:
            raise RuntimeError(f"Agent {temp_agent.name} failed because all configured model tiers lacked their required API keys.")
            
        last_error = None
        for i, model_tier in enumerate(valid_tiers):
            logger.info(f"Attempting execution of {temp_agent.name} using model {model_tier} (Tier {i+1}/{len(tiers)})...")
            agent = agent_getter()
            # Strip provider prefix for ADK native runner
            adk_model = model_tier
            if adk_model.startswith("gemini/"):
                adk_model = adk_model.replace("gemini/", "")
            agent.model = adk_model
            
            # Setup environment keys if needed
            g_key = settings.get("google.api_key")
            if g_key:
                os.environ["GOOGLE_API_KEY"] = g_key
                os.environ["GEMINI_API_KEY"] = g_key
                
            openai_key = settings.get("coding_tools.openai_api_key")
            if openai_key:
                os.environ["OPENAI_API_KEY"] = openai_key
                
            anthropic_key = settings.get("coding_tools.anthropic_api_key")
            if anthropic_key:
                os.environ["ANTHROPIC_API_KEY"] = anthropic_key
                
            session_service = InMemorySessionService()
            runner = Runner(agent=agent, session_service=session_service, app_name="founders-crew", auto_create_session=True)
            
            # Build the user message content from the input data
            from google.genai import types as genai_types
            user_message = genai_types.Content(
                role="user",
                parts=[genai_types.Part.from_text(text=str(input_data))]
            )
            
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
            logger.error(f"Failed to parse JSON from agent output: {e}. Output was: {output}")
            return {}

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
        
        # 3. Clone repository local copy
        try:
            github_clone_or_pull(repo_name)
        except Exception as e:
            logger.warning(f"Local clone failed: {e}")

        # 4. Execute Triage
        try:
            issue_details = {
                "title": state.issue.title,
                "body": state.issue.body,
                "creator": state.issue.creator,
                "labels": state.issue.labels,
                "comments": []
            }
            triage_out = await self._run_agent(get_triage_agent, session_id, issue_details)
            triage_data = self._parse_json_from_output(triage_out)
            
            if not triage_data:
                raise ValueError("Triage agent output could not be parsed as JSON.")
                
            state.issue.classification = triage_data.get("classification", "bug")
            state.issue.complexity = triage_data.get("complexity", "medium")
            state.issue.affected_files = triage_data.get("affected_files", [])
            state.status = WorkflowStatus.PLANNING
            self.store.save_state(state)
        except Exception as e:
            state.status = WorkflowStatus.FAILED
            state.error_message = f"Triage stage failed: {e}"
            self.store.save_state(state)
            github_add_comment(repo_name, issue_number, f"❌ Triage failed: {e}")
            return

        await self._run_from_planning(state)

    async def _run_from_planning(self, state: WorkflowStateModel) -> None:
        session_id = state.session_id
        repo_name = state.issue.repository
        issue_number = state.issue.number
        
        # 5. Execute Planning
        try:
            plan_out = await self._run_agent(get_planner_agent, session_id, state.issue.model_dump())
            plan_data = self._parse_json_from_output(plan_out)
            
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
            state.status = WorkflowStatus.FAILED
            state.error_message = f"Planning stage failed: {e}"
            self.store.save_state(state)
            github_add_comment(repo_name, issue_number, f"❌ Planning failed: {e}")

    async def handle_comment_created(self, repo_name: str, issue_number: int, comment_body: str, commenter: str) -> None:
        """Triggered when comment is added. Checks for approvals at gated stages."""
        session_id = f"{repo_name.replace('/', '_')}_{issue_number}"
        state = self.store.load_state(session_id)
        if not state:
            return
            
        comment_clean = comment_body.strip().lower()
        is_approval = "approve" in comment_clean or "lgtm" in comment_clean
        
        # 1. Handle Plan Approval Gate
        if state.status == WorkflowStatus.AWAIT_PLAN_APPROVAL and is_approval:
            logger.info(f"Plan approved by {commenter} for issue {issue_number}")
            state.plan.approved = True
            state.status = WorkflowStatus.BUILDING
            self.store.save_state(state)
            
            github_add_comment(repo_name, issue_number, "⚡ Plan approved! Starting the coding and testing cycle...")
            # Trigger build-test-review flow asynchronously
            asyncio.create_task(self.run_build_test_review_flow(state))
            
        # 2. Handle QA Approval Gate
        elif state.status == WorkflowStatus.AWAIT_QA_APPROVAL and is_approval:
            logger.info(f"QA report approved by {commenter} for issue {issue_number}")
            state.qa_report.approved = True
            state.status = WorkflowStatus.DEPLOYING
            self.store.save_state(state)
            
            github_add_comment(repo_name, issue_number, "🚀 QA Approved! Opening the Pull Request...")
            # Trigger deploy step
            asyncio.create_task(self.run_deploy_step(state))

    async def run_build_test_review_flow(self, state: WorkflowStateModel, start_at: str = "building") -> None:
        """Runs the build-test-review loop."""
        session_id = state.session_id
        repo_name = state.issue.repository
        issue_number = state.issue.number
        
        # 1. Create Feature Branch
        if start_at == "building":
            branch_name = f"founderscrew/fix-issue-{issue_number}"
            state.branch_name = branch_name
            self.store.save_state(state)
        
        # 2. Execute Building (BuilderAgent)
        if start_at == "building":
            try:
                build_instruction = f"Apply plan steps to resolve the issue: {state.plan.summary}\nSteps:\n"
                for step in state.plan.steps:
                    build_instruction += f"- Step {step.step_number}: {step.description}\n"
                build_instruction += f"\nIMPORTANT: You MUST also write a brief, targeted automated test script (e.g. tests/issue_{issue_number}_test.js) that specifically verifies this issue is resolved. Do not rely on the global regression suite.\n"
                    
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
                
                state.status = WorkflowStatus.TESTING
                self.store.save_state(state)
                start_at = "testing"
            except Exception as e:
                state.status = WorkflowStatus.FAILED
                state.error_message = f"Building failed: {e}"
                self.store.save_state(state)
                github_add_comment(repo_name, issue_number, f"❌ Building failed: {e}")
                return
            
        # 3. Execute Testing (TesterAgent)
        if start_at == "testing":
            try:
                # Use targeted test command if available, otherwise fallback
                test_cmd = state.test_command
                if not test_cmd:
                    test_cmd = "pytest"
                    for f in state.issue.affected_files:
                        if f.endswith(".js") or f.endswith(".ts") or f.endswith(".json"):
                            test_cmd = "npm test"
                            break
                        
                input_tester = {
                    "command": test_cmd,
                    "repository": repo_name
                }
                
                test_out = await self._run_agent(get_tester_agent, session_id, input_tester)
                test_data = self._parse_json_from_output(test_out)
                
                if not test_data:
                    raise ValueError("Tester agent output could not be parsed as JSON.")
                    
                state.test_results = TestResultsModel(
                    passed=test_data.get("passed", False),
                    outcomes=[TestOutcome(test_name=test_cmd, passed=test_data.get("passed", False), output=test_data.get("output", ""))]
                )
                if not state.test_results.passed:
                    raise RuntimeError(f"Automated test execution failed:\n{test_data.get('output', 'No detail output.')}")
                    
                state.status = WorkflowStatus.REVIEWING
                self.store.save_state(state)
                start_at = "reviewing"
            except Exception as e:
                state.status = WorkflowStatus.FAILED
                state.error_message = f"Testing failed: {e}"
                self.store.save_state(state)
                github_add_comment(repo_name, issue_number, f"❌ Testing failed: {e}")
                return

        # 4. Execute Reviewing (ReviewerAgent)
        if start_at == "reviewing":
            try:
                review_out = await self._run_agent(get_reviewer_agent, session_id, state.test_results.model_dump())
                state.status = WorkflowStatus.QA
                self.store.save_state(state)
                start_at = "qa"
            except Exception as e:
                logger.warning(f"Reviewer agent failed: {e}. Proceeding directly to QA.")
                state.status = WorkflowStatus.QA
                self.store.save_state(state)
                start_at = "qa"

        # 5. Execute QA (QAAgent)
        if start_at == "qa":
            try:
                # Visual screenshot checks
                qa_out = await self._run_agent(get_qa_agent, session_id, {"url": "https://test.local"})
                qa_data = self._parse_json_from_output(qa_out)
                
                if not qa_data:
                    raise ValueError("QA agent output could not be parsed as JSON.")
                    
                state.qa_report = QAReportModel(
                    passed=qa_data.get("passed", True),
                    summary=qa_data.get("observations", "No visual issues detected during QA check."),
                    approved=False
                )
                state.status = WorkflowStatus.AWAIT_QA_APPROVAL
                self.store.save_state(state)
                
                # Post QA report to issue
                qa_body = (
                    f"### 🔎 Founders.crew QA Report\n\n"
                    f"Test results: {'✅ Passed' if state.test_results.passed else '❌ Failed'}\n"
                    f"Visual check: {'✅ Passed' if state.qa_report.passed else '⚠️ Visual warning'}\n\n"
                    f"**Visual Observations:**\n{state.qa_report.summary}\n\n"
                    f"---\n"
                    f"👉 **Founder Approval Required:** Please reply with **approve** or **lgtm** to deploy and open Pull Request."
                )
                github_add_comment(repo_name, issue_number, qa_body)
                
            except Exception as e:
                state.status = WorkflowStatus.FAILED
                state.error_message = f"QA stage failed: {e}"
                self.store.save_state(state)
                github_add_comment(repo_name, issue_number, f"❌ QA stage failed: {e}")

    async def run_deploy_step(self, state: WorkflowStateModel) -> None:
        """Runs the final deployer agent to create PR."""
        session_id = state.session_id
        repo_name = state.issue.repository
        issue_number = state.issue.number
        
        try:
            # Expose accumulative data
            pr_data = {
                "branch_name": state.branch_name,
                "repository": repo_name,
                "issue_number": issue_number,
                "plan_summary": state.plan.summary
            }
            
            deploy_out = await self._run_agent(get_deployer_agent, session_id, pr_data)
            deploy_data = self._parse_json_from_output(deploy_out)
            
            if not deploy_data:
                raise ValueError("Deployer agent output could not be parsed as JSON.")
                
            pr_url = deploy_data.get("pr_url", "")
            state.pr_url = pr_url
            state.status = WorkflowStatus.AWAIT_PR_APPROVAL
            self.store.save_state(state)
            
            success_body = (
                f"🎉 **Founders.crew has opened a Pull Request!**\n\n"
                f"Review the code changes and merge the PR here:\n"
                f"👉 **PR Link:** {pr_url or 'Created'}"
            )
            github_add_comment(repo_name, issue_number, success_body)
            
        except Exception as e:
            state.status = WorkflowStatus.FAILED
            state.error_message = f"Deployment failed: {e}"
            self.store.save_state(state)
            github_add_comment(repo_name, issue_number, f"❌ Deploy stage failed: {e}")

    async def resume_failed_workflow(self, session_id: str) -> None:
        """Resumes a failed workflow from the stage where it failed."""
        state = self.store.load_state(session_id)
        if not state or state.status != WorkflowStatus.FAILED:
            return
            
        err = state.error_message or ""
        repo_name = state.issue.repository
        issue_number = state.issue.number
        
        # Reset error message and save state
        state.error_message = None
        
        if "Triage stage failed" in err:
            state.status = WorkflowStatus.TRIAGE
            self.store.save_state(state)
            github_add_comment(repo_name, issue_number, "🔄 Retrying Triage stage...")
            asyncio.create_task(self._run_from_triage(state))
            
        elif "Planning stage failed" in err:
            state.status = WorkflowStatus.PLANNING
            self.store.save_state(state)
            github_add_comment(repo_name, issue_number, "🔄 Retrying Planning stage...")
            asyncio.create_task(self._run_from_planning(state))
            
        elif "Building failed" in err:
            state.status = WorkflowStatus.BUILDING
            self.store.save_state(state)
            github_add_comment(repo_name, issue_number, "🔄 Retrying Building stage...")
            asyncio.create_task(self.run_build_test_review_flow(state, start_at="building"))
            
        elif "Testing failed" in err:
            state.status = WorkflowStatus.TESTING
            self.store.save_state(state)
            github_add_comment(repo_name, issue_number, "🔄 Retrying Testing stage...")
            asyncio.create_task(self.run_build_test_review_flow(state, start_at="testing"))
            
        elif "QA stage failed" in err:
            state.status = WorkflowStatus.QA
            self.store.save_state(state)
            github_add_comment(repo_name, issue_number, "🔄 Retrying QA stage...")
            asyncio.create_task(self.run_build_test_review_flow(state, start_at="qa"))
            
        elif "Deployment failed" in err:
            state.status = WorkflowStatus.DEPLOYING
            self.store.save_state(state)
            github_add_comment(repo_name, issue_number, "🔄 Retrying Deploy stage...")
            asyncio.create_task(self.run_deploy_step(state))
            
        else:
            # Default fallback: restart from triage
            state.status = WorkflowStatus.TRIAGE
            self.store.save_state(state)
            github_add_comment(repo_name, issue_number, "🔄 Retrying from Triage stage...")
            asyncio.create_task(self._run_from_triage(state))

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
