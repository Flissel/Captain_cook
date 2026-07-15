from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DeliveryRole(str, Enum):
    ARCHITECT_BUILDER = "architect_builder"
    REAL_CASE_TESTER = "real_case_tester"
    QUALITY_WARDEN = "quality_warden"


class DeliveryStatus(str, Enum):
    PLANNED = "planned"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    TESTING = "testing"
    REVIEWING = "reviewing"
    PASSED = "passed"
    REDO = "redo"
    ESCALATED = "escalated"


class DeliveryEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(default_factory=lambda: str(uuid4()))
    kind: str
    uri: str
    sha256: str
    created_at: datetime = Field(default_factory=utc_now)


class DeliveryTodo(BaseModel):
    model_config = ConfigDict(frozen=True)

    todo_id: str = Field(default_factory=lambda: str(uuid4()))
    project_id: str
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]
    assignee: DeliveryRole | None = None
    status: DeliveryStatus = DeliveryStatus.PLANNED
    iteration: int = 1
    max_iterations: int = 5
    lease_expires_at: datetime | None = None
    codex_session_id: str | None = None
    dependencies: tuple[str, ...] = ()
    evidence: tuple[DeliveryEvidence, ...] = ()
    failure_report_id: str | None = None
    version: int = 1
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DeliveryEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence: int
    event_id: str
    todo_id: str
    actor: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
