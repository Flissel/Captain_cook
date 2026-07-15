"""Offline contract tests for the Captain Cook householder worker fleet."""
from pathlib import Path

import pytest

from blockchain.Blockchain_modell import Blockchain
from blockchain.storage import InMemoryStorage

from agenten.household.executor import HouseholderExecutionError
from agenten.household.roles import HouseholderRoleError, load_householder_roles
from agenten.household.worker import HouseholderWorker, create_householder_worker_factories
from agenten.orchestration.pipeline import PipelineBootError, build_pipeline
from agenten.runtime.event_bus import InMemoryEventBus
from agenten.tools.base import ToolRegistry
from agenten.workers.base import WorkerExecutionError


@pytest.mark.asyncio
async def test_householder_worker_emits_a_json_safe_offline_report():
    role = load_householder_roles()[0]
    worker = HouseholderWorker(InMemoryEventBus(), ToolRegistry(), role=role)

    report = await worker.execute("subproblem-42", "Review the boundary between planner and ledger")

    assert report == {
        "role": "architect",
        "decision": "offline_review_completed",
        "artifacts": ["agents/household/architect.md"],
        "evidence": ["deterministic offline executor", "subproblem:subproblem-42"],
        "limitations": ["No LLM, MCP server, browser, or deployment was invoked."],
    }
@pytest.mark.asyncio
async def test_householder_worker_preserves_executor_failure_semantics():
    role = load_householder_roles()[0]

    class RejectingExecutor:
        async def run(self, role, subproblem_id, description):
            raise HouseholderExecutionError("prompt contract is invalid", retriable=False)

    worker = HouseholderWorker(InMemoryEventBus(), ToolRegistry(), role=role, executor=RejectingExecutor())

    with pytest.raises(WorkerExecutionError, match="prompt contract is invalid") as error:
        await worker.execute("subproblem-42", "Review a contract")

    assert error.value.retriable is False
@pytest.mark.asyncio
async def test_householder_roles_route_through_the_real_pipeline():
    async def household_decompose(description, depth):
        if depth != 0:
            return []
        return [
            {
                "description": f"Architecture review for {description}",
                "capability_tags": ["architecture_review"],
                "atomic": True,
            },
            {
                "description": f"Ledger review for {description}",
                "capability_tags": ["ledger_review"],
                "atomic": True,
            },
            {
                "description": f"Delivery plan for {description}",
                "capability_tags": ["delivery_plan"],
                "atomic": True,
            },
            {
                "description": f"Quality review for {description}",
                "capability_tags": ["quality_review"],
                "atomic": True,
            },
        ]

    pipeline = build_pipeline(
        blockchain=Blockchain(storage=InMemoryStorage()),
        llm_decompose=household_decompose,
        worker_factories=create_householder_worker_factories(),
    )

    await pipeline.start()
    await pipeline.submit_problem("Make the Devpost project trustworthy")
    assert await pipeline.wait_until_terminal(expected_subproblem_count=4, timeout=5.0)
    await pipeline.stop()

    done_blocks = pipeline.ledger_query.blocks_in_stage("done")
    assert {block.data["agent_type"] for block in done_blocks} == {
        "householder_architect",
        "householder_ledger_steward",
        "householder_delivery_builder",
        "householder_quality_warden",
    }
    assert {block.data["result"]["role"] for block in done_blocks} == {
        "architect",
        "ledger-steward",
        "delivery-builder",
        "quality-warden",
    }
@pytest.mark.asyncio
async def test_pipeline_rejects_a_householder_capability_that_would_shadow_echo():
    class ShadowEchoWorker(HouseholderWorker):
        capability_tags = ["echo"]

    role = load_householder_roles()[0]

    def shadow_factory(*, bus, tools, heartbeat_interval_seconds, description_resolver):
        worker = ShadowEchoWorker(
            bus,
            tools,
            role=role,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            description_resolver=description_resolver,
        )
        worker.capability_tags = ["echo"]
        return worker

    with pytest.raises(PipelineBootError, match="already registered"):
        build_pipeline(
            blockchain=Blockchain(storage=InMemoryStorage()),
            llm_decompose=lambda description, depth: None,
            worker_factories=[shadow_factory],
        )


def test_role_loader_rejects_a_definition_without_required_frontmatter(tmp_path: Path):
    (tmp_path / "broken.md").write_text("# Missing frontmatter\n", encoding="utf-8")

    with pytest.raises(HouseholderRoleError, match="YAML frontmatter"):
        load_householder_roles(tmp_path)
