"""Claim-bound, durable Package-C runtime execution over the Codex port."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid5

from agenten.agent_factory.capability_factory_entrypoint import (
    CapabilityExecutionPlan,
    CapabilityRuntimeExecution,
)
from agenten.agent_factory.capability_live_adapters import ContentAddressedArtifactStore
from agenten.agent_factory.contracts import AgentFactoryJobV2
from agenten.agent_factory.outcome_contracts import (
    ExecutionOutcomeV1,
    validate_execution_outcome_binding,
)
from agenten.agent_runtime.capabilities import PROFILE_CAPABILITIES
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    CapabilityGrant,
    CapabilityProfile,
    IntegrationIntent,
    ProviderEffectReceipt,
    RuntimeOperation,
    RuntimeStatus,
)
from agenten.agent_runtime.ports import CodexExecutionPort
from gateway.capability_catalog import CapabilityCatalogRecord
from gateway.contracts import (
    RuntimeExecutionClaim,
    RuntimeExecutionClaimReceipt,
    canonical_contract_sha256,
)


class ClaimAwareRuntimeRecoveryRequired(RuntimeError):
    """A provider effect may have started but no durable result is available."""


def capability_runtime_batch_id(job: AgentFactoryJobV2) -> str:
    """Return the bounded, immutable Gateway batch identity for one release."""

    return f"capability-release-{job.job_id.hex[:12]}"


class CapabilityEffectStorePort(Protocol):
    def claim(
        self,
        plan: CapabilityExecutionPlan,
        claim: RuntimeExecutionClaim,
        effect_id: UUID,
    ) -> bool: ...

    def complete(
        self,
        execution: CapabilityRuntimeExecution,
    ) -> CapabilityRuntimeExecution: ...

    def lookup(
        self,
        *,
        command_id: UUID,
        effect_id: UUID,
    ) -> CapabilityRuntimeExecution | None: ...


class ContentAddressedCapabilityEffectStore:
    """Immutable local journal preventing a provider effect from being repeated."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve() / "claim-aware-runtime"
        self._root.mkdir(parents=True, exist_ok=True)

    def claim(
        self,
        plan: CapabilityExecutionPlan,
        claim: RuntimeExecutionClaim,
        effect_id: UUID,
    ) -> bool:
        record = {
            "schema": "captain.claim-aware-runtime-effect.v1",
            "effect_id": str(effect_id),
            "command_id": str(plan.command.event_id),
            "plan_sha256": canonical_contract_sha256(plan),
            "claim_sha256": canonical_contract_sha256(claim),
        }
        path = self._pending_path(effect_id)
        payload = _canonical_bytes(record)
        try:
            return self._write_once(path, payload)
        except ValueError:
            try:
                existing = json.loads(path.read_bytes())
            except (OSError, json.JSONDecodeError, TypeError):
                raise ValueError("durable runtime effect could not be verified") from None
            stable_fields = ("schema", "effect_id", "command_id", "plan_sha256")
            if any(existing.get(name) != record[name] for name in stable_fields):
                raise ValueError("durable runtime effect identity changed")
            return False

    def complete(
        self,
        execution: CapabilityRuntimeExecution,
    ) -> CapabilityRuntimeExecution:
        effect_id = execution.provider_receipt.effect_id
        pending = self._pending_path(effect_id)
        if not pending.is_file():
            raise ValueError("runtime effect completion has no durable claim")
        content = execution.model_dump_json(by_alias=True).encode("utf-8")
        self._write_once(self._completed_path(effect_id), content)
        return self.lookup(
            command_id=execution.result.command_id,
            effect_id=effect_id,
        ) or execution

    def lookup(
        self,
        *,
        command_id: UUID,
        effect_id: UUID,
    ) -> CapabilityRuntimeExecution | None:
        path = self._completed_path(effect_id)
        try:
            content = path.read_bytes()
            execution = CapabilityRuntimeExecution.model_validate_json(content)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise ValueError("durable runtime effect result is invalid") from None
        receipt = execution.provider_receipt
        if receipt.effect_id != effect_id or receipt.command_id != command_id:
            raise ValueError("durable runtime effect identity changed")
        if receipt.result_digest != canonical_contract_sha256(execution.result):
            raise ValueError("durable runtime effect result digest changed")
        return execution

    @staticmethod
    def _write_once(path: Path, content: bytes) -> bool:
        try:
            with path.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            return True
        except FileExistsError:
            try:
                existing = path.read_bytes()
            except OSError:
                raise ValueError("durable runtime effect could not be verified") from None
            if existing != content:
                raise ValueError("durable runtime effect identity changed")
            return False

    def _pending_path(self, effect_id: UUID) -> Path:
        return self._root / f"{effect_id}.pending.json"

    def _completed_path(self, effect_id: UUID) -> Path:
        return self._root / f"{effect_id}.completed.json"


class ClaimAwareCapabilityRuntime:
    """Package-C runtime that never repeats an already-claimed provider effect."""

    def __init__(
        self,
        *,
        executor: CodexExecutionPort,
        artifacts: ContentAddressedArtifactStore,
        effects: CapabilityEffectStorePort,
        clock: Callable[[], datetime],
    ) -> None:
        self._executor = executor
        self._artifacts = artifacts
        self._effects = effects
        self._clock = clock

    async def prepare(
        self,
        job: AgentFactoryJobV2,
        authority: CapabilityCatalogRecord,
    ) -> CapabilityExecutionPlan:
        _require_job_authority(job, authority)
        try:
            package_manifest = json.loads(
                self._artifacts.read_bytes(authority.package_ref).decode("utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("released capability package is unavailable in the shared CAS") from exc
        prompt_ref = self._artifacts.put(
            _canonical_bytes(
                {
                    "schema": "captain.capability-execution-prompt.v1",
                    "instruction": (
                        "Execute the released capability described by package_manifest. "
                        "The manifest below is the complete Captain authority; do not "
                        "attempt to resolve another package. Persist exactly one typed "
                        "captain.execution-outcome.v1 JSON artifact in the shared CAS "
                        "and reference it from the terminal runtime result. This is a "
                        "release-validation run, not a production business event: when "
                        "the package contains generated tests but no canonical business "
                        "payload, validate its declared artifacts and generated tests "
                        "using only redacted synthetic examples derived from the released "
                        "schemas and Real cases. Do not fail solely because a live claim, "
                        "customer, or other external production input is absent. Fail "
                        "closed for a missing package artifact, test failure, unresolved "
                        "required tool gap, or any attempt to perform a binding action."
                    ),
                    "correlation_id": str(job.correlation_id),
                    "factory_job_id": str(job.job_id),
                    "capability_id": authority.capability_id,
                    "capability_version": authority.capability_version,
                    "team_version": authority.team_version,
                    "package_ref": authority.package_ref.model_dump(mode="json"),
                    "package_manifest": package_manifest,
                    "accepted_assertion_ids": list(authority.accepted_assertion_ids),
                }
            ),
            "application/json",
            namespace="runtime-prompt",
        )
        command_id = uuid5(
            job.correlation_id,
            "|".join(
                (
                    "captain.claim-aware-capability-runtime.v1",
                    str(job.job_id),
                    authority.capability_id,
                    str(authority.capability_version),
                    str(authority.team_version),
                    str(authority.catalog_fence),
                    prompt_ref.sha256,
                )
            ),
        )
        admission_at = max(job.occurred_at, authority.published_at)
        if admission_at >= job.deadline_at:
            raise ValueError("capability publication exhausted the runtime authority window")
        command = AgentRuntimeCommand.model_validate(
            {
                "schema": "captain.agent-runtime-command.v1",
                "event_id": str(command_id),
                "correlation_id": str(job.correlation_id),
                "causation_id": str(authority.terminal_decision_id),
                "occurred_at": admission_at,
                "producer": "captain",
                "subject_id": "capability-execution",
                "subject_version": job.subject_version,
                "payload": {
                    "operation": RuntimeOperation.CODEX_RUN.value,
                    "project_id": "capability-factory",
                    "batch_id": capability_runtime_batch_id(job),
                    "subtask_id": "capability-execution",
                    "workspace_ref": (
                        f"workspace://capability-factory/{job.correlation_id}"
                    ),
                    "prompt_ref": prompt_ref.model_dump(mode="json"),
                    "integration_intent": IntegrationIntent.NONE.value,
                    "capability_profile": CapabilityProfile.CODE_BUILDER.value,
                    "limits": {
                        "wall_seconds": min(
                            3600,
                            max(1, int((job.deadline_at - job.occurred_at).total_seconds())),
                        ),
                        "max_iterations": 1,
                    },
                },
            }
        )
        grant = CapabilityGrant(
            schema_name="captain.capability-grant.v1",
            grant_id=f"capability-release-{command_id}",
            command_id=command.event_id,
            batch_id=capability_runtime_batch_id(job),
            batch_version=job.subject_version,
            subtask_id="capability-execution",
            workspace_ref=command.payload.workspace_ref or "",
            profile=CapabilityProfile.CODE_BUILDER,
            capabilities=tuple(sorted(PROFILE_CAPABILITIES[CapabilityProfile.CODE_BUILDER])),
            mcp_servers=(),
            issued_at=admission_at,
            expires_at=job.deadline_at,
        )
        return CapabilityExecutionPlan(
            command=command,
            grant=grant,
            claim_owner_id="capability-factory-runtime",
        )

    def guarantees_durable_idempotency(
        self,
        plan: CapabilityExecutionPlan,
        authority: CapabilityCatalogRecord,
    ) -> bool:
        return (
            plan.command.causation_id == authority.terminal_decision_id
            and plan.grant.command_id == plan.command.event_id
            and plan.command.payload.operation is RuntimeOperation.CODEX_RUN
        )

    async def lookup_effect(
        self,
        *,
        command_id: UUID,
        effect_id: UUID,
    ) -> CapabilityRuntimeExecution | None:
        return self._effects.lookup(command_id=command_id, effect_id=effect_id)

    async def execute(
        self,
        plan: CapabilityExecutionPlan,
        authority: CapabilityCatalogRecord,
        claim: RuntimeExecutionClaimReceipt,
        *,
        effect_id: UUID,
    ) -> CapabilityRuntimeExecution:
        now = _utc(self._clock())
        _require_plan_and_claim(plan, authority, claim, effect_id=effect_id, now=now)
        existing = self._effects.lookup(
            command_id=plan.command.event_id,
            effect_id=effect_id,
        )
        if existing is not None:
            return existing
        if not self._effects.claim(plan, claim.claim, effect_id):
            raise ClaimAwareRuntimeRecoveryRequired(
                "runtime provider effect is pending and cannot be repeated"
            )
        result = await self._executor.start(plan.command, plan.grant)
        _require_result(plan, claim.claim, result)
        outcome = await self.derive_outcome(plan, authority, result)
        if result.session_id is None:
            raise ValueError("runtime provider result lacks a durable session identity")
        execution = CapabilityRuntimeExecution(
            result=result,
            outcome=outcome,
            provider_receipt=ProviderEffectReceipt(
                provider_operation_id=f"codex-session:{result.session_id}",
                effect_id=effect_id,
                command_id=plan.command.event_id,
                origin_claim_id=claim.claim.claim_id,
                origin_claim_fencing_token=claim.claim.fencing_token,
                origin_claim_digest=canonical_contract_sha256(claim.claim),
                request_digest=canonical_contract_sha256(plan),
                result_digest=canonical_contract_sha256(result),
                status=result.status.value,
                idempotency_guaranteed=True,
            ),
        )
        return self._effects.complete(execution)

    async def derive_outcome(
        self,
        plan: CapabilityExecutionPlan,
        authority: CapabilityCatalogRecord,
        result: AgentRuntimeResult,
    ) -> ExecutionOutcomeV1:
        candidates: list[ExecutionOutcomeV1] = []
        for reference in result.artifact_refs:
            if reference.media_type != "application/json":
                continue
            try:
                content = self._artifacts.read_bytes(reference)
                candidate = ExecutionOutcomeV1.model_validate_json(content)
            except (OSError, ValueError):
                continue
            candidates.append(candidate)
        if len(candidates) != 1:
            raise ValueError("runtime result requires one typed execution outcome artifact")
        outcome = validate_execution_outcome_binding(
            candidates[0],
            command=plan.command,
            result=result,
            expected_capability_id=authority.capability_id,
            expected_capability_version=authority.capability_version,
            expected_team_version=authority.team_version,
        )
        if tuple(item.assertion_id for item in outcome.assertion_outcomes) != (
            authority.accepted_assertion_ids
        ):
            raise ValueError("execution outcome changed accepted assertion authority")
        available = set((*result.artifact_refs, *result.evidence_refs))
        referenced = {
            *outcome.evidence_refs,
            *(reference for item in outcome.assertion_outcomes for reference in item.evidence_refs),
        }
        if outcome.output_ref is not None:
            referenced.add(outcome.output_ref)
        if not referenced.issubset(available):
            raise ValueError("execution outcome references unavailable runtime evidence")
        return outcome


def _require_job_authority(
    job: AgentFactoryJobV2,
    authority: CapabilityCatalogRecord,
) -> None:
    if (
        authority.status != "ready_to_use"
        or authority.capability_id != job.required_capability
        or authority.accepted_assertion_ids != job.acceptance_assertion_ids
        or authority.promoted_capability.capability_id != authority.capability_id
        or authority.promoted_capability.version != authority.capability_version
    ):
        raise ValueError("runtime capability authority does not match the factory job")


def _require_plan_and_claim(
    plan: CapabilityExecutionPlan,
    authority: CapabilityCatalogRecord,
    receipt: RuntimeExecutionClaimReceipt,
    *,
    effect_id: UUID,
    now: datetime,
) -> None:
    command = plan.command
    grant = plan.grant
    claim = receipt.claim
    expected_effect_id = uuid5(command.event_id, "durable-provider-effect")
    if (
        receipt.claim_credential is None
        or authority.status != "ready_to_use"
        or command.causation_id != authority.terminal_decision_id
        or claim.status != "active"
        or claim.command_id != command.event_id
        or claim.owner_id != plan.claim_owner_id
        or effect_id != expected_effect_id
    ):
        raise ValueError("runtime execution claim does not match the plan")
    if (
        claim.capability_id != authority.capability_id
        or claim.capability_version != authority.capability_version
        or claim.team_version != authority.team_version
        or claim.catalog_fence != authority.catalog_fence
        or claim.package_ref != authority.package_ref
        or claim.published_at != authority.published_at
    ):
        raise ValueError("runtime execution claim changed capability authority")
    if (
        grant.command_id != command.event_id
        or grant.batch_id != command.payload.batch_id
        or grant.subtask_id != command.payload.subtask_id
        or grant.workspace_ref != command.payload.workspace_ref
        or grant.profile != command.payload.capability_profile
        or grant.capabilities
        != tuple(sorted(PROFILE_CAPABILITIES[CapabilityProfile.CODE_BUILDER]))
    ):
        raise ValueError("runtime execution grant does not match the command")
    if not claim.claimed_at <= now < claim.expires_at:
        raise ValueError("runtime execution claim is not active at provider dispatch")
    if not grant.issued_at <= now < grant.expires_at:
        raise ValueError("runtime execution grant is not active at provider dispatch")


def _require_result(
    plan: CapabilityExecutionPlan,
    claim: RuntimeExecutionClaim,
    result: AgentRuntimeResult,
) -> None:
    command = plan.command
    if (
        result.command_id != command.event_id
        or result.correlation_id != command.correlation_id
        or result.subject_id != command.subject_id
        or result.subject_version != command.subject_version
        or result.grant_id != plan.grant.grant_id
        or result.operation is not command.payload.operation
        or result.status in {RuntimeStatus.ACCEPTED, RuntimeStatus.RUNNING}
        or not claim.claimed_at <= result.occurred_at < claim.expires_at
    ):
        raise ValueError("runtime provider result changed claim or command authority")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("claim-aware runtime clock must be UTC")
    return value.astimezone(timezone.utc)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
