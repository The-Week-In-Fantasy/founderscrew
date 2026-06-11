import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path
from founderscrew.state.models import WorkflowStatus, WorkflowStateModel, IssueContext
from founderscrew.orchestrator import Orchestrator

@pytest.fixture
def temp_db_path(tmp_path):
    return tmp_path / "test_orchestrator.db"

@pytest.mark.anyio
async def test_handle_issue_labeled_success(temp_db_path):
    """Verifies that an incoming issue labeled event runs triage and planning successfully."""
    orch = Orchestrator()
    
    # Mock settings and store paths
    with patch("founderscrew.config.settings.get", return_value=temp_db_path):
        orch.store._init_sqlite()  # Reinit SQLite db in temp folder
        
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
                mock_clone.assert_called_once_with("owner/repo")

@pytest.mark.anyio
async def test_handle_comment_created_approval(temp_db_path):
    """Verifies that an approval comment transitions state and triggers building flow."""
    orch = Orchestrator()
    
    with patch("founderscrew.config.settings.get", return_value=temp_db_path):
        orch.store._init_sqlite()
        
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
        
        # Mock github and build-test runner
        with patch("founderscrew.orchestrator.github_add_comment") as mock_comment, \
             patch.object(orch, "run_build_test_review_flow", new_callable=AsyncMock) as mock_flow:
                 
            await orch.handle_comment_created("o/r", 100, "Approve this plan please", "founder")
            
            # Verify status transitioned to BUILDING
            updated_state = orch.store.load_state("o_r_100")
            assert updated_state.status == WorkflowStatus.BUILDING
            assert updated_state.plan.approved is True
            mock_comment.assert_called_once()
            mock_flow.assert_called_once()


@pytest.mark.anyio
async def test_resume_failed_workflow(temp_db_path):
    """Verifies that resume_failed_workflow picks up at the correct step based on error message."""
    orch = Orchestrator()
    
    with patch("founderscrew.config.settings.get", return_value=temp_db_path):
        orch.store._init_sqlite()
        
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
            mock_triage.assert_called_once()

        # Test case 2: Testing failed resumes testing
        state.status = WorkflowStatus.FAILED
        state.error_message = "Testing failed: run failed"
        orch.store.save_state(state)
        
        with patch("founderscrew.orchestrator.github_add_comment") as mock_comment, \
             patch.object(orch, "run_build_test_review_flow", new_callable=AsyncMock) as mock_flow:
            await orch.resume_failed_workflow("o_r_100")
            assert orch.store.load_state("o_r_100").status == WorkflowStatus.TESTING
            mock_comment.assert_called_once()
            mock_flow.assert_called_once()
            assert mock_flow.call_args[0][0].status == WorkflowStatus.TESTING
            assert mock_flow.call_args[1].get("start_at") == "testing"

