import os
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path
from founderscrew.tools.coding_adapter import CodingToolAdapter
from founderscrew.orchestrator import Orchestrator
from founderscrew.config import settings

class MockPart:
    def __init__(self, text=None):
        self.text = text
        self.function_call = None
        self.function_response = None

class MockContent:
    def __init__(self, parts=None):
        self.parts = parts or []

class MockEvent:
    def __init__(self, text=None, error_code=None, error_message=None, author="model"):
        self.content = MockContent([MockPart(text=text)]) if text else None
        self.partial = False
        self.author = author
        self.error_code = error_code
        self.error_message = error_message
        self.output = None

def make_mock_settings_get(temp_db_path):
    # Retrieve configuration correctly, only override db path
    def mock_get(key, default=None):
        if key == "state.db_path":
            return temp_db_path
        # Get from settings.config dictionary structure
        parts = key.split(".")
        val = settings.config
        for part in parts:
            if isinstance(val, dict) and part in val:
                val = val[part]
            else:
                return default
        return val
    return mock_get

@pytest.fixture
def temp_db_path(tmp_path):
    return tmp_path / "test_fallback.db"

@pytest.mark.anyio
async def test_orchestrator_run_agent_fallback(temp_db_path):
    """Verifies that _run_agent tries the next model tier if a tier fails."""
    orch = Orchestrator()
    
    # Selective settings.get patch to avoid Pydantic validation errors on model key paths
    with patch.object(settings, "get", side_effect=make_mock_settings_get(temp_db_path)):
        orch.store.sqlite_db_path = temp_db_path
        orch.store._init_sqlite()
        
        # Configure tiers for triage agent
        with patch.dict(settings.config, {
            "agents": {
                "fast_tier1": "gemini-2.5-flash",
                "fast_tier2": "openai/gpt-4o-mini",
                "fast_tier3": "anthropic/claude-3-haiku"
            },
            "coding_tools": {
                "openai_api_key": "fake",
                "anthropic_api_key": "fake"
            },
            "google": {
                "api_key": "fake"
            }
        }):
            # We want to mock Runner's run_async method to fail for Tier 1 but pass for Tier 2
            mock_runner_instance = MagicMock()
            
            call_count = 0
            
            async def mock_run_async_generator(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    # Tier 1 fails
                    yield MockEvent(error_code=1, error_message="Tier 1 failed due to limits")
                else:
                    # Tier 2 succeeds with text in content.parts
                    yield MockEvent(text="Triage complete output")
            
            mock_runner_instance.run_async = mock_run_async_generator
            
            with patch("founderscrew.orchestrator.Runner", return_value=mock_runner_instance):
                from founderscrew.agents.triage_agent import get_triage_agent
                output = await orch._run_agent(get_triage_agent, "test_session", "input_payload")
                
                assert output == "Triage complete output"
                assert call_count == 2  # Proves Tier 1 failed and Tier 2 was executed

def test_vertex_tier_requires_gcp_project(monkeypatch):
    """Vertex AI partner tiers are gated on a GCP project, not provider API keys."""
    from founderscrew.tools.model_routing import filter_available_tiers

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with patch.dict(settings.config, {"google": {"api_key": "fake", "project_id": ""}, "coding_tools": {}}):
        # No GCP project: vertex tier is skipped even though it names "claude"
        assert filter_available_tiers(
            ["gemini/gemini-3.5-flash", "vertex_ai/claude-sonnet-4-6"]
        ) == ["gemini/gemini-3.5-flash"]

    with patch.dict(settings.config, {"google": {"api_key": "fake", "project_id": "my-gcp-project"}, "coding_tools": {}}):
        # With a project, the vertex tier is available without an Anthropic key
        assert filter_available_tiers(
            ["vertex_ai/claude-sonnet-4-6"]
        ) == ["vertex_ai/claude-sonnet-4-6"]

def test_coding_adapter_fallback_cascade(tmp_path):
    """Verifies that CodingToolAdapter falls back sequentially through coding tool tiers and then to API mode."""
    adapter = CodingToolAdapter()
    adapter.mode = "cli"
    
    # Configure tool tiers
    with patch.dict(settings.config, {
        "coding_tools": {
            "tier1": "claude",
            "tier2": "cursor",
            "tier3": "gemini"
        }
    }):
        # Mock subprocess.run to raise exception for claude and cursor, but pass for gemini
        adapter.__init__()
        
        call_count = 0
        def mock_subprocess_run(cmd, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if "claude" in cmd_str:
                # Tier 1 CLI fails
                result = MagicMock()
                result.returncode = 1
                result.stderr = "claude not installed"
                return result
            elif "cursor" in cmd_str:
                # Tier 2 CLI fails
                result = MagicMock()
                result.returncode = 1
                result.stderr = "cursor failed"
                return result
            else:
                # Tier 3 CLI succeeds
                result = MagicMock()
                result.returncode = 0
                result.stdout = "Gemini edit successful"
                result.stderr = ""
                return result
                
        with patch("subprocess.run", side_effect=mock_subprocess_run):
            res = adapter.execute_coding_task(
                instruction="edit code",
                files=["main.py"],
                workdir=str(tmp_path)
            )
            
            assert res["success"] is True
            assert res["tool"] == "gemini"
            assert res["mode"] == "cli"

def test_coding_adapter_all_cli_fail_cascades_to_api(tmp_path):
    """Verifies that CodingToolAdapter falls back to API mode if all CLI tools fail."""
    adapter = CodingToolAdapter()
    adapter.mode = "cli"
    
    with patch.dict(settings.config, {
        "coding_tools": {
            "tier1": "claude",
            "tier2": "cursor",
            "tier3": "gemini"
        },
        "agents": {
            "planning_tier1": "gemini/gemini-2.5-pro",
            "planning_tier2": "openai/gpt-4o"
        }
    }):
        adapter.__init__()
        
        # Subprocess fails for all commands
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "fail"
        
        # Mock litellm completion
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_message = MagicMock()
        mock_message.content = "FILE: main.py\n```python\nprint('api content')\n```"
        mock_choice.message = mock_message
        mock_response.choices = [mock_choice]
        
        with patch("subprocess.run", return_value=mock_result), \
             patch("litellm.completion", return_value=mock_response) as mock_litellm:
                 
            res = adapter.execute_coding_task(
                instruction="edit code",
                files=["main.py"],
                workdir=str(tmp_path)
            )
            
            assert res["success"] is True
            assert res["mode"] == "api"
            assert res["tool"] == "gemini/gemini-2.5-pro"
            mock_litellm.assert_called_once()
            
            content = (tmp_path / "main.py").read_text(encoding="utf-8")
            assert "api content" in content
