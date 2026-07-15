"""Offline, deterministic presentation adapter for the Captain Cook pipeline."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from blockchain.Blockchain_modell import Blockchain
from blockchain.storage import InMemoryStorage

from agenten.ledger_bridge.stage_machine import Stage, TERMINAL_STAGES
from agenten.orchestration.pipeline import build_pipeline

DEMO_PROBLEM = "Prepare a small engineering brief for a new workflow automation."
EXPECTED_SUBPROBLEM_COUNT = 2


@dataclass(frozen=True)
class DemoSummary:
    """Inspectable outcome of one deterministic pipeline execution."""

    success: bool
    problem_id: str
    terminal_count: int
    done_count: int
    blocks: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


async def _deterministic_decompose(description: str, depth: int) -> list[dict[str, object]]:
    if depth != 0:
        return []
    return [
        {
            "description": f"Collect implementation constraints for: {description}",
            "capability_tags": ["echo"],
            "atomic": True,
        },
        {
            "description": f"Draft an execution brief for: {description}",
            "capability_tags": ["echo"],
            "atomic": True,
        },
    ]


async def _accept_all(_description: str, _ruleset: object) -> bool:
    return True


def _serialize_block(block: object) -> dict[str, object]:
    index = getattr(block, "index")
    block_type = getattr(block, "block_type")
    status = getattr(block, "status")
    data = getattr(block, "data")

    serialized: dict[str, object] = {
        "index": index,
        "block_type": block_type,
        "status": status,
    }
    if isinstance(data, dict):
        description = data.get("description")
        result = data.get("result")
        if isinstance(description, str):
            serialized["description"] = description
        if isinstance(result, dict):
            serialized["result"] = result
    return serialized


async def run_demo(output_path: Path | None = None) -> DemoSummary:
    """Run the dependency-free pipeline demo and optionally persist its evidence."""
    blockchain = Blockchain(storage=InMemoryStorage())
    pipeline = build_pipeline(
        llm_decompose=_deterministic_decompose,
        llm_judge=_accept_all,
        blockchain=blockchain,
    )

    await pipeline.start()
    try:
        problem_id = await pipeline.submit_problem(DEMO_PROBLEM)
        converged = await pipeline.wait_until_terminal(
            expected_subproblem_count=EXPECTED_SUBPROBLEM_COUNT,
            timeout=5.0,
        )
        if not converged:
            raise RuntimeError("Offline demo did not reach a terminal state within five seconds")

        terminal_count = sum(
            pipeline.ledger_query.count_in_stage(stage)
            for stage in TERMINAL_STAGES
        )
        done_count = pipeline.ledger_query.count_in_stage(Stage.DONE)
        summary = DemoSummary(
            success=done_count == EXPECTED_SUBPROBLEM_COUNT and not pipeline.recorder.errors,
            problem_id=problem_id,
            terminal_count=terminal_count,
            done_count=done_count,
            blocks=[_serialize_block(block) for block in blockchain.chain],
        )
    finally:
        await pipeline.stop()

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(summary.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return summary
