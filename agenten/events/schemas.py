"""Frozen event dataclasses for the supply-chain pipeline.

Every lifecycle transition (problem submitted, subproblem proposed/
accepted/rejected/assigned/completed/failed, retries, circuit-breaker
state) is one of these. All of them carry a `meta: EventMeta` for
idempotency/tracing, since neither AutoGen's pub/sub nor our own
restart-replay recovery guarantees exactly-once delivery.

This module has no AutoGen import — it's the contract every other unit
(U1-U11) builds against without needing autogen-core installed.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional
import time
import uuid

from agenten.decomposition.budget import DecompositionBudget


def new_event_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class EventMeta:
    event_id: str
    correlation_id: str  # stable id of the thing this event is about (subproblem_id / block index)
    root_problem_id: str
    attempt: int = 0
    ts: float = 0.0
    constitution_version: str = "unset"


def make_meta(
    correlation_id: str,
    root_problem_id: str,
    attempt: int = 0,
    constitution_version: str = "unset",
    ts: Optional[float] = None,
) -> EventMeta:
    return EventMeta(
        event_id=new_event_id(),
        correlation_id=correlation_id,
        root_problem_id=root_problem_id,
        attempt=attempt,
        ts=ts if ts is not None else time.time(),
        constitution_version=constitution_version,
    )


def topic_for(event_type: type) -> str:
    """The pub/sub topic name an event class is published/subscribed on."""
    return f"supplychain.{event_type.__name__}"


RejectionReason = Literal[
    "out_of_scope", "duplicate", "malformed", "budget_exceeded", "fanout_exceeded", "quality_bar"
]
CircuitState = Literal["closed", "open", "half_open"]


@dataclass(frozen=True)
class ProblemSubmitted:
    meta: EventMeta
    problem_id: str
    description: str
    priority: str = "normal"
    budget: Optional[DecompositionBudget] = None


@dataclass(frozen=True)
class SubproblemProposed:
    meta: EventMeta
    subproblem_id: str
    parent_id: Optional[str]
    depth: int
    description: str
    capability_tags: List[str] = field(default_factory=list)
    atomic: bool = False


@dataclass(frozen=True)
class SubproblemAccepted:
    meta: EventMeta
    subproblem_id: str
    block_index: Optional[int] = None


@dataclass(frozen=True)
class SubproblemRejected:
    meta: EventMeta
    subproblem_id: str
    reason: RejectionReason
    detail: str = ""


@dataclass(frozen=True)
class SubproblemAssigned:
    meta: EventMeta
    subproblem_id: str
    agent_type: str
    agent_key: str
    lease_expires_at: float


@dataclass(frozen=True)
class WorkerHeartbeat:
    meta: EventMeta
    subproblem_id: str
    agent_type: str
    agent_key: str
    progress_note: str = ""


@dataclass(frozen=True)
class SubproblemCompleted:
    meta: EventMeta
    subproblem_id: str
    result: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubproblemFailed:
    meta: EventMeta
    subproblem_id: str
    error: str
    retriable: bool = True


@dataclass(frozen=True)
class LeaseExpired:
    meta: EventMeta
    subproblem_id: str
    agent_type: str
    agent_key: str


@dataclass(frozen=True)
class RetryRequested:
    meta: EventMeta
    subproblem_id: str
    delay_seconds: float = 0.0


@dataclass(frozen=True)
class EscalateToRedecompose:
    meta: EventMeta
    subproblem_id: str
    reason: str = ""


@dataclass(frozen=True)
class CircuitStateChanged:
    meta: EventMeta
    agent_type: str
    state: CircuitState
