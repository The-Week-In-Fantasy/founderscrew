from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from founderscrew.tools import (
    github_create_branch,
    github_commit_files,
    github_create_pr,
    github_add_comment,
    github_merge_pr
)
from founderscrew.config import settings

def get_deployer_agent() -> LlmAgent:
    """Returns the deployer agent instance."""
    return LlmAgent(
        name="DeployerAgent",
        description="Autonomous deployment agent that opens pull requests, commits code, and merges PRs.",
        model=settings.get("agents.fast_model", "gemini-2.5-flash"),
        instruction="""You are a Release Engineer.
Your job is to manage the release flow: create branches, commit final code, create pull requests, post status comments, and merge PRs.

To perform this job:
1. Create a branch using github_create_branch if a branch name is provided.
2. Commit files to that branch using github_commit_files.
3. Open a Pull Request using github_create_pr with a clear summary of changes, planning steps, test results, and QA observations.
4. Add status comments to the issue using github_add_comment.
5. Merge the PR using github_merge_pr if approvals are signed off.

Return a structured markdown JSON block containing:
- success: boolean
- branch_name: string
- pr_url: string (or empty if not created yet)
- merged: boolean
- action_taken: description of actions performed
""",
        tools=[
            FunctionTool(github_create_branch),
            FunctionTool(github_commit_files),
            FunctionTool(github_create_pr),
            FunctionTool(github_add_comment),
            FunctionTool(github_merge_pr)
        ],
        output_key="deploy_result"
    )
