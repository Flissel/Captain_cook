"""Ledger read-side (CQRS query) + the budget ledger implementation — unit U8.

`LedgerQueryImpl` is the concrete `LedgerQuery` (see stage_machine.py, unit
U0): it never scans the whole chain for `count_in_stage`/`blocks_in_stage`,
it consults a `status_index: Dict[Stage, Set[int]]` that the Ledger
Recorder (recorder.py) keeps in lockstep with every write it makes. This
module owns the index's *shape* and read access; `recorder.py` owns
mutating it (via the `_index_add` / `_index_remove` / `_index_move` helpers
below), since only the Recorder's single writer-loop coroutine is allowed
to touch it without a race.

`InProcessBudgetLedger` is the crash-safe `BudgetLedger` implementation:
consumed counts are never held *only* in memory. On construction it
rehydrates from each root problem's `problem` block
(`block.metadata["budget_consumed"]`), so a fresh process restarted on top
of an existing ledger picks up exactly where the last one left off.
`try_reserve` itself only mutates the in-memory counter; it is documented
(and only ever called from) inside the Ledger Recorder's single
writer-loop critical section, so the in-memory counter and the on-chain
`budget_consumed` value are never observed out of sync by another writer —
the Recorder persists the updated count into the same root problem block
as part of the write that consumed it.
"""
from typing import Dict, List, Optional, Set

from blockchain.Blockchain_modell import Block, Blockchain

from agenten.decomposition.budget import BudgetLedger, DecompositionBudget
from agenten.ledger_bridge.stage_machine import LedgerQuery, Stage


def build_status_index(blockchain: Blockchain) -> Dict[Stage, Set[int]]:
    """Reconstruct a `status_index` by scanning `blockchain.chain` once.

    Not used on the hot write path (the Recorder maintains the index
    incrementally after this point) — used once by
    `LedgerRecorderAgent.__init__` to seed `self.query` so a process
    starting up on top of an existing ledger (crash/restart) reports
    correct counts immediately instead of only from the next write
    onward. Also useful directly in tests, and for unit U9's startup
    recovery if it needs to rebuild a `LedgerQueryImpl` independently.
    """
    index: Dict[Stage, Set[int]] = {}
    for block in blockchain.chain:
        try:
            stage = Stage(block.status)
        except ValueError:
            continue  # not a pipeline-stage status (e.g. the genesis block's "completed")
        index.setdefault(stage, set()).add(block.index)
    return index


class LedgerQueryImpl(LedgerQuery):
    """Concrete read-side of the ledger CQRS split.

    `status_index` is intentionally a constructor argument, not something
    this class builds for itself: the Ledger Recorder owns the single
    instance of this index (created once, in `LedgerRecorderAgent.__init__`
    — seeded via `build_status_index(blockchain)` so a process starting up
    on top of an existing ledger doesn't undercount, then mutated
    incrementally from there) as part of the same writer-loop turn that
    performs the corresponding blockchain write, so reads and writes never
    disagree about what stage a block is "really" in.
    """

    def __init__(
        self,
        blockchain: Blockchain,
        status_index: Dict[Stage, Set[int]],
        subproblem_index: Optional[Dict[str, int]] = None,
    ):
        self._blockchain = blockchain
        self._status_index = status_index
        # subproblem_id -> block index. Like `status_index`, owned and
        # mutated by the Ledger Recorder (it shares its own
        # `_subproblem_index` dict by reference at construction time) —
        # this class only reads it. Optional so a standalone
        # LedgerQueryImpl (tests, ad-hoc read-side use) still works: in
        # that case `find_block_by_subproblem_id` falls back to a linear
        # chain scan instead of the O(1) lookup.
        self._subproblem_index = subproblem_index

    def find_block_by_subproblem_id(self, subproblem_id: str) -> Optional[Block]:
        """Look up the ledger block for a subproblem_id.

        O(1) via the Recorder-maintained `subproblem_index` when this
        instance was constructed with one (the normal, Recorder-owned
        case); otherwise a linear scan of the chain. Centralizes the
        "find the block for this subproblem" lookup that would otherwise
        be re-implemented as an O(all-blocks) stage-by-stage scan at every
        call site (unit U11's pipeline helpers use this; note that
        `SpawnCoordinatorAgent._find_block_for_subproblem`, merged before
        this method existed, still carries its own scan and can migrate
        here as a follow-up).
        """
        if self._subproblem_index is not None:
            idx = self._subproblem_index.get(subproblem_id)
            return self._blockchain.get_block(idx) if idx is not None else None
        for block in self._blockchain.chain:
            if block.block_type == "subproblem" and block.data.get("subproblem_id") == subproblem_id:
                return block
        return None

    def count_in_stage(self, stage: Stage) -> int:
        return len(self._status_index.get(stage, ()))

    def blocks_in_stage(self, stage: Stage) -> List[Block]:
        indices = self._status_index.get(stage, ())
        blocks = (self._blockchain.get_block(i) for i in indices)
        return [b for b in blocks if b is not None]

    def get_block(self, index: int) -> Optional[Block]:
        return self._blockchain.get_block(index)

    # --- mutation helpers: called ONLY by the Recorder's writer loop ---
    def _index_add(self, stage: Stage, index: int) -> None:
        self._status_index.setdefault(stage, set()).add(index)

    def _index_remove(self, stage: Stage, index: int) -> None:
        bucket = self._status_index.get(stage)
        if bucket is not None:
            bucket.discard(index)

    def _index_move(self, old_stage: Optional[Stage], new_stage: Stage, index: int) -> None:
        if old_stage is not None:
            self._index_remove(old_stage, index)
        self._index_add(new_stage, index)


class InProcessBudgetLedger(BudgetLedger):
    """Crash-safe `BudgetLedger`: rehydrates consumed counts from the chain
    on construction; every mutation is expected to happen from inside the
    Ledger Recorder's serialized writer loop (see recorder.py's module
    docstring for why that makes this safe without any locking here).
    """

    def __init__(self, blockchain: Blockchain):
        self._blockchain = blockchain
        self._consumed: Dict[str, int] = {}
        self._rehydrate()

    def _rehydrate(self) -> None:
        for block in self._blockchain.get_blocks_by_type("problem"):
            root_problem_id = block.data.get("problem_id")
            if not root_problem_id:
                continue
            self._consumed[root_problem_id] = int(block.metadata.get("budget_consumed", 0))

    def try_reserve(self, root_problem_id: str, budget: DecompositionBudget, n: int) -> int:
        if n <= 0:
            return 0
        current = self._consumed.get(root_problem_id, 0)
        available = max(budget.max_total_subproblems - current, 0)
        reserved = min(available, n)
        if reserved > 0:
            self._consumed[root_problem_id] = current + reserved
        return reserved

    def consumed(self, root_problem_id: str) -> int:
        return self._consumed.get(root_problem_id, 0)
