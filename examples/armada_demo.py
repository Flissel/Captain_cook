"""Runnable demo of the unit-U11 supply-chain pipeline integration.

Builds the pipeline (agenten.orchestration.pipeline.build_pipeline) with a
canned, non-LLM `llm_decompose` that splits a hardcoded "Problem X"
description into exactly 2 subproblems tagged for the EchoWorker's
capability, and a canned `llm_judge` that always accepts. Submits the
problem, drives the InMemoryEventBus/asyncio event loop until both
subproblems reach a terminal ledger stage, then prints the resulting
blockchain contents -- the full supply-chain audit trail from the "problem"
block down through both "subproblem" blocks to Stage.DONE.

Run with:

    python examples/armada_demo.py
"""
import asyncio
import os
import sys

# Allow `python examples/armada_demo.py` to be run directly (not just
# `python -m examples.armada_demo`) without requiring the repo root to
# already be on PYTHONPATH -- mirrors how main.py at the repo root can
# import `agenten`/`blockchain` for free by virtue of living there.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blockchain.Blockchain_modell import Blockchain
from blockchain.storage import InMemoryStorage

from agenten.ledger_bridge.stage_machine import Stage
from agenten.orchestration.pipeline import build_pipeline

PROBLEM_DESCRIPTION = "Problem X: launch a new product line into an unfamiliar market"


async def canned_llm_decompose(description, depth):
    """Deterministic stand-in for a real LLM-backed decomposition call.
    Splits any depth-0 problem into exactly 2 atomic subproblems tagged
    "echo" (routable to EchoWorker, the dependency-free reference worker
    from unit U5) and proposes nothing at any deeper depth.
    """
    if depth != 0:
        return []
    return [
        {
            "description": f"Research phase: gather market data for {description!r}",
            "capability_tags": ["echo"],
            "atomic": True,
        },
        {
            "description": f"Execution phase: draft the go-to-market plan for {description!r}",
            "capability_tags": ["echo"],
            "atomic": True,
        },
    ]


async def canned_llm_judge(description, ruleset):
    """Stand-in for a real LLM-backed semantic scope/quality judge: always
    accepts (layer-1 deterministic checks in agenten/constitution/validators.py
    already ran first and would have rejected anything malformed).
    """
    return True


def _print_block(block, indent: int = 0) -> None:
    prefix = "  " * indent
    label = block.data.get("description") or block.data.get("problem_id") or block.block_type
    print(f"{prefix}[{block.index}] {block.block_type:<10} status={block.status:<10} {label}")
    if block.block_type == "subproblem" and "result" in block.data:
        print(f"{prefix}      result={block.data['result']!r}")


def print_audit_trail(blockchain: Blockchain) -> None:
    print("\n=== Supply-chain ledger audit trail ===")
    for problem_block in blockchain.get_blocks_by_type("problem"):
        _print_block(problem_block)
        for child_index in problem_block.children:
            child = blockchain.get_block(child_index)
            if child is not None:
                _print_block(child, indent=1)
    print("=== end audit trail ===\n")


async def main() -> int:
    blockchain = Blockchain(storage=InMemoryStorage())
    pipeline = build_pipeline(
        llm_decompose=canned_llm_decompose,
        llm_judge=canned_llm_judge,
        blockchain=blockchain,
    )

    await pipeline.start()
    try:
        problem_id = await pipeline.submit_problem(PROBLEM_DESCRIPTION)
        print(f"Submitted problem_id={problem_id!r}: {PROBLEM_DESCRIPTION!r}")

        converged = await pipeline.wait_until_terminal(expected_subproblem_count=2, timeout=10.0)
        if not converged:
            print("ERROR: pipeline did not converge to a terminal state within the timeout", file=sys.stderr)
            return 1

        # Unit U7 (Reaper): a single scan_once() call is enough to prove
        # it's wired up correctly against the same ledger/bus -- nothing
        # should have an expired lease in this happy-path run, so this is
        # expected to report zero newly-expired leases.
        reaped = await pipeline.reaper.scan_once()
        print(f"Reaper.scan_once(): {len(reaped)} lease(s) newly flagged as expired (expected: 0)")

        if pipeline.recorder.errors:
            print(f"WARNING: LedgerRecorder logged {len(pipeline.recorder.errors)} error(s): {pipeline.recorder.errors}")

        print(f"Stage.DONE count: {pipeline.ledger_query.count_in_stage(Stage.DONE)}")
        print_audit_trail(blockchain)
        return 0
    finally:
        await pipeline.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
