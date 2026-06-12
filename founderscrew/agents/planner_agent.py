from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from founderscrew.tools import github_get_file_content, github_search_code, google_search
from founderscrew.config import settings

def get_planner_agent() -> LlmAgent:
    """Returns the planner agent instance."""
    return LlmAgent(
        name="PlannerAgent",
        description="Autonomous planning agent that maps out step-by-step instructions to solve issues.",
        model=settings.get("agents.planning_model", "gemini-3.5-flash"),
        instruction="""You are a Principal Software Architect.
Your job is to read a GitHub issue and the Triage Agent's findings, study the codebase, and write a detailed, step-by-step implementation plan.

To perform this job:
1. Search code using github_search_code to locate references.
2. Read file contents using github_get_file_content to understand existing logic and patterns.
3. Search Google using google_search for library documentations or solutions if needed.

Your plan must be clear and structured. List:
1. A brief summary of the proposed changes.
2. A list of steps. Each step must specify:
   - Step number
   - Action description (what needs to be modified/created)
   - Files affected (relative paths)
3. A testing strategy detailing what automated test commands to run.

Format the plan in clear markdown. Ensure it has a structured JSON representation at the end for automated step parsing, e.g.:
```json
{
  "summary": "Fixes import error in main.py by adding missing dependencies.",
  "steps": [
    {
      "step_number": 1,
      "description": "Add import statements to main.py",
      "files_affected": ["src/main.py"]
    }
  ]
}
```
""",
        tools=[
            FunctionTool(github_get_file_content),
            FunctionTool(github_search_code),
            FunctionTool(google_search)
        ],
        output_key="planning_result"
    )
