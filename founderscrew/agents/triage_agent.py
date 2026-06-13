from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from founderscrew.tools import google_search
from founderscrew.config import settings

def get_triage_agent() -> LlmAgent:
    """Returns the triage agent instance."""
    return LlmAgent(
        name="TriageAgent",
        description="Autonomous triage agent that categorizes issues and gathers repository context.",
        model=settings.get("agents.fast_model", "gemini-3.1-flash-lite"),
        instruction="""You are a senior DevOps triage engineer.
Your job is to read a GitHub issue, classify it, identify affected files, and estimate complexity.

Your input already includes the issue title/body, repository name, cached repo profile, and a local repository file list gathered by the orchestrator. Do NOT fetch the GitHub issue again.

To perform this job:
1. Read the issue details provided in the input.
2. Use the provided repo_context and repo_files to identify likely affected files.
3. If the issue mentions complex terminology, libraries, or dependencies, search Google using google_search.
4. If the issue is too broad, risky, or not appropriate for autonomous bug/minor enhancement work, classify it as not_safe_for_autonomy.

Return a structured markdown JSON block containing:
- classification: 'bug', 'minor_enhancement', or 'not_safe_for_autonomy'
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
            FunctionTool(google_search)
        ],
        output_key="triage_result"
    )
