"""Pipeline stage state machine + the ledger read-side (CQRS query) interface.

Stage transitions are validated centrally so an invalid jump (e.g. QUEUED
straight to DONE) is caught at the point of write, not discovered later as
ledger corruption. Enforced by the Ledger Recorder (unit U8), the sole
writer to the chain.
"""
from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module import-light
    from blockchain.Blockchain_modell import Block


class Stage(str, Enum):
    QUEUED = "queued"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"
    RETRYING = "retrying"
    REJECTED = "rejected"


TERMINAL_STAGES: Set[Stage] = {Stage.DONE, Stage.FAILED, Stage.REJECTED}

ALLOWED_TRANSITIONS: Dict[Stage, Set[Stage]] = {
    Stage.QUEUED: {Stage.VALIDATING},
    Stage.VALIDATING: {Stage.ACCEPTED, Stage.REJECTED},
    Stage.ACCEPTED: {Stage.ASSIGNED, Stage.FAILED},
    Stage.ASSIGNED: {Stage.IN_PROGRESS, Stage.RETRYING, Stage.FAILED},
    Stage.IN_PROGRESS: {Stage.VERIFYING, Stage.RETRYING, Stage.FAILED},
    Stage.VERIFYING: {Stage.DONE, Stage.FAILED},
    Stage.RETRYING: {Stage.ASSIGNED, Stage.FAILED},
    Stage.DONE: set(),
    Stage.FAILED: set(),
    Stage.REJECTED: set(),
}


def validate_transition(current: Stage, target: Stage) -> None:
    if current in TERMINAL_STAGES:
        raise ValueError(f"Cannot transition out of terminal stage {current!r} (attempted -> {target!r})")
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"Illegal stage transition {current!r} -> {target!r}")


class LedgerQuery(ABC):
    """Read side of the ledger CQRS split. The concrete implementation
    (unit U8, agenten/ledger_bridge/query.py) maintains a status index
    alongside every write so these are O(1)/O(stage-size), not a linear
    scan of the whole chain.
    """

    @abstractmethod
    def count_in_stage(self, stage: Stage) -> int: ...

    @abstractmethod
    def blocks_in_stage(self, stage: Stage) -> List["Block"]: ...

    @abstractmethod
    def get_block(self, index: int) -> Optional["Block"]: ...
