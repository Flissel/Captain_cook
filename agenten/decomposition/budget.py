"""Decomposition budget: bounds how far a problem may recursively split.

Enforced in two places: the Decomposer checks depth/fanout/progress locally
before proposing children (agenten/decomposition/decomposer.py, unit U3);
the total-subproblem cap is reserved atomically inside the Ledger
Recorder's serialized write (agenten/ledger_bridge/recorder.py, unit U8) to
avoid a check-then-act race between concurrent decomposition batches under
the same root problem.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DecompositionBudget:
    max_depth: int = 4
    max_total_subproblems: int = 200
    max_fanout_per_node: int = 6
    max_tokens: Optional[int] = None


class BudgetLedger(ABC):
    """Tracks how much of a root problem's budget has been consumed.

    Implementations must be crash-safe: consumed counts are persisted
    alongside the ledger write that reserves them (see
    InProcessBudgetLedger in unit U8), not held only in process memory.
    """

    @abstractmethod
    def try_reserve(self, root_problem_id: str, budget: DecompositionBudget, n: int) -> int:
        """Reserve up to n slots against the root problem's total-subproblem
        cap. Returns the number actually reserved (0 <= result <= n); the
        caller must reject/mark-partial anything beyond what was reserved.
        """

    @abstractmethod
    def consumed(self, root_problem_id: str) -> int:
        """Total subproblems reserved so far for this root problem."""
