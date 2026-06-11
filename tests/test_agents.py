import pytest
from founderscrew.agents import (
    get_triage_agent,
    get_planner_agent,
    get_builder_agent,
    get_tester_agent,
    get_reviewer_agent,
    get_qa_agent,
    get_deployer_agent
)

def test_triage_agent_init():
    agent = get_triage_agent()
    assert agent.name == "TriageAgent"
    assert len(agent.tools) > 0
    assert agent.output_key == "triage_result"

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
