import asyncio
from types import SimpleNamespace

import pytest

from agenten.Captain import CaptainAgent
from agenten.llm.model_client import build_replay_model_client
from agenten.workflows.base import AgentRoleSpec, NestedChatWorkflow, WorkflowStep


class FakeAgent:
    def __init__(self, name: str):
        self.name = name
        self.tasks: list[str] = []

    async def run(self, *, task: str):
        self.tasks.append(task)
        return SimpleNamespace(messages=[SimpleNamespace(content=f"{self.name}: {task}")])


class FakeCaptain:
    def __init__(self):
        self.agents: dict[str, FakeAgent] = {}

    def create_agent_assistant(self, name: str, system_message: str):
        agent = FakeAgent(name)
        self.agents[name] = agent
        return agent

    def create_agent_user_proxy(self, name: str, system_message: str):
        return self.create_agent_assistant(name, system_message)


def build_workflow() -> NestedChatWorkflow:
    return NestedChatWorkflow(
        name="test",
        roles=[AgentRoleSpec("generator"), AgentRoleSpec("critic"), AgentRoleSpec("user_proxy", kind="user_proxy")],
        steps=[
            WorkflowStep(recipient_role="critic", message=lambda *, history, context: f"Critique {history[-1]} for {context['topic']}"),
            WorkflowStep(recipient_role="generator", message="Refine using the previous output."),
        ],
        entry_role="generator",
        trigger_role="user_proxy",
        kickoff_message="Start {topic}.",
    )


def test_run_async_passes_history_and_context_between_agents():
    captain = FakeCaptain()

    result = asyncio.run(build_workflow().run_async(captain, {"topic": "migration"}))

    assert result.startswith("test_generator:")
    critic = captain.agents["test_critic"]
    assert "Critique test_generator: Start migration." in critic.tasks[0]
    generator = captain.agents["test_generator"]
    assert "previous output" in generator.tasks[-1]


def test_sync_run_is_rejected_inside_running_event_loop():
    async def invoke():
        with pytest.raises(RuntimeError, match="run_async"):
            build_workflow().run(FakeCaptain(), {"topic": "migration"})

    asyncio.run(invoke())


def test_captain_runs_current_agentchat_with_replay_client(tmp_path):
    captain = CaptainAgent(
        name="test",
        llm_config={},
        blockchain_path=str(tmp_path / "blockchain.json"),
        model_client=build_replay_model_client(["kickoff", "draft", "critique", "refined", "final"]),
    )

    result = captain.run_workflow(
        "project_definition",
        {"project_description": "Build a test system"},
    )

    assert result == "final"
