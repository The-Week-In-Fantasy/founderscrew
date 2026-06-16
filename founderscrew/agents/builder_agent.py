from typing import List, Dict, Any
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from founderscrew.tools import CodingToolAdapter, github_clone_or_pull
from founderscrew.config import settings

def run_coding_tool(instruction: str, files: List[str], repository: str) -> Dict[str, Any]:
    """Executes code changes on files in the local repository workspace.
    
    Args:
        instruction: Clear instructions on what modifications to apply.
        files: List of file paths to modify (relative to repository root).
        repository: Repository owner/name (e.g. 'owner/repo').
    """
    try:
        workdir = github_clone_or_pull(repository)
        adapter = CodingToolAdapter()
        res = adapter.execute_coding_task(instruction, files, workdir)
        if not res.get("success"):
            raise RuntimeError(res.get("error") or "Unknown error in coding tool.")
        return res
    except Exception as e:
        raise RuntimeError(f"run_coding_tool execution failed: {e}")

def get_builder_agent() -> LlmAgent:
    """Returns the builder agent instance."""
    return LlmAgent(
        name="BuilderAgent",
        description="Autonomous coding agent that modifies codebases using specialized editors and compilers.",
        model=settings.get("agents.planning_model", "gemini-3.5-flash"),
        instruction="""You are a Senior Software Developer.
Your job is to apply code modifications to files in the repository based on an implementation plan.

To perform this job:
1. Call the run_coding_tool with step instructions and file paths only for code, test, and documentation edits.
2. Ensure you modify only the files listed in the step.
3. Write clean, modular code with comments explaining non-obvious choices.
4. Write a brief, targeted automated test that specifically verifies the issue is resolved. Save it as tests/integration/issue_[number]_test.spec.js for JavaScript/TypeScript projects, or tests/test_issue_[number].py for Python projects. Do not rely on the global regression suite.

Do not use run_coding_tool to run shell commands, git commands, cleanup commands, or artifact/index remediation such as git rm --cached. Those operations are handled by the orchestrator's safe quality-gate tooling.
Do not add, move, or render production components on unrelated routes just to make QA easier. Verify and fix the component where it is actually imported/rendered unless the issue explicitly asks for routing changes.

Return a structured JSON block enclosed in ```json ... ``` containing:
- "summary": a brief description of the code changes.
- "modified_files": a list of files you modified.
- "test_command": the specific shell command to run the targeted test you created. **IMPORTANT**: The file path in this command MUST EXACTLY match the path where you just saved the test file (e.g. 'npx playwright test tests/integration/issue_123_test.spec.js').
""",
        tools=[
            FunctionTool(run_coding_tool)
        ],
        output_key="build_result"
    )
