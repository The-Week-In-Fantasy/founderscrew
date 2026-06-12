from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from founderscrew.tools import (
    github_create_branch,
    github_commit_files,
    github_create_pr,
    github_add_comment,
    github_merge_pr,
    github_push_workspace
)
from founderscrew.config import settings

def get_deployer_agent() -> LlmAgent:
    """Returns the deployer agent instance."""
    return LlmAgent(
        name="DeployerAgent",
        description="Autonomous deployment agent that opens pull requests, commits code, and merges PRs.",
        model=settings.get("agents.fast_model", "gemini-3.1-flash-lite"),
        instruction="""You are a Release Engineer.
Your job is to manage the release flow: create branches, commit final code, create pull requests, post status comments, and merge PRs.

To perform this job:
1. Push the local workspace code changes using github_push_workspace with the provided branch_name. This commits the Builder's local edits and pushes them to GitHub (the branch is created automatically).
2. Open a Pull Request using github_create_pr with a clear summary of changes, planning steps, test results, and QA observations.
3. Add status comments to the issue using github_add_comment.
4. Merge the PR using github_merge_pr if approvals are signed off.
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
            FunctionTool(github_merge_pr)
        ],
        output_key="deploy_result"
    )
