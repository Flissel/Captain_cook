"""Workers run outside the publish chain.

On ``InMemoryEventBus`` a handler runs nested inside whoever published, which
for ``SubproblemAssigned`` is the Ledger Recorder's writer loop. A worker that
executed inline there blocked every ledger write and every other subproblem
until it finished.
"""
import asyncio

import pytest

from blockchain.Blockchain_modell import Blockchain
from blockchain.storage import InMemoryStorage

from agenten.ledger_bridge.stage_machine import Stage
from agenten.orchestration.pipeline import build_pipeline
from agenten.workers.base import WorkerAgent

pytestmark = pytest.mark.asyncio


async def _accept_all_judge(description, ruleset) -> bool:
    return True


async def _two_subproblems(description: str, depth: int) -> list[dict[str, object]]:
    if depth != 0:
        return []
    return [
        {"description": f"First half of {description}", "capability_tags": ["rendezvous"], "atomic": True},
        {"description": f"Second half of {description}", "capability_tags": ["rendezvous"], "atomic": True},
    ]


def _pipeline_with(worker_cls: type[WorkerAgent]):
    def factory(*, bus, tools, heartbeat_interval_seconds, description_resolver):
        return worker_cls(
            bus,
            tools,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            description_resolver=description_resolver,
        )

    return build_pipeline(
        blockchain=Blockchain(storage=InMemoryStorage()),
        llm_decompose=_two_subproblems,
        llm_judge=_accept_all_judge,
        worker_factories=(factory,),
    )


async def test_two_subproblems_execute_at_the_same_time():
    both_running = asyncio.Event()
    running: list[str] = []

    class RendezvousWorker(WorkerAgent):
        agent_type = "rendezvous_worker"
        capability_tags = ["rendezvous"]

        async def execute(self, subproblem_id: str, description: str) -> dict[str, object]:
            running.append(subproblem_id)
            if len(running) == 2:
                both_running.set()
            # Serial execution never lets the second worker start.
            await asyncio.wait_for(both_running.wait(), timeout=2.0)
            return {"done": description}

    pipeline = _pipeline_with(RendezvousWorker)
    await pipeline.start()
    await pipeline.submit_problem("a concurrent problem")

    converged = await pipeline.wait_until_terminal(expected_subproblem_count=2, timeout=5.0)
    await pipeline.stop()

    assert converged
    assert pipeline.recorder.errors == []
    assert len(pipeline.ledger_query.blocks_in_stage(Stage.DONE)) == 2
    # Each subproblem ran once: no timeout, no retry got them through.
    assert len(running) == 2


async def test_stop_waits_for_workers_still_executing():
    class SlowWorker(WorkerAgent):
        agent_type = "slow_worker"
        capability_tags = ["rendezvous"]

        async def execute(self, subproblem_id: str, description: str) -> dict[str, object]:
            await asyncio.sleep(0.05)
            return {"done": description}

    pipeline = _pipeline_with(SlowWorker)
    await pipeline.start()
    await pipeline.submit_problem("stop right away")

    await asyncio.wait_for(pipeline.stop(), timeout=5.0)

    assert pipeline.recorder.errors == []
    assert len(pipeline.ledger_query.blocks_in_stage(Stage.DONE)) == 2
