import pytest
import subprocess
from unittest.mock import patch, AsyncMock
from founderscrew.state.models import (
    ImplementationPlanModel,
    QAReportModel,
    QualityGateResult,
    TestOutcome as OutcomeModel,
    TestResultsModel as ResultsModel,
    WorkflowStatus,
    WorkflowStateModel,
    IssueContext,
)
from founderscrew.orchestrator import Orchestrator
from founderscrew.workflow_queue import WorkflowQueue

@pytest.fixture
def temp_db_path(tmp_path):
    return tmp_path / "test_orchestrator.db"

@pytest.mark.anyio
async def test_handle_issue_labeled_success(temp_db_path):
    """Verifies that issue labeling queues, then worker execution runs triage/planning."""
    orch = Orchestrator()
    
    # Mock settings and store paths
    with patch("founderscrew.config.settings.get", return_value=temp_db_path):
        orch.store.sqlite_db_path = temp_db_path
        orch.store._init_sqlite()  # Reinit SQLite db in temp folder
        orch.queue = WorkflowQueue()
        
        # Mock github fetch tools
        mock_issue = {
            "title": "Fix buggy loop",
            "body": "There is a bug in the loop",
            "creator": "founder-bob",
            "labels": ["crew:ready"],
            "comments": []
        }
        
        with patch("founderscrew.orchestrator.github_get_issue", return_value=mock_issue) as mock_get, \
             patch("founderscrew.orchestrator.github_add_comment") as mock_comment, \
             patch("founderscrew.orchestrator.github_clone_or_pull") as mock_clone:
                 
            # Mock ADK agent runs
            # We must mock _run_agent which is an async method on Orchestrator
            triage_mock_output = '{"classification": "bug", "affected_files": ["main.py"], "complexity": "low", "reason": "simple"}'
            plan_mock_output = '{"summary": "Plan to fix loop", "steps": [{"step_number": 1, "description": "fix loop", "files_affected": ["main.py"]}]}'
            
            with patch.object(orch, "_run_agent") as mock_run_agent:
                # Return triage result first, then plan result
                mock_run_agent.side_effect = [
                    triage_mock_output,
                    plan_mock_output
                ]
                
                await orch.handle_issue_labeled("owner/repo", 42, "bob")
                queued = orch.queue.claim_next()
                assert queued is not None
                assert queued.session_id == "owner_repo_42"
                assert queued.stage == "triage"

                await orch.run_queued_stage(queued.session_id, queued.stage)
                
                # Check status was updated to AWAIT_PLAN_APPROVAL
                state = orch.store.load_state("owner_repo_42")
                assert state is not None
                assert state.status == WorkflowStatus.AWAIT_PLAN_APPROVAL
                assert state.plan is not None
                assert state.plan.summary == "Plan to fix loop"
                assert len(state.plan.steps) == 1
                assert state.issue.classification == "bug"
                assert state.issue.complexity == "low"
                
                mock_get.assert_called_once_with("owner/repo", 42)
                mock_comment.assert_called()
                mock_clone.assert_any_call("owner/repo")

@pytest.mark.anyio
async def test_handle_comment_created_approval(temp_db_path):
    """Verifies that an approval comment transitions state and triggers building flow."""
    orch = Orchestrator()
    
    with patch("founderscrew.config.settings.get", return_value=temp_db_path):
        orch.store.sqlite_db_path = temp_db_path
        orch.store._init_sqlite()
        orch.queue = WorkflowQueue()
        
        # Pre-seed state as AWAIT_PLAN_APPROVAL
        from founderscrew.state.models import IssueContext, ImplementationPlanModel, PlanStep
        issue = IssueContext(number=100, title="T", creator="c", repository="o/r")
        state = WorkflowStateModel(
            session_id="o_r_100",
            issue=issue,
            status=WorkflowStatus.AWAIT_PLAN_APPROVAL,
            plan=ImplementationPlanModel(summary="Sum", steps=[PlanStep(step_number=1, description="D")])
        )
        orch.store.save_state(state)
        
        # Mock github and queue handoff
        with patch("founderscrew.orchestrator.github_add_comment") as mock_comment, \
             patch("founderscrew.orchestrator.github_get_bot_login", return_value="founders-crew-bot"):

            # The bot's own comment (containing the keyword) must never approve
            await orch.handle_comment_created("o/r", 100, "Please reply with approve or lgtm", "founders-crew-bot")
            assert orch.store.load_state("o_r_100").status == WorkflowStatus.AWAIT_PLAN_APPROVAL

            # A comment merely mentioning approval must not approve
            await orch.handle_comment_created("o/r", 100, "I don't approve of this yet", "c")
            assert orch.store.load_state("o_r_100").status == WorkflowStatus.AWAIT_PLAN_APPROVAL

            # An unauthorized commenter must not approve
            await orch.handle_comment_created("o/r", 100, "approve", "random-stranger")
            assert orch.store.load_state("o_r_100").status == WorkflowStatus.AWAIT_PLAN_APPROVAL

            # The issue creator leading with the keyword approves
            await orch.handle_comment_created("o/r", 100, "Approve - this plan looks good", "c")

            # Verify status transitioned to BUILDING
            updated_state = orch.store.load_state("o_r_100")
            assert updated_state.status == WorkflowStatus.BUILDING
            assert updated_state.plan.approved is True
            mock_comment.assert_called_once()
            queued = orch.queue.claim_next()
            assert queued is not None
            assert queued.session_id == "o_r_100"
            assert queued.stage == "building"

@pytest.mark.anyio
async def test_planning_records_quality_contract(temp_db_path):
    orch = Orchestrator()

    with patch("founderscrew.config.settings.get", return_value=temp_db_path):
        orch.store.sqlite_db_path = temp_db_path
        orch.store._init_sqlite()

        issue = IssueContext(
            number=3,
            title="Update setup docs",
            body="The setup instructions are stale.",
            creator="c",
            repository="o/r",
            affected_files=["README.md"],
        )
        state = WorkflowStateModel(session_id="o_r_3", issue=issue, status=WorkflowStatus.PLANNING)
        orch.store.save_state(state)

        plan_output = (
            '{"summary": "Update setup docs", '
            '"acceptance_criteria": ["README shows the new setup command"], '
            '"risk_level": "low", "ui_qa_required": false, "docs_required": true, '
            '"expected_test_commands": ["pytest"], '
            '"steps": [{"step_number": 1, "description": "edit README", "files_affected": ["README.md"]}]}'
        )

        with patch("founderscrew.orchestrator.github_add_comment"), \
             patch("founderscrew.orchestrator.github_clone_or_pull", return_value=str(temp_db_path.parent)), \
             patch.object(orch, "_run_agent", new_callable=AsyncMock, return_value=plan_output):

            await orch._run_from_planning(state)

        updated = orch.store.load_state("o_r_3")
        assert updated.status == WorkflowStatus.AWAIT_PLAN_APPROVAL
        assert updated.acceptance_criteria == ["README shows the new setup command"]
        assert updated.risk_level == "low"
        assert updated.ui_qa_required is False
        assert updated.docs_required is True


@pytest.mark.anyio
async def test_build_test_self_heal_loop(temp_db_path):
    """A failing test run is fed back to the Builder, then re-tested to green."""
    orch = Orchestrator()

    with patch("founderscrew.config.settings.get", return_value=temp_db_path):
        orch.store.sqlite_db_path = temp_db_path
        orch.store._init_sqlite()
        orch.queue = WorkflowQueue()

        from founderscrew.state.models import ImplementationPlanModel
        issue = IssueContext(number=7, title="Fix widget", creator="c", repository="o/r", affected_files=["a.js"])
        state = WorkflowStateModel(
            session_id="o_r_7",
            issue=issue,
            status=WorkflowStatus.TESTING,
            plan=ImplementationPlanModel(summary="Fix the widget", steps=[]),
            branch_name="founderscrew/fix-issue-7"
        )
        orch.store.save_state(state)

        with patch("founderscrew.orchestrator.github_add_comment"), \
             patch("founderscrew.orchestrator.github_clone_or_pull", return_value=str(temp_db_path.parent)), \
             patch("founderscrew.orchestrator.github_push_workspace", return_value={"success": True}), \
             patch("founderscrew.orchestrator.set_active_workspace_branch"), \
             patch("founderscrew.orchestrator.capture_screenshot", return_value=True), \
             patch("founderscrew.orchestrator.analyze_screenshot", return_value={"ok": True, "is_blank": False}), \
             patch("founderscrew.orchestrator.get_repo_memory", return_value={"profile": None, "lessons": []}), \
             patch("founderscrew.orchestrator.add_repo_lesson") as mock_lesson, \
             patch.object(orch, "_run_agent") as mock_run_agent:

            mock_run_agent.side_effect = [
                '{"passed": false, "output": "1 test failed: widget is broken"}',   # tester: red
                '{"summary": "fixed widget", "test_command": "npm test"}',          # builder: fix pass
                '{"passed": true, "output": "all tests passed"}',                   # tester: green
                '{"passed": true, "recommendations": [], "auto_fixable": []}',      # reviewer
                '{"passed": true, "similarity_percentage": 100.0, "observations": "renders fine"}',  # qa
            ]

            await orch.run_build_test_review_flow(state, start_at="testing")

            final = orch.store.load_state("o_r_7")
            assert final.status == WorkflowStatus.DEPLOYING
            assert final.test_results.passed is True
            assert final.test_command == "npm test"
            assert mock_run_agent.call_count == 5
            # QA screenshot is recorded on the report for the dashboard
            assert final.qa_report is not None
            assert len(final.qa_report.screenshots) == 1
            assert final.qa_report.screenshots[0].endswith("o_r_7_qa.png")
            queued = orch.queue.claim_next()
            assert queued is not None
            assert queued.stage == "deploy"
            # The self-heal was recorded as an episodic repo lesson
            mock_lesson.assert_called_once()
            lesson = mock_lesson.call_args[0][2]
            assert lesson["issue"] == 7
            assert "self-healed" in lesson["summary"]

@pytest.mark.anyio
async def test_qa_blank_screenshot_self_heals_before_approval(temp_db_path):
    """A blank QA screenshot is diagnosed, sent back to Builder, then recaptured."""
    orch = Orchestrator()

    def settings_get(key, default=None):
        if key == "state.db_path":
            return temp_db_path
        if key == "qa.target_url":
            return "http://localhost:3001"
        if key == "qa.max_visual_fix_attempts":
            return 1
        return default

    with patch("founderscrew.config.settings.get", side_effect=settings_get):
        orch.store.sqlite_db_path = temp_db_path
        orch.store._init_sqlite()
        orch.queue = WorkflowQueue()

        issue = IssueContext(number=7, title="Fix widget", creator="c", repository="o/r", affected_files=["a.js"])
        state = WorkflowStateModel(
            session_id="o_r_7",
            issue=issue,
            status=WorkflowStatus.QA,
            plan=ImplementationPlanModel(summary="Fix the widget", steps=[]),
            test_results=ResultsModel(
                passed=True,
                outcomes=[OutcomeModel(test_name="npm test", passed=True, output="ok")],
            ),
            branch_name="founderscrew/fix-issue-7",
        )
        orch.store.save_state(state)

        with patch("founderscrew.orchestrator.github_add_comment"), \
             patch("founderscrew.orchestrator.github_clone_or_pull", return_value=str(temp_db_path.parent)), \
             patch("founderscrew.orchestrator.start_dev_server", return_value=(None, "http://localhost:3001")), \
             patch("founderscrew.orchestrator.stop_dev_server"), \
             patch("founderscrew.orchestrator.capture_screenshot", return_value=True) as mock_capture, \
             patch("founderscrew.orchestrator.analyze_screenshot") as mock_analyze, \
             patch("founderscrew.orchestrator.diagnose_page_render") as mock_diagnose, \
             patch.object(orch, "_builder_fix", new_callable=AsyncMock) as mock_builder_fix, \
             patch.object(orch, "_execute_tests", new_callable=AsyncMock, return_value=(True, "ok")) as mock_tests, \
             patch.object(orch, "_run_agent") as mock_run_agent:

            mock_analyze.side_effect = [
                {
                    "ok": True,
                    "is_blank": True,
                    "reason": "100.0% of sampled pixels are the same color",
                    "unique_color_count": 1,
                    "dominant_color_ratio": 1.0,
                    "color_variance": 0.0,
                },
                {
                    "ok": True,
                    "is_blank": False,
                    "reason": "",
                    "unique_color_count": 40,
                    "dominant_color_ratio": 0.4,
                    "color_variance": 30.0,
                },
            ]
            mock_diagnose.return_value = {
                "ok": True,
                "status": 200,
                "finalUrl": "http://localhost:3001",
                "title": "",
                "bodyTextLength": 0,
                "consoleErrors": ["error: app crashed"],
                "pageErrors": [],
                "failedRequests": [],
            }
            mock_run_agent.return_value = '{"passed": true, "similarity_percentage": 100.0, "observations": "renders after fix"}'

            await orch.run_build_test_review_flow(state, start_at="qa")

            final = orch.store.load_state("o_r_7")
            assert final.status == WorkflowStatus.DEPLOYING
            assert final.qa_report is not None
            assert "renders after fix" in final.qa_report.summary
            assert mock_capture.call_count == 2
            mock_diagnose.assert_called_once()
            mock_builder_fix.assert_awaited_once()
            mock_tests.assert_awaited_once()
            queued = orch.queue.claim_next()
            assert queued is not None
            assert queued.stage == "deploy"

@pytest.mark.anyio
async def test_qa_uses_inferred_route_for_initial_capture_and_agent_context(temp_db_path, tmp_path):
    orch = Orchestrator()
    (tmp_path / "src/components").mkdir(parents=True)
    (tmp_path / "src/pages").mkdir(parents=True)
    (tmp_path / "src/App.jsx").write_text(
        """
import DashboardPage from './pages/DashboardPage';
import DraftAssistantPage from './pages/DraftAssistantPage';
<Route path="/dashboard" element={<DashboardPage />} />
<Route path="/draft" element={<FeatureFlagGate><DraftAssistantPage /></FeatureFlagGate>} />
""",
        encoding="utf-8",
    )
    (tmp_path / "src/components/DraftPlayerBoard.jsx").write_text(
        "export default function DraftPlayerBoard() { return null; }\n",
        encoding="utf-8",
    )
    (tmp_path / "src/pages/DraftAssistantPage.jsx").write_text(
        "import DraftPlayerBoard from '../components/DraftPlayerBoard';\n"
        "export default function DraftAssistantPage() { return <DraftPlayerBoard />; }\n",
        encoding="utf-8",
    )

    def settings_get(key, default=None):
        if key == "state.db_path":
            return temp_db_path
        if key == "qa.target_url":
            return "http://localhost:3001"
        return default

    with patch("founderscrew.config.settings.get", side_effect=settings_get):
        orch.store.sqlite_db_path = temp_db_path
        orch.store._init_sqlite()
        orch.queue = WorkflowQueue()

        state = WorkflowStateModel(
            session_id="o_r_327",
            issue=IssueContext(
                number=327,
                title="Summaries cut off on DraftPlayerBoard in draft assistant",
                creator="c",
                repository="o/r",
                affected_files=["src/components/DraftPlayerBoard.jsx"],
            ),
            status=WorkflowStatus.QA,
            plan=ImplementationPlanModel(summary="Fix DraftPlayerBoard summaries", steps=[]),
            test_results=ResultsModel(
                passed=True,
                outcomes=[OutcomeModel(test_name="npm test", passed=True, output="ok")],
            ),
        )
        orch.store.save_state(state)

        with patch("founderscrew.orchestrator.github_add_comment"), \
             patch("founderscrew.orchestrator.github_clone_or_pull", return_value=str(tmp_path)), \
             patch("founderscrew.orchestrator.stop_dev_server"), \
             patch("founderscrew.orchestrator.capture_screenshot", return_value=True) as mock_capture, \
             patch("founderscrew.orchestrator.analyze_screenshot", return_value={"ok": True, "is_blank": False}), \
             patch.object(
                 orch,
                 "_run_agent",
                 new_callable=AsyncMock,
                 return_value='{"passed": true, "similarity_percentage": 100.0, "observations": "verified"}',
             ) as mock_run_agent:

            await orch.run_build_test_review_flow(state, start_at="qa")

    assert mock_capture.call_args.args[0] == "http://localhost:3001/draft"
    qa_input = mock_run_agent.await_args.args[2]
    assert qa_input["qa_target_path"] == "/draft"
    assert qa_input["qa_allowed_paths"] == ["/draft"]
    assert "/draft" in qa_input["qa_route_candidates"]
    assert "/dashboard" not in qa_input["qa_route_candidates"]

@pytest.mark.anyio
async def test_builder_fix_receives_and_records_change_context(temp_db_path):
    orch = Orchestrator()

    with patch("founderscrew.config.settings.get", return_value=temp_db_path):
        orch.store.sqlite_db_path = temp_db_path
        orch.store._init_sqlite()

        issue = IssueContext(number=9, title="Fix widget", creator="c", repository="o/r", affected_files=["a.js"])
        state = WorkflowStateModel(
            session_id="o_r_9",
            issue=issue,
            status=WorkflowStatus.TESTING,
            plan=ImplementationPlanModel(summary="Fix the widget", steps=[]),
            branch_name="founderscrew/fix-issue-9",
            modified_files=["a.js"],
            build_summaries=["Initial build: changed widget render path"],
            test_failure_history=["Attempt 1: expected text was missing"],
        )
        orch.store.save_state(state)

        with patch("founderscrew.orchestrator.github_push_workspace", return_value={"success": True}), \
             patch.object(orch, "_repo_memory_text", return_value=""), \
             patch.object(orch, "_run_agent", new_callable=AsyncMock) as mock_run_agent:

            mock_run_agent.return_value = (
                '{"summary": "kept widget fix and added fallback", '
                '"modified_files": ["b.js"], "test_command": "npm test -- widget"}'
            )

            await orch._builder_fix(state, "Fix the current failure.")

            builder_input = mock_run_agent.await_args.args[2]
            assert "Existing workflow change context" in builder_input["instruction"]
            assert "a.js" in builder_input["instruction"]
            assert "Initial build: changed widget render path" in builder_input["instruction"]
            assert "expected text was missing" in builder_input["instruction"]

            updated = orch.store.load_state("o_r_9")
            assert updated.modified_files == ["a.js", "b.js"]
            assert updated.test_command == "npm test -- widget"
            assert "Fix pass: kept widget fix and added fallback" in updated.build_summaries

@pytest.mark.anyio
async def test_artifact_hygiene_self_heals_tracked_generated_file(tmp_path):
    orch = Orchestrator()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    artifact = tmp_path / "playwright.config.js"
    artifact.write_text("module.exports = {};\n", encoding="utf-8")
    subprocess.run(["git", "add", "playwright.config.js"], cwd=tmp_path, check=True, capture_output=True, text=True)

    state = WorkflowStateModel(
        session_id="o_r_artifact",
        issue=IssueContext(number=11, title="Fix artifact", creator="c", repository="o/r"),
        status=WorkflowStatus.TESTING,
        plan=ImplementationPlanModel(summary="Fix artifact", steps=[]),
    )

    with patch("founderscrew.orchestrator.github_add_comment"), \
         patch.object(orch, "_builder_fix", new_callable=AsyncMock) as mock_builder:
        healed = await orch._run_quality_gate_with_self_heal(
            state,
            "artifact_hygiene",
            lambda attempt, summary: orch._run_artifact_quality_gate(state, str(tmp_path), attempt, summary),
            str(tmp_path),
        )

    tracked = subprocess.run(
        ["git", "ls-files", "--", "playwright.config.js"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert healed is True
    assert tracked == ""
    assert artifact.exists()
    assert any(g.name == "artifact_hygiene" and not g.passed for g in state.quality_gates)
    assert any(g.name == "artifact_hygiene" and g.passed for g in state.quality_gates)
    mock_builder.assert_not_awaited()

def test_artifact_hygiene_rejects_unsafe_untrack_path():
    orch = Orchestrator()
    with patch("founderscrew.orchestrator.subprocess.run") as mock_run:
        ok, summary = orch._untrack_generated_artifacts("repo", ["src/app.js"])

    assert ok is False
    assert "Refusing to untrack non-generated artifact paths" in summary
    mock_run.assert_not_called()

def test_artifact_hygiene_reports_git_untrack_failure():
    orch = Orchestrator()
    fake_result = type("Result", (), {"returncode": 1, "stdout": "", "stderr": "index is locked"})()

    with patch("founderscrew.orchestrator.subprocess.run", return_value=fake_result):
        ok, summary = orch._untrack_generated_artifacts("repo", ["playwright.config.js"])

    assert ok is False
    assert "index is locked" in summary

@pytest.mark.anyio
async def test_quality_docs_gate_self_heals_then_reruns_tests(temp_db_path):
    orch = Orchestrator()

    with patch("founderscrew.config.settings.get", return_value=temp_db_path):
        orch.store.sqlite_db_path = temp_db_path
        orch.store._init_sqlite()

        issue = IssueContext(number=10, title="Document config", creator="c", repository="o/r")
        state = WorkflowStateModel(
            session_id="o_r_10",
            issue=issue,
            status=WorkflowStatus.TESTING,
            plan=ImplementationPlanModel(summary="Document config", steps=[]),
            branch_name="founderscrew/fix-issue-10",
            docs_required=True,
            ui_qa_required=False,
            acceptance_criteria=["README explains the config"],
        )
        orch.store.save_state(state)

        async def fake_builder_fix(target_state, _instruction):
            target_state.modified_files.append("README.md")
            orch.store.save_state(target_state)

        with patch("founderscrew.orchestrator.github_clone_or_pull", return_value=str(temp_db_path.parent)), \
             patch("founderscrew.orchestrator.github_add_comment"), \
             patch.object(orch, "_repo_profile", return_value={"docs_paths": ["README.md"]}), \
             patch.object(orch, "_run_artifact_quality_gate", side_effect=lambda state, workdir, attempt=1, remediation_summary="": orch._record_quality_gate(state, "artifact_hygiene", "", True, "ok", attempt)), \
             patch.object(orch, "_builder_fix", new=AsyncMock(side_effect=fake_builder_fix)) as mock_fix, \
             patch.object(orch, "_execute_tests", new_callable=AsyncMock, return_value=(True, "ok")) as mock_tests:

            passed = await orch._run_quality_gates(state)

        updated = orch.store.load_state("o_r_10")
        assert passed is True
        assert updated.status == WorkflowStatus.TESTING
        assert any(g.name == "docs" and not g.passed for g in updated.quality_gates)
        assert any(g.name == "docs" and g.passed for g in updated.quality_gates)
        mock_fix.assert_awaited_once()
        mock_tests.assert_awaited_once()

@pytest.mark.anyio
async def test_reject_stage_with_feedback_queues_builder_rework(temp_db_path):
    orch = Orchestrator()

    with patch("founderscrew.config.settings.get", return_value=temp_db_path):
        orch.store.sqlite_db_path = temp_db_path
        orch.store._init_sqlite()
        orch.queue = WorkflowQueue()

        issue = IssueContext(number=7, title="Fix widget", creator="c", repository="o/r")
        state = WorkflowStateModel(
            session_id="o_r_7",
            issue=issue,
            status=WorkflowStatus.AWAIT_QA_APPROVAL,
            plan=ImplementationPlanModel(summary="Fix the widget", steps=[]),
        )
        orch.store.save_state(state)

        with patch("founderscrew.orchestrator.github_add_comment"):
            await orch.reject_stage_with_feedback("o_r_7", "qa", "The screenshot is blank.")

        updated = orch.store.load_state("o_r_7")
        assert updated.status == WorkflowStatus.BUILDING
        assert "The screenshot is blank." in updated.plan.feedback
        queued = orch.queue.claim_next()
        assert queued is not None
        assert queued.stage == "building"
        assert queued.payload["feedback_stage"] == "qa"

@pytest.mark.anyio
async def test_resume_failed_workflow(temp_db_path):
    """Verifies that resume_failed_workflow picks up at the correct step based on error message."""
    orch = Orchestrator()
    
    with patch("founderscrew.config.settings.get", return_value=temp_db_path):
        orch.store.sqlite_db_path = temp_db_path
        orch.store._init_sqlite()
        orch.queue = WorkflowQueue()
        
        # Test case 1: Triage failed resumes triage
        issue = IssueContext(number=100, title="T", creator="c", repository="o/r")
        state = WorkflowStateModel(
            session_id="o_r_100",
            issue=issue,
            status=WorkflowStatus.FAILED,
            error_message="Triage stage failed: some error"
        )
        orch.store.save_state(state)
        
        with patch("founderscrew.orchestrator.github_add_comment") as mock_comment, \
             patch.object(orch, "_run_from_triage", new_callable=AsyncMock) as mock_triage:
            await orch.resume_failed_workflow("o_r_100")
            assert orch.store.load_state("o_r_100").status == WorkflowStatus.TRIAGE
            mock_comment.assert_called_once()
            queued = orch.queue.claim_next()
            assert queued is not None
            assert queued.stage == "triage"
            mock_triage.assert_not_called()

        # Test case 2: Testing failed resumes testing
        state.status = WorkflowStatus.FAILED
        state.error_message = "Testing failed: run failed"
        orch.store.save_state(state)
        
        with patch("founderscrew.orchestrator.github_add_comment") as mock_comment, \
             patch.object(orch, "run_build_test_review_flow", new_callable=AsyncMock) as mock_flow:
            await orch.resume_failed_workflow("o_r_100")
            assert orch.store.load_state("o_r_100").status == WorkflowStatus.TESTING
            mock_comment.assert_called_once()
            queued = orch.queue.claim_next()
            assert queued is not None
            assert queued.stage == "testing"
            mock_flow.assert_not_called()

@pytest.mark.anyio
async def test_deploy_receives_build_test_and_qa_evidence(temp_db_path):
    orch = Orchestrator()

    with patch("founderscrew.config.settings.get", return_value=temp_db_path):
        orch.store.sqlite_db_path = temp_db_path
        orch.store._init_sqlite()

        issue = IssueContext(number=12, title="Fix dashboard", creator="c", repository="o/r", affected_files=["a.js"])
        state = WorkflowStateModel(
            session_id="o_r_12",
            issue=issue,
            status=WorkflowStatus.DEPLOYING,
            branch_name="founderscrew/fix-issue-12",
            plan=ImplementationPlanModel(summary="Fix dashboard", steps=[]),
            test_results=ResultsModel(
                passed=True,
                outcomes=[OutcomeModel(test_name="npm test -- dashboard", passed=True, output="stdout says dashboard renders")],
            ),
            qa_report=QAReportModel(
                passed=True,
                summary="Interactive QA clicked the dashboard and verified content.",
                screenshots=["/tmp/shot.png"],
            ),
            modified_files=["a.js", "b.js"],
            build_summaries=["Initial build: fixed dashboard loading state", "Fix pass: handled empty data"],
            test_failure_history=["Attempt 1: dashboard showed Loading forever"],
            acceptance_criteria=["Dashboard renders real content"],
            quality_gates=[
                QualityGateResult(name="targeted_tests", command="npm test -- dashboard", passed=True),
                QualityGateResult(name="lint", command="npm run lint", passed=True),
                QualityGateResult(name="typecheck", command="npm run typecheck", passed=True),
                QualityGateResult(name="docs", passed=True),
                QualityGateResult(name="artifact_hygiene", passed=True),
                QualityGateResult(name="visual_qa", command="interactive Playwright QA", passed=True),
            ],
            final_evidence_summary="Quality status: Ready for PR\n\nQuality gates:\n- targeted_tests: PASS",
        )
        orch.store.save_state(state)

        with patch("founderscrew.orchestrator.set_active_workspace_branch"), \
             patch("founderscrew.orchestrator.github_add_comment"), \
             patch("founderscrew.orchestrator.add_repo_lesson"), \
             patch.object(orch, "_run_agent", new_callable=AsyncMock) as mock_run_agent:

            mock_run_agent.return_value = '{"success": true, "pr_url": "https://github.com/o/r/pull/1", "merged": false}'

            await orch._run_deploy_step(state)

            deploy_input = mock_run_agent.await_args.args[2]
            assert deploy_input["files_changed"] == ["a.js", "b.js"]
            assert "Dashboard renders real content" in deploy_input["acceptance_criteria"]
            assert "fixed dashboard loading state" in deploy_input["build_evidence"]
            assert "stdout says dashboard renders" in deploy_input["test_evidence"]
            assert "dashboard showed Loading forever" in deploy_input["test_evidence"]
            assert "Interactive QA clicked the dashboard" in deploy_input["qa_evidence"]
            assert "Ready for PR" in deploy_input["quality_evidence"]
            assert "did not auto-merge" in deploy_input["deployment_notes"]

@pytest.mark.anyio
async def test_deploy_blocks_when_required_quality_gates_missing(temp_db_path):
    orch = Orchestrator()

    with patch("founderscrew.config.settings.get", return_value=temp_db_path):
        orch.store.sqlite_db_path = temp_db_path
        orch.store._init_sqlite()

        issue = IssueContext(number=13, title="Fix dashboard", creator="c", repository="o/r")
        state = WorkflowStateModel(
            session_id="o_r_13",
            issue=issue,
            status=WorkflowStatus.DEPLOYING,
            branch_name="founderscrew/fix-issue-13",
            plan=ImplementationPlanModel(summary="Fix dashboard", steps=[]),
            ui_qa_required=True,
            quality_gates=[QualityGateResult(name="targeted_tests", passed=True)],
        )
        orch.store.save_state(state)

        with patch("founderscrew.orchestrator.set_active_workspace_branch"), \
             patch("founderscrew.orchestrator.github_add_comment") as mock_comment, \
             patch.object(orch, "_run_agent", new_callable=AsyncMock) as mock_run_agent:

            await orch._run_deploy_step(state)

        updated = orch.store.load_state("o_r_13")
        assert updated.status == WorkflowStatus.FAILED
        assert "required quality gates" in updated.error_message
        mock_comment.assert_called_once()
        mock_run_agent.assert_not_awaited()
