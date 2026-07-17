"""Validation boundary for immutable Hermes planning artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal, Protocol
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agenten.agent_runtime.contracts import (
    AgentBlueprint,
    ArtifactRef,
    HermesPlanResult,
    IntegrationIntent,
)


class HermesPlanningError(RuntimeError):
    """Hermes output did not satisfy the Captain-owned planning boundary."""


class PlanningArtifactReader(Protocol):
    async def read(self, reference: ArtifactRef) -> bytes: ...


class HermesPlanningDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.hermes-planning-document.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    project_id: str = Field(min_length=1)
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    objective: str = Field(min_length=1)
    planner_id: str = Field(min_length=1)
    blueprint_digests: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_unique_blueprints(self) -> "HermesPlanningDocument":
        if len(self.blueprint_digests) != len(set(self.blueprint_digests)):
            raise ValueError("blueprint digests must not contain duplicates")
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in self.blueprint_digests):
            raise ValueError("blueprint digests must be lowercase SHA-256 values")
        return self


class ValidatedPlanningInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1)
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    plan_ref: ArtifactRef
    decision_log_ref: ArtifactRef
    objective: str = Field(min_length=1)
    blueprints: tuple[AgentBlueprint, ...]
    planner_id: str = Field(min_length=1)
    runtime_provenance: str = Field(min_length=1)


_SECRET_VALUE = re.compile(
    rb"(?i)(?:api[_-]?key|authorization|bearer|password|secret|token)\s*(?::|=)\s*\S+"
)
_ABSOLUTE_USER_PATH = re.compile(rb"(?i)(?:[a-z]:\\|/(?:home|Users)/)")


class HermesPlanReader:
    def __init__(self, artifacts: PlanningArtifactReader) -> None:
        self._artifacts = artifacts

    async def read(self, result: HermesPlanResult) -> ValidatedPlanningInput:
        plan_bytes = await self._read_verified(result.plan_ref, label="plan")
        await self._read_verified(result.decision_log_ref, label="decision log")
        try:
            document = HermesPlanningDocument.model_validate_json(plan_bytes)
        except (ValueError, ValidationError):
            raise HermesPlanningError("plan artifact has an invalid schema") from None
        if _SECRET_VALUE.search(plan_bytes) or _ABSOLUTE_USER_PATH.search(plan_bytes):
            raise HermesPlanningError("plan artifact contains forbidden private data")
        if document.project_id != result.project_id:
            raise HermesPlanningError("plan project does not match its result")
        if document.correlation_id != result.correlation_id:
            raise HermesPlanningError("plan correlation does not match its result")
        if document.subject_version != result.subject_version:
            raise HermesPlanningError("plan version does not match its result")
        if document.planner_id != result.planner_id:
            raise HermesPlanningError("plan planner does not match its provenance")
        if result.minibook.project_id != result.project_id:
            raise HermesPlanningError("Minibook projection project does not match plan")
        expected_digests = tuple(reference.sha256 for reference in result.blueprint_refs)
        if document.blueprint_digests != expected_digests:
            raise HermesPlanningError("plan blueprint digests do not match its result")

        blueprints: list[AgentBlueprint] = []
        for reference in result.blueprint_refs:
            content = await self._read_verified(reference, label="blueprint")
            try:
                if reference.media_type in {"application/yaml", "text/yaml"}:
                    value = yaml.safe_load(content.decode("utf-8"))
                elif reference.media_type == "application/json":
                    value = json.loads(content)
                else:
                    raise HermesPlanningError("blueprint media type is not supported")
                blueprints.append(AgentBlueprint.model_validate(value))
            except HermesPlanningError:
                raise
            except (UnicodeDecodeError, ValueError, ValidationError, yaml.YAMLError):
                raise HermesPlanningError("blueprint artifact is invalid") from None

        reported_intents = {
            IntegrationIntent(value)
            for value in result.integration_intents
            if IntegrationIntent(value) is not IntegrationIntent.NONE
        }
        blueprint_intents = {
            blueprint.integration_intent
            for blueprint in blueprints
            if blueprint.integration_intent is not IntegrationIntent.NONE
        }
        if reported_intents != blueprint_intents:
            raise HermesPlanningError("blueprint integration intents do not match result")
        return ValidatedPlanningInput(
            project_id=result.project_id,
            correlation_id=result.correlation_id,
            subject_version=result.subject_version,
            plan_ref=result.plan_ref,
            decision_log_ref=result.decision_log_ref,
            objective=document.objective,
            blueprints=tuple(blueprints),
            planner_id=result.planner_id,
            runtime_provenance=result.runtime_provenance,
        )

    async def _read_verified(self, reference: ArtifactRef, *, label: str) -> bytes:
        try:
            content = await self._artifacts.read(reference)
        except Exception:
            raise HermesPlanningError(f"{label} artifact is unavailable") from None
        if not isinstance(content, bytes):
            raise HermesPlanningError(f"{label} artifact reader returned invalid bytes")
        digest = hashlib.sha256(content).hexdigest()
        if digest != reference.sha256:
            raise HermesPlanningError(f"{label} artifact digest does not match reference")
        return content
