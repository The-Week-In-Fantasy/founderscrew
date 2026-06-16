import os
import re
import subprocess
import logging
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional
import litellm
from founderscrew.config import settings

logger = logging.getLogger("founderscrew.coding_adapter")

_CLI_HEALTH_FAILURES: Dict[str, str] = {}

_PERSISTENT_CLI_FAILURE_PATTERNS = {
    "gemini": [
        ("SERVICE_DISABLED", "Gemini CLI Code Assist API is disabled"),
        ("cloudaicompanion.googleapis.com", "Gemini CLI Code Assist API is disabled"),
        ("IDEClient] Directory mismatch", "Gemini CLI IDE workspace mismatch"),
    ],
}


class CodingToolAdapter:
    """Dispatches coding instructions to either local CLI tools or LLM APIs using a 3-tier fallback system."""
    
    def __init__(self):
        self.mode = settings.get("coding_tools.mode", "cli").lower()
        self.tier1 = settings.get("coding_tools.tier1", settings.get("coding_tools.preferred", "claude")).lower()
        self.tier2 = settings.get("coding_tools.tier2", settings.get("coding_tools.fallback", "cursor")).lower()
        self.tier3 = settings.get("coding_tools.tier3", "gemini").lower()

    def execute_coding_task(self, instruction: str, files: List[str], workdir: str) -> Dict[str, Any]:
        """Executes a coding instruction on the specified files in the working directory using a 3-tier fallback system.
        
        Args:
            instruction: The natural language instruction describing what to code/fix.
            files: List of file paths relative to workdir that are relevant/affected.
            workdir: The directory containing the code repository.
        """
        # If mode is api, we just use API directly
        if self.mode == "api":
            return self._execute_api(instruction, files, workdir)
            
        # Collect non-empty CLI tools
        cli_tiers = [t for t in [self.tier1, self.tier2, self.tier3] if t]
        
        last_error = None
        for i, tool in enumerate(cli_tiers):
            unavailable_reason = self._cli_unavailable_reason(tool)
            if unavailable_reason:
                logger.info(f"Skipping coding CLI tool {tool} (Tier {i+1}): {unavailable_reason}")
                last_error = unavailable_reason
                continue
            try:
                logger.info(f"Attempting coding task using {tool} CLI (Tier {i+1}/{len(cli_tiers)})...")
                return self._execute_cli(tool, instruction, files, workdir)
            except Exception as e:
                error_text = str(e)
                persistent_reason = self._persistent_cli_failure_reason(tool, error_text)
                if persistent_reason:
                    _CLI_HEALTH_FAILURES[tool] = persistent_reason
                    logger.warning(
                        f"Coding CLI tool {tool} disabled for this process: {persistent_reason}. "
                        "Future attempts will skip this tier until the process restarts."
                    )
                    last_error = persistent_reason
                else:
                    logger.info(f"Coding CLI tool {tool} (Tier {i+1}) failed: {e}")
                    last_error = error_text
                
        # If all CLI tools fail, fall back to API mode automatically
        logger.warning("All coding CLI tiers failed. Falling back to direct API mode execution...")
        try:
            return self._execute_api(instruction, files, workdir)
        except Exception as api_err:
            raise RuntimeError(f"All coding tools and API fallbacks failed. Last CLI error: {last_error}. API error: {api_err}")

    def _cli_unavailable_reason(self, tool: str) -> Optional[str]:
        if tool in _CLI_HEALTH_FAILURES:
            return _CLI_HEALTH_FAILURES[tool]
        executable = {
            "claude": "claude",
            "cursor": "cursor",
            "gemini": "gemini",
            "codex": "codex",
        }.get(tool)
        if not executable:
            return f"Unknown CLI tool: {tool}"
        if not shutil.which(executable):
            return f"{executable} executable was not found on PATH"
        return None

    def _persistent_cli_failure_reason(self, tool: str, error_text: str) -> Optional[str]:
        for marker, reason in _PERSISTENT_CLI_FAILURE_PATTERNS.get(tool, []):
            if marker in (error_text or ""):
                return reason
        return None

    def _execute_cli(self, tool: str, instruction: str, files: List[str], workdir: str) -> Dict[str, Any]:
        """Runs the task using local CLI tools."""
        files_str = " ".join(files)
        
        env = os.environ.copy()
        if tool == "claude":
            # Claude Code CLI: runs 'claude' command
            cmd = ["claude", "-p", f"{instruction} for files: {files_str}"]
            cmd.extend(["--dangerously-skip-permissions"])
        elif tool == "cursor":
            # Cursor CLI: cursor --exec "instruction"
            cmd = ["cursor", "--exec", f"{instruction} on {files_str}"]
        elif tool == "gemini":
            # Gemini CLI: headless prompt mode
            cmd = ["gemini", "--prompt", f"{instruction} on {files_str}", "--skip-trust", "--approval-mode", "yolo"]
            env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        elif tool == "codex":
            # Codex CLI: codex exec
            cmd = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", f"{instruction} on {files_str}"]
        else:
            raise ValueError(f"Unknown CLI tool: {tool}")

        logger.info(f"Executing CLI command: {' '.join(cmd)} in {workdir}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=workdir,
            env=env,
            shell=True, # Needed on Windows for global npm scripts like claude
            timeout=180 # Prevent interactive CLI tools from hanging forever
        )
        
        if result.returncode != 0:
            raise RuntimeError(f"CLI command failed with exit code {result.returncode}. Error: {result.stderr}")
            
        return {
            "success": True,
            "tool": tool,
            "mode": "cli",
            "output": result.stdout,
            "error": result.stderr,
            "modified_files": files # CLI tools modify files directly in-place
        }

    def _execute_api(self, instruction: str, files: List[str], workdir: str) -> Dict[str, Any]:
        """Runs the task using direct API calls (via LiteLLM) to edit files, trying planning tiers if needed."""
        if self._looks_like_shell_remediation(instruction):
            raise RuntimeError(
                "Refusing to send shell-command remediation through API file-edit fallback. "
                "Use an orchestrator-owned safe tool for git/index or shell operations."
            )

        # Resolve planning model tiers
        tier1 = settings.get("agents.planning_tier1", settings.get("agents.planning_model", "gemini/gemini-3.5-flash"))
        tier2 = settings.get("agents.planning_tier2", None)
        tier3 = settings.get("agents.planning_tier3", None)

        api_tiers = [t for t in [tier1, tier2, tier3] if t]
        if not api_tiers:
            api_tiers = ["gemini/gemini-3.5-flash"]
            
        from founderscrew.tools.model_routing import filter_available_tiers
        valid_api_tiers = filter_available_tiers(api_tiers)

        if not valid_api_tiers:
            raise RuntimeError("All configured API coding tools were skipped because their required API keys are missing.")
            
        last_error = None
        for i, model in enumerate(valid_api_tiers):
            # Ensure gemini models are correctly routed to Google AI Studio instead of Vertex AI in LiteLLM
            if model.startswith("gemini-") and "/" not in model:
                model = f"gemini/{model}"
            logger.info(f"Executing coding task in API mode using model {model} (Tier {i+1}/{len(api_tiers)})...")
            try:
                # Read the files content
                file_contents = {}
                for f in files:
                    file_path = Path(workdir) / f
                    if file_path.exists():
                        try:
                            with open(file_path, "r", encoding="utf-8") as file_obj:
                                file_contents[f] = file_obj.read()
                        except Exception as e:
                            logger.warning(f"Warning: Could not read file {f}: {e}")
                            file_contents[f] = "[Error reading file]"
                    else:
                        file_contents[f] = "[File does not exist yet]"

                # Build prompt
                files_context = ""
                for f, content in file_contents.items():
                    files_context += f"\n--- FILE: {f} ---\n{content}\n"

                prompt = f"""You are a professional software engineer. Your task is to modify the files below based on the instruction.

INSTRUCTION:
{instruction}

FILES CONTEXT:
{files_context}

Please output your file changes using this exact markdown block format for each file:

FILE: path/to/file.py
```python
[complete updated contents of the file]
```

Do not output diffs. Output the full complete file content for each file you edit. You must specify the FILE path prefix above each code block.
"""

                messages = [
                    {"role": "system", "content": "You are a senior software developer writing clean, correct code changes."},
                    {"role": "user", "content": prompt}
                ]

                # Call the LLM (credentials resolved via environment variables)
                from founderscrew.tools.model_routing import apply_provider_env
                apply_provider_env()

                completion_kwargs = {
                    "model": model,
                    "messages": messages,
                }
                if not self._is_gemini_3_plus(model):
                    completion_kwargs["temperature"] = 0.1

                response = litellm.completion(**completion_kwargs)
                
                content_response = response.choices[0].message.content
                
                # Parse output and write files
                pattern = r"FILE:\s*([^\n]+)\s*\n```[a-zA-Z0-9_-]*\n(.*?)\n```"
                matches = re.findall(pattern, content_response, re.DOTALL)
                
                modified_files = []
                for file_path_str, code_content in matches:
                    file_path_str = file_path_str.strip()
                    # Remove backticks if present
                    if file_path_str.startswith("`") and file_path_str.endswith("`"):
                        file_path_str = file_path_str[1:-1]
                        
                    full_path = Path(workdir) / file_path_str
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(full_path, "w", encoding="utf-8") as f_out:
                        f_out.write(code_content)
                    modified_files.append(file_path_str)
                    
                if not modified_files:
                    # Fall back to search for colon format: ```lang:filepath
                    pattern_colon = r"```[a-zA-Z0-9_-]*:([^\n]+)\n(.*?)\n```"
                    matches_colon = re.findall(pattern_colon, content_response, re.DOTALL)
                    for file_path_str, code_content in matches_colon:
                        file_path_str = file_path_str.strip()
                        if file_path_str.startswith("`") and file_path_str.endswith("`"):
                            file_path_str = file_path_str[1:-1]
                        full_path = Path(workdir) / file_path_str
                        full_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(full_path, "w", encoding="utf-8") as f_out:
                            f_out.write(code_content)
                        modified_files.append(file_path_str)

                if len(modified_files) > 0:
                    return {
                        "success": True,
                        "tool": model,
                        "mode": "api",
                        "output": content_response,
                        "modified_files": modified_files
                    }
                else:
                    excerpt = (content_response or "").strip().replace("\n", " ")[:500]
                    raise RuntimeError(f"No modified files parsed from API response. Response excerpt: {excerpt}")
            except Exception as e:
                logger.error(f"API Mode execution with model {model} failed: {e}")
                last_error = str(e)
                
        raise RuntimeError(f"API mode failed all planning model tiers. Last error: {last_error}")

    def _looks_like_shell_remediation(self, instruction: str) -> bool:
        text = (instruction or "").strip().lower()
        if not text.startswith(("run ", "execute ")):
            return False
        command_patterns = [
            r"\bgit\s+(?:rm|add|checkout|reset|clean|commit|push|pull|fetch)\b",
            r"\brm\s+-",
            r"\bdel\s+",
            r"\bremove-item\b",
            r"\bnpm\s+",
            r"\bpytest\b",
            r"\bpython\s+-m\b",
        ]
        return any(re.search(pattern, text) for pattern in command_patterns)

    def _is_gemini_3_plus(self, model: str) -> bool:
        model_name = (model or "").lower().split("/")[-1]
        return model_name.startswith("gemini-3")
