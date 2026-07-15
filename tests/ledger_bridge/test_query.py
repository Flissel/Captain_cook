"""Unit tests for agenten/ledger_bridge/query.py — the CQRS read side
(`LedgerQueryImpl`) and the crash-safe `InProcessBudgetLedger`.
"""
import importlib
import importlib.util
import sys
import types

import pytest

from blockchain.Blockchain_modell import Blockchain
from blockchain.storage import InMemoryStorage

from agenten.decomposition.budget import DecompositionBudget
from agenten.events.schemas import SubproblemUnroutable, topic_for
from agenten.ledger_bridge.query import (
    InProcessBudgetLedger,
    LedgerQueryImpl,
    build_status_index,
)
from agenten.ledger_bridge.stage_machine import Stage


def test_ledger_query_impl_reads_via_status_index():
    blockchain = Blockchain(storage=InMemoryStorage())
    b1 = blockchain.add_block(block_type="subproblem", data={"id": "s1"}, status=Stage.QUEUED.value)
    b2 = blockchain.add_block(block_type="subproblem", data={"id": "s2"}, status=Stage.QUEUED.value)
    status_index = {Stage.QUEUED: {b1.index, b2.index}}
    query = LedgerQueryImpl(blockchain, status_index)

    assert query.count_in_stage(Stage.QUEUED) == 2
    assert query.count_in_stage(Stage.DONE) == 0
    ids = {b.data["id"] for b in query.blocks_in_stage(Stage.QUEUED)}
    assert ids == {"s1", "s2"}
    assert query.get_block(b1.index) is b1
    assert query.get_block(9999) is None


def test_ledger_query_impl_index_mutation_helpers():
    blockchain = Blockchain(storage=InMemoryStorage())
    b1 = blockchain.add_block(block_type="subproblem", data={}, status=Stage.QUEUED.value)
    query = LedgerQueryImpl(blockchain, {})

    query._index_add(Stage.QUEUED, b1.index)
    assert query.count_in_stage(Stage.QUEUED) == 1

    query._index_move(Stage.QUEUED, Stage.VALIDATING, b1.index)
    assert query.count_in_stage(Stage.QUEUED) == 0
    assert query.count_in_stage(Stage.VALIDATING) == 1

    query._index_remove(Stage.VALIDATING, b1.index)
    assert query.count_in_stage(Stage.VALIDATING) == 0


def test_build_status_index_scans_chain_and_skips_non_stage_statuses():
    blockchain = Blockchain(storage=InMemoryStorage())  # genesis has status "completed"
    b1 = blockchain.add_block(block_type="subproblem", data={}, status=Stage.DONE.value)
    b2 = blockchain.add_block(block_type="subproblem", data={}, status=Stage.FAILED.value)

    index = build_status_index(blockchain)
    assert index[Stage.DONE] == {b1.index}
    assert index[Stage.FAILED] == {b2.index}
    assert all(Stage.QUEUED != s or b1.index not in idxs for s, idxs in index.items())


# ----------------------------------------------------------------------
# InProcessBudgetLedger: crash-safety / rehydration
# ----------------------------------------------------------------------
def test_budget_ledger_rehydrates_from_preexisting_problem_block():
    """Simulates a restart: a Blockchain already has a 'problem' block with
    `metadata['budget_consumed']` set from a previous process, and a fresh
    InProcessBudgetLedger built on top of it must pick up exactly where
    that process left off.
    """
    blockchain = Blockchain(storage=InMemoryStorage())
    budget = DecompositionBudget(max_total_subproblems=10)
    blockchain.add_block(
        block_type="problem",
        data={"problem_id": "root-x", "description": "..."},
        status=Stage.IN_PROGRESS.value,
        metadata={"budget_consumed": 7, "budget": budget.model_dump()},
    )

    ledger = InProcessBudgetLedger(blockchain)
    assert ledger.consumed("root-x") == 7

    reserved = ledger.try_reserve("root-x", budget, 5)
    assert reserved == 3  # only 3 slots left (10 - 7)
    assert ledger.consumed("root-x") == 10

    # budget now fully consumed
    assert ledger.try_reserve("root-x", budget, 1) == 0


def test_budget_ledger_fresh_root_starts_at_zero():
    blockchain = Blockchain(storage=InMemoryStorage())
    ledger = InProcessBudgetLedger(blockchain)
    assert ledger.consumed("unknown-root") == 0
    budget = DecompositionBudget(max_total_subproblems=3)
    assert ledger.try_reserve("unknown-root", budget, 2) == 2
    assert ledger.try_reserve("unknown-root", budget, 5) == 1  # only 1 left
    assert ledger.try_reserve("unknown-root", budget, 1) == 0


def test_budget_ledger_try_reserve_n_zero_or_negative_is_a_noop():
    blockchain = Blockchain(storage=InMemoryStorage())
    ledger = InProcessBudgetLedger(blockchain)
    budget = DecompositionBudget(max_total_subproblems=3)
    assert ledger.try_reserve("root", budget, 0) == 0
    assert ledger.consumed("root") == 0


# ----------------------------------------------------------------------
# Import-cleanliness of the optional AutoGen Core adapter in recorder.py
# ----------------------------------------------------------------------
@pytest.mark.skipif(
    importlib.util.find_spec("autogen_core") is not None,
    reason="autogen_core IS installed (requirements.txt pins it); the no-autogen "
    "degradation path can't be exercised in-process in this environment",
)
def test_recorder_module_imports_cleanly_without_autogen_core():
    """When autogen_core is NOT installed (see the module's `try: import
    autogen_core / except ImportError: autogen_core = None` guard), the
    module import itself must not blow up, and the RoutedAgent adapter must
    degrade to None instead of a half-defined class. Skipped when the real
    package is present -- the environment-conditional twin of
    tests/test_autogen_bus_integration.py's importorskip.
    """
    assert "autogen_core" not in sys.modules or sys.modules["autogen_core"] is None

    import agenten.ledger_bridge.recorder as recorder_module

    importlib.reload(recorder_module)
    assert recorder_module.autogen_core is None
    assert recorder_module.LedgerRecorderRoutedAgent is None
    # And the core class + RECORDER_TOPICS are still fully usable.
    assert recorder_module.LedgerRecorderAgent is not None
    assert len(recorder_module.RECORDER_TOPICS) == 10
    assert topic_for(SubproblemUnroutable) in recorder_module.RECORDER_TOPICS


def test_recorder_module_wires_routed_agent_adapter_when_autogen_core_present(monkeypatch):
    """Stubs a minimal fake `autogen_core` module (the real package isn't
    installed in this environment) to prove the `if autogen_core is not
    None:` branch in recorder.py actually defines a working
    `LedgerRecorderRoutedAgent` that forwards into a real
    `LedgerRecorderAgent`, instead of just trusting that branch never
    executes.
    """
    fake_module = types.ModuleType("autogen_core")

    class FakeRoutedAgent:
        def __init__(self, description: str = "") -> None:
            self.description = description

    def fake_message_handler(fn):
        return fn

    class FakeMessageContext:
        pass

    fake_module.RoutedAgent = FakeRoutedAgent
    fake_module.message_handler = fake_message_handler
    fake_module.MessageContext = FakeMessageContext
    monkeypatch.setitem(sys.modules, "autogen_core", fake_module)

    import agenten.ledger_bridge.recorder as recorder_module

    try:
        importlib.reload(recorder_module)
        assert recorder_module.autogen_core is fake_module
        assert recorder_module.LedgerRecorderRoutedAgent is not None

        from agenten.runtime.event_bus import InMemoryEventBus

        blockchain = Blockchain(storage=InMemoryStorage())
        budget_ledger = InProcessBudgetLedger(blockchain)
        bus = InMemoryEventBus()
        agent = recorder_module.LedgerRecorderRoutedAgent(bus, blockchain, budget_ledger)
        assert agent.query is not None
        assert agent.errors == []
    finally:
        monkeypatch.delitem(sys.modules, "autogen_core", raising=False)
        importlib.reload(recorder_module)
        # Restore-and-verify must match whatever this environment actually
        # has: with the real autogen_core installed (requirements.txt pins
        # it) the reload re-imports it and the adapter is a real class;
        # without it, both degrade to None.
        if importlib.util.find_spec("autogen_core") is not None:
            assert recorder_module.autogen_core is not None
            assert recorder_module.LedgerRecorderRoutedAgent is not None
        else:
            assert recorder_module.autogen_core is None
            assert recorder_module.LedgerRecorderRoutedAgent is None
