from founderscrew.agents import (
    get_triage_agent,
    get_planner_agent,
    get_builder_agent,
    get_tester_agent,
    get_reviewer_agent,
    get_qa_agent,
    get_deployer_agent
)
from founderscrew.agents import tester_agent

def test_triage_agent_init():
    agent = get_triage_agent()
    assert agent.name == "TriageAgent"
    assert len(agent.tools) > 0
    assert agent.output_key == "triage_result"
    tool_names = {getattr(tool, "name", "") for tool in agent.tools}
    assert "github_list_repo_files" not in tool_names
    assert "github_get_issue" not in tool_names

def test_planner_agent_init():
    agent = get_planner_agent()
    assert agent.name == "PlannerAgent"
    assert len(agent.tools) > 0
    assert agent.output_key == "planning_result"

def test_builder_agent_init():
    agent = get_builder_agent()
    assert agent.name == "BuilderAgent"
    assert len(agent.tools) == 1
    assert agent.output_key == "build_result"

def test_tester_agent_init():
    agent = get_tester_agent()
    assert agent.name == "TesterAgent"
    assert len(agent.tools) > 0
    assert agent.output_key == "test_result"

def test_run_test_command_uses_hermetic_playwright_env(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text('{"scripts":{"test":"playwright test"}}')
    (tmp_path / "node_modules").mkdir()
    calls = []

    def fake_run_safe_shell_command(command, cwd, timeout=60, extra_env=None):
        calls.append((command, cwd, timeout, extra_env))
        return {"success": True, "stdout": "ok", "stderr": "", "returncode": 0}

    monkeypatch.setattr(tester_agent, "github_clone_or_pull", lambda repository: str(tmp_path))
    monkeypatch.setattr(tester_agent, "run_safe_shell_command", fake_run_safe_shell_command)
    monkeypatch.setattr(tester_agent.settings, "get", lambda key, default=None: default)

    result = tester_agent.run_test_command("npm test", "owner/repo")

    assert result["success"] is True
    assert calls == [
        (
            "npx playwright install chromium",
            str(tmp_path),
            300,
            {
                "PLAYWRIGHT_BROWSERS_PATH": "0",
            },
        ),
        (
            "npm test",
            str(tmp_path),
            600,
            {
                "CI": "1",
                "PORT": "3001",
                "PLAYWRIGHT_BASE_URL": "http://localhost:3001",
                "PLAYWRIGHT_BROWSERS_PATH": "0",
            },
        )
    ]

def test_reviewer_agent_init():
    agent = get_reviewer_agent()
    assert agent.name == "ReviewerAgent"
    assert len(agent.tools) > 0
    assert agent.output_key == "review_result"

def test_qa_agent_init():
    agent = get_qa_agent()
    assert agent.name == "QAAgent"
    assert len(agent.tools) > 0
    assert agent.output_key == "qa_result"

def test_deployer_agent_init():
    agent = get_deployer_agent()
    assert agent.name == "DeployerAgent"
    assert len(agent.tools) > 0
    assert agent.output_key == "deploy_result"
