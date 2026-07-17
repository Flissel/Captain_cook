"""Immutable canonical-plan contracts shared across process boundaries."""

import json
from enum import Enum
from typing import Tuple

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.validation.contracts import WorkBatch


PLAN_SCHEMA_VERSION = "captain-canonical-plan/v1"


class WorkPackageStatus(str, Enum):
    PLANNED = "planned"
    REUSED = "reused"


class CanonicalWorkPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_contract: str = Field(validation_alias=AliasChoices("batch", "batch_contract"))
    status: WorkPackageStatus
    worker_id: str = Field(pattern=r"^worker-[0-9]{2}$")
    handoff: str = Field(pattern=r"^HANDOFF TO WORKER [1-9][0-9]*$")
    holdout_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("batch_contract", mode="before")
    @classmethod
    def seal_batch_contract(cls, value: object) -> str:
        if isinstance(value, WorkBatch):
            batch = value
        elif isinstance(value, str):
            batch = WorkBatch.model_validate_json(value)
        else:
            batch = WorkBatch.model_validate(value)
        return json.dumps(
            batch.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def batch(self) -> WorkBatch:
        return WorkBatch.model_validate_json(self.batch_contract)

    @property
    def batch_id(self) -> str:
        return self.batch.batch_id

    @property
    def depends_on(self) -> tuple[str, ...]:
        return tuple(self.batch.depends_on)

    @model_validator(mode="after")
    def status_matches_capability_resolution(self) -> "CanonicalWorkPackage":
        expected = WorkPackageStatus.REUSED if self.batch.satisfied_by else WorkPackageStatus.PLANNED
        if self.status is not expected:
            raise ValueError("work package status must be derived from satisfied_by")
        worker_number = int(self.worker_id.removeprefix("worker-"))
        handoff_number = int(self.handoff.rsplit(" ", 1)[1])
        if worker_number != handoff_number:
            raise ValueError("handoff worker number must match worker_id")
        return self


class CanonicalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = PLAN_SCHEMA_VERSION
    plan_id: str = Field(pattern=r"^plan-[0-9a-f]{24}$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_reference: str = Field(min_length=1)
    worker_pool: Tuple[str, ...] = Field(min_length=1)
    work_packages: Tuple[CanonicalWorkPackage, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dag_and_workers(self) -> "CanonicalPlan":
        if len(self.worker_pool) != len(set(self.worker_pool)):
            raise ValueError("worker_pool must not contain duplicates")
        batch_ids = [package.batch_id for package in self.work_packages]
        if len(batch_ids) != len(set(batch_ids)):
            raise ValueError("canonical plan contains duplicate batch ids")
        positions = {batch_id: index for index, batch_id in enumerate(batch_ids)}
        for package in self.work_packages:
            if package.worker_id not in self.worker_pool:
                raise ValueError(f"unknown worker id: {package.worker_id}")
            for dependency in package.depends_on:
                if dependency not in positions:
                    raise ValueError(f"unknown dependency: {dependency}")
                if positions[dependency] >= positions[package.batch_id]:
                    raise ValueError("work packages must be in dependency order")
        return self
