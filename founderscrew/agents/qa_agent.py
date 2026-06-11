from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from founderscrew.tools import capture_screenshot, compare_screenshots
from founderscrew.config import settings

def get_qa_agent() -> LlmAgent:
    """Returns the QA agent instance."""
    return LlmAgent(
        name="QAAgent",
        description="Autonomous QA agent that validates visual layouts and compares screenshots.",
        model=settings.get("agents.fast_model", "gemini-2.5-flash"),
        instruction="""You are a visual Quality Assurance Specialist.
Your job is to compare screenshot visuals of local test pages against standard references (or between branch and master states) to confirm visual correctness.

To perform this job:
1. Capture screenshots using capture_screenshot.
2. Compare screenshots using compare_screenshots to calculate similarity percentage.

Analyze the difference score and visual layout.
Return a structured markdown JSON block containing:
- passed: boolean (true if visual verification succeeded, false if similarity is too low or errors found)
- similarity_percentage: float score
- observations: text detailing visual checks (layout, alignment, elements present)
""",
        tools=[
            FunctionTool(capture_screenshot),
            FunctionTool(compare_screenshots)
        ],
        output_key="qa_result"
    )
