from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from founderscrew.tools import github_get_issue, github_list_repo_files, github_get_file_content, google_search
from founderscrew.config import settings

def get_triage_agent() -> LlmAgent:
    """Returns the triage agent instance."""
    return LlmAgent(
        name="TriageAgent",
        description="Autonomous triage agent that categorizes issues and gathers repository context.",
        model=settings.get("agents.fast_model", "gemini-3.1-flash-lite"),
        instruction="""You are a senior DevOps triage engineer.
Your job is to read a GitHub issue, classify it, identify affected files, and estimate complexity.

To perform this job:
1. Fetch the issue details using github_get_issue.
2. Scan the repository files using github_list_repo_files to find related source files.
3. Review relevant file contents if necessary using github_get_file_content.
4. If the issue mentions complex terminology, libraries, or dependencies, search Google using google_search.

Return a structured markdown JSON block containing:
- classification: 'bug', 'feature', or 'enhancement'
- affected_files: list of relative file paths in the repo
- complexity: 'low', 'medium', or 'high'
- reason: brief explanation for your choice

Example output:
{
  "classification": "bug",
  "affected_files": ["src/main.py"],
  "complexity": "low",
  "reason": "Fixes import error in main.py"
}
""",
        tools=[
            FunctionTool(github_get_issue),
            FunctionTool(github_list_repo_files),
            FunctionTool(github_get_file_content),
            FunctionTool(google_search)
        ],
        output_key="triage_result"
    )
