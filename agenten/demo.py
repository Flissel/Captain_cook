"""Offline, deterministic presentation adapter for the Captain Cook pipeline."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from autogen_core.models import ChatCompletionClient

from blockchain.Blockchain_modell import Blockchain
from blockchain.storage import InMemoryStorage

from agenten.household.llm_executor import LlmHouseholderExecutor
from agenten.household.worker import create_householder_worker_factories
from agenten.ledger_bridge.stage_machine import Stage, TERMINAL_STAGES
from agenten.orchestration.pipeline import build_pipeline

DEMO_PROBLEM = "Prepare a small engineering brief for a new workflow automation."
EXPECTED_SUBPROBLEM_COUNT = 4
DETERMINISTIC_CONVERGENCE_TIMEOUT_SECONDS = 5.0
LLM_CALL_TIMEOUT_SECONDS = 30.0
LLM_CALL_MAX_ATTEMPTS = 2
LLM_CONVERGENCE_MARGIN_SECONDS = 30.0
# Worst case: every householder runs sequentially and exhausts all attempts.
LLM_CONVERGENCE_TIMEOUT_SECONDS = (
    EXPECTED_SUBPROBLEM_COUNT * LLM_CALL_TIMEOUT_SECONDS * LLM_CALL_MAX_ATTEMPTS
    + LLM_CONVERGENCE_MARGIN_SECONDS
)


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
            "description": f"Review the architecture boundary for: {description}",
            "capability_tags": ["architecture_review"],
            "atomic": True,
        },
        {
            "description": f"Review ledger evidence needs for: {description}",
            "capability_tags": ["ledger_review"],
            "atomic": True,
        },
        {
            "description": f"Create a delivery plan for: {description}",
            "capability_tags": ["delivery_plan"],
            "atomic": True,
        },
        {
            "description": f"Audit release evidence for: {description}",
            "capability_tags": ["quality_review"],
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


async def run_demo(
    output_path: Path | None = None,
    *,
    householder_model_client: ChatCompletionClient | None = None,
) -> DemoSummary:
    """Run the pipeline demo and optionally persist its evidence.

    Without a model client the householders stay deterministic and the demo
    needs no network. With one, all four householders report through
    `LlmHouseholderExecutor`, whose evidence is marked `llm_reported:`.
    """
    blockchain = Blockchain(storage=InMemoryStorage())
    executor = (
        LlmHouseholderExecutor(
            householder_model_client,
            timeout_seconds=LLM_CALL_TIMEOUT_SECONDS,
            max_attempts=LLM_CALL_MAX_ATTEMPTS,
        )
        if householder_model_client is not None
        else None
    )
    convergence_timeout = (
        DETERMINISTIC_CONVERGENCE_TIMEOUT_SECONDS
        if householder_model_client is None
        else LLM_CONVERGENCE_TIMEOUT_SECONDS
    )
    pipeline = build_pipeline(
        llm_decompose=_deterministic_decompose,
        llm_judge=_accept_all,
        blockchain=blockchain,
        worker_factories=create_householder_worker_factories(executor=executor),
    )

    await pipeline.start()
    try:
        problem_id = await pipeline.submit_problem(DEMO_PROBLEM)
        converged = await pipeline.wait_until_terminal(
            expected_subproblem_count=EXPECTED_SUBPROBLEM_COUNT,
            timeout=convergence_timeout,
        )
        if not converged:
            raise RuntimeError(
                f"Demo did not reach a terminal state within {convergence_timeout:g} seconds"
            )

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
