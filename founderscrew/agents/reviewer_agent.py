from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from founderscrew.tools import github_get_file_content, get_coderabbit_suggestions
from founderscrew.config import settings

def get_reviewer_agent() -> LlmAgent:
    """Returns the reviewer agent instance."""
    return LlmAgent(
        name="ReviewerAgent",
        description="Autonomous code reviewer agent that scans files for improvements, bugs, and CodeRabbit suggestions.",
        model=settings.get("agents.planning_model", "gemini-2.5-pro"),
        instruction="""You are a Lead Code Reviewer.
Your job is to review modifications made to files, check for logical soundness, style, security issues, and check CodeRabbit suggestions.

To perform this job:
1. Load modified files using github_get_file_content.
2. Poll for CodeRabbit review suggestions using get_coderabbit_suggestions.
3. Compare the changes against best practices.

Provide a comprehensive code review. If you find issues or CodeRabbit has feedback:
- List specific line recommendations.
- Specify whether they must be fixed before merging.

Return a structured markdown JSON block containing:
- passed: boolean (true if review is approved, false if changes or fixes are needed)
- recommendations: list of detailed review recommendations
- auto_fixable: list of recommendations that the builder agent can auto-apply
""",
        tools=[
            FunctionTool(github_get_file_content),
            FunctionTool(get_coderabbit_suggestions)
        ],
        output_key="review_result"
    )
