"""Pydantic event models for the supply-chain pipeline.

Every lifecycle transition (problem submitted, subproblem proposed/
accepted/rejected/assigned/completed/failed, retries, circuit-breaker
state) is one of these. All of them carry a `meta: EventMeta` for
idempotency/tracing, since neither AutoGen's pub/sub nor our own
restart-replay recovery guarantees exactly-once delivery.

These are Pydantic models (frozen, i.e. immutable, same as the plain
dataclasses this module started as) rather than stdlib dataclasses:
autogen_core's default message serializer rejects dataclasses that have
Optional/Union fields or nested dataclass fields ("...not supported. To
use a union/nested types, use a Pydantic model") — every event here nests
EventMeta and most have Optional fields, so plain dataclasses are not
usable verbatim as AutoGen Core message types. This was discovered by
unit U1 while wiring the real runtime adapter; DecompositionBudget
(agenten/decomposition/budget.py), the one non-EventMeta nested type,
was converted to Pydantic for the same reason.

This module has no AutoGen import — it's the contract every other unit
(U1-U11) builds against without needing autogen-core installed.
"""
from typing import Any, Dict, List, Literal, Optional
import time
import uuid

from pydantic import BaseModel, ConfigDict, Field

from agenten.decomposition.budget import DecompositionBudget


def new_event_id() -> str:
    return str(uuid.uuid4())


class EventMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

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


class ProblemSubmitted(BaseModel):
    model_config = ConfigDict(frozen=True)

    meta: EventMeta
    problem_id: str
    description: str
    priority: str = "normal"
    budget: Optional[DecompositionBudget] = None


class SubproblemProposed(BaseModel):
    model_config = ConfigDict(frozen=True)

    meta: EventMeta
    subproblem_id: str
    parent_id: Optional[str]
    depth: int
    description: str
    capability_tags: List[str] = Field(default_factory=list)
    atomic: bool = False


class SubproblemAccepted(BaseModel):
    model_config = ConfigDict(frozen=True)

    meta: EventMeta
    subproblem_id: str
    block_index: Optional[int] = None


class SubproblemRejected(BaseModel):
    model_config = ConfigDict(frozen=True)

    meta: EventMeta
    subproblem_id: str
    reason: RejectionReason
    detail: str = ""


class SubproblemAssigned(BaseModel):
    model_config = ConfigDict(frozen=True)

    meta: EventMeta
    subproblem_id: str
    agent_type: str
    agent_key: str
    lease_expires_at: float


class WorkerHeartbeat(BaseModel):
    model_config = ConfigDict(frozen=True)

    meta: EventMeta
    subproblem_id: str
    agent_type: str
    agent_key: str
    progress_note: str = ""


class SubproblemCompleted(BaseModel):
    model_config = ConfigDict(frozen=True)

    meta: EventMeta
    subproblem_id: str
    result: Dict[str, Any] = Field(default_factory=dict)


class SubproblemFailed(BaseModel):
    model_config = ConfigDict(frozen=True)

    meta: EventMeta
    subproblem_id: str
    error: str
    retriable: bool = True


class SubproblemUnroutable(BaseModel):
    model_config = ConfigDict(frozen=True)

    meta: EventMeta
    subproblem_id: str
    capability_tags: List[str] = Field(default_factory=list)
    error: str


class LeaseExpired(BaseModel):
    model_config = ConfigDict(frozen=True)

    meta: EventMeta
    subproblem_id: str
    agent_type: str
    agent_key: str


class RetryRequested(BaseModel):
    model_config = ConfigDict(frozen=True)

    meta: EventMeta
    subproblem_id: str
    delay_seconds: float = 0.0


class EscalateToRedecompose(BaseModel):
    model_config = ConfigDict(frozen=True)

    meta: EventMeta
    subproblem_id: str
    reason: str = ""


class CircuitStateChanged(BaseModel):
    model_config = ConfigDict(frozen=True)

    meta: EventMeta
    agent_type: str
    state: CircuitState
