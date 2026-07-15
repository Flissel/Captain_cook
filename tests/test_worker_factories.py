"""Contract tests for injecting a deterministic worker fleet into the pipeline."""
import pytest

from blockchain.Blockchain_modell import Blockchain
from blockchain.storage import InMemoryStorage

from agenten.orchestration.pipeline import build_pipeline
from agenten.workers.base import WorkerAgent


class ManifestWorker(WorkerAgent):
    agent_type = "manifest_worker"
    capability_tags = ["manifest_review"]

    async def execute(self, subproblem_id: str, description: str) -> dict[str, object]:
        return {"reviewed": description}


@pytest.mark.asyncio
async def test_pipeline_injects_a_worker_factory_with_ledger_resolved_description():
    async def decompose(description: str, depth: int) -> list[dict[str, object]]:
        if depth != 0:
            return []
        return [
            {
                "description": f"Manifest review for {description}",
                "capability_tags": ["manifest_review"],
                "atomic": True,
            }
        ]

    def factory(*, bus, tools, heartbeat_interval_seconds, description_resolver):
        return ManifestWorker(
            bus,
            tools,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            description_resolver=description_resolver,
        )

    pipeline = build_pipeline(
        blockchain=Blockchain(storage=InMemoryStorage()),
        llm_decompose=decompose,
        worker_factories=(factory,),
    )
    await pipeline.start()
    await pipeline.submit_problem("Check the worker factory contract")
    assert await pipeline.wait_until_terminal(expected_subproblem_count=1, timeout=5.0)
    await pipeline.stop()

    done = pipeline.ledger_query.blocks_in_stage("done")
    assert len(done) == 1
    assert done[0].data["agent_type"] == "manifest_worker"
    assert done[0].data["result"] == {"reviewed": "Manifest review for Check the worker factory contract"}
