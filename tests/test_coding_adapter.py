import os
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
from founderscrew.tools.coding_adapter import CodingToolAdapter

def test_coding_adapter_cli_claude(tmp_path):
    """Verifies that CLI mode for Claude executes the expected subprocess command."""
    adapter = CodingToolAdapter()
    adapter.mode = "cli"
    adapter.tier1 = "claude"
    adapter.tier2 = ""
    adapter.tier3 = ""
    
    with patch("subprocess.run") as mock_run:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Task complete"
        mock_result.stderr = ""
        mock_run.return_value = mock_result
        
        res = adapter.execute_coding_task(
            instruction="fix index.js import error",
            files=["index.js"],
            workdir=str(tmp_path)
        )
        
        assert res["success"] is True
        assert res["mode"] == "cli"
        assert res["tool"] == "claude"
        mock_run.assert_called_once()
        # Verify it passed command lists to subprocess
        args = mock_run.call_args[0][0]
        assert "claude" in args
        assert "-p" in args

def test_coding_adapter_api_mode(tmp_path):
    """Verifies that API mode parses the model output and updates files correctly."""
    adapter = CodingToolAdapter()
    adapter.mode = "api"
    adapter.api_model = "gemini/gemini-2.5-pro"
    
    # Create a dummy file in temp path
    dummy_file = tmp_path / "hello.py"
    dummy_file.write_text("print('hello')\n", encoding="utf-8")
    
    # Mock Response from litellm
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_message = MagicMock()
    
    # Simulate LLM response containing file updates in requested format
    mock_message.content = """
Here are the changes:

FILE: hello.py
```python
print('hello world!')
```

FILE: new_module.py
```python
def add(a, b):
    return a + b
```
"""
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]
    
    with patch("litellm.completion", return_value=mock_response) as mock_complete:
        res = adapter.execute_coding_task(
            instruction="say hello world and add add function",
            files=["hello.py", "new_module.py"],
            workdir=str(tmp_path)
        )
        
        assert res["success"] is True
        assert res["mode"] == "api"
        assert "hello.py" in res["modified_files"]
        assert "new_module.py" in res["modified_files"]
        
        # Verify files were actually written/updated
        updated_hello = (tmp_path / "hello.py").read_text(encoding="utf-8")
        assert updated_hello == "print('hello world!')"
        
        new_module = (tmp_path / "new_module.py").read_text(encoding="utf-8")
        assert new_module == "def add(a, b):\n    return a + b"
        
        mock_complete.assert_called_once()
