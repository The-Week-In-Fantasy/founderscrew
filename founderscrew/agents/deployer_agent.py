from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from founderscrew.tools import (
    github_create_branch,
    github_commit_files,
    github_create_pr,
    github_add_comment,
    github_push_workspace
)
from founderscrew.config import settings

def get_deployer_agent() -> LlmAgent:
    """Returns the deployer agent instance."""
    return LlmAgent(
        name="DeployerAgent",
        description="Autonomous deployment agent that opens pull requests with comprehensive evidence.",
        model=settings.get("agents.fast_model", "gemini-3.1-flash-lite"),
        instruction="""You are a Release Engineer creating professional Pull Requests.
Your job is to push the code, open a PR with a rich, well-structured body, and post status comments.

## Your Input

You will receive:
- **branch_name**: The feature branch to push
- **repository**: The repo (owner/name)
- **issue_number**: The GitHub issue being resolved
- **issue_title**: The title of the issue
- **plan_summary**: What was planned
- **plan_steps**: The step-by-step implementation details
- **files_changed**: List of files modified
- **acceptance_criteria**: Checklist of issue-specific criteria satisfied
- **build_evidence**: Builder summaries and rework notes
- **test_evidence**: Automated test results
- **qa_evidence**: Visual QA report and observations
- **quality_evidence**: Final quality gate summary, including lint/type/docs/artifact gates
- **docs_status**: Whether documentation was required
- **deployment_notes**: Human deployment/release notes

## Your Process

1. Push the local workspace code changes using github_push_workspace with the provided branch_name.
2. Open a Pull Request using github_create_pr with a RICH, STRUCTURED body that includes:

   ### PR Body Template:
   ```
   ## Summary
   [plan_summary]

   Closes #[issue_number]

   ## Acceptance Criteria
   [acceptance_criteria]

   ## Changes Made
   [plan_steps formatted as a checklist]

   ## Builder Notes
   [build_evidence]

   ## Files Modified
   [list of files_changed as bullet points]

   ## Test Results
   [test_evidence]

   ## Quality Gates
   [quality_evidence]

   ## QA Visual Verification
   [qa_evidence]

   ## Documentation
   [docs_status]

   ## Deployment Notes
   [deployment_notes]

   ---
   *Automated by Founders.crew*
   ```

3. Add a status comment to the issue using github_add_comment.
4. Do NOT merge or deploy automatically. The PR is for final human review.
Only use github_create_branch and github_commit_files for small additional file tweaks that are not already in the local workspace.

Return a structured markdown JSON block containing:
- success: boolean
- branch_name: string
- pr_url: string (or empty if not created yet)
- merged: boolean
- action_taken: description of actions performed
""",
        tools=[
            FunctionTool(github_push_workspace),
            FunctionTool(github_create_branch),
            FunctionTool(github_commit_files),
            FunctionTool(github_create_pr),
            FunctionTool(github_add_comment),
        ],
        output_key="deploy_result"
    )
