from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid5

import pytest

from agenten.agent_factory.capability_factory_entrypoint import CapabilityRuntimeExecution
from agenten.agent_factory.capability_live_adapters import ContentAddressedArtifactStore
from agenten.agent_factory.claim_aware_capability_runtime import (
    ClaimAwareCapabilityRuntime,
    ClaimAwareRuntimeRecoveryRequired,
    ContentAddressedCapabilityEffectStore,
)
from agenten.agent_factory.outcome_contracts import AssertionOutcome, ExecutionOutcomeV1
from agenten.agent_runtime.contracts import AgentRuntimeResult, RuntimeStatus
from gateway.capability_catalog import CapabilityCatalogRecord
from gateway.contracts import RuntimeExecutionClaim, RuntimeExecutionClaimReceipt
from tests.agent_factory.test_capability_resolution import capability, job


def authority() -> CapabilityCatalogRecord:
    factory_job = job()
    promoted = capability()
    return CapabilityCatalogRecord(
        capability_id=promoted.capability_id,
        capability_version=promoted.version,
        team_version=1,
        schema_major=1,
        package_ref=promoted.code_ref,
        release_authority_job_id=factory_job.job_id,
        terminal_decision_id=factory_job.event_id,
        promoted_capability=promoted,
        accepted_assertion_ids=factory_job.acceptance_assertion_ids,
        status="ready_to_use",
        catalog_fence=2,
        published_at=factory_job.occurred_at,
    )


def claim_for(plan, catalog: CapabilityCatalogRecord) -> RuntimeExecutionClaimReceipt:
    claimed_at = plan.command.occurred_at + timedelta(seconds=1)
    return RuntimeExecutionClaimReceipt(
        claim=RuntimeExecutionClaim(
            command_id=plan.command.event_id,
            claim_id=uuid5(plan.command.event_id, "claim"),
            owner_id=plan.claim_owner_id,
            fencing_token=3,
            claimed_at=claimed_at,
            expires_at=claimed_at + timedelta(minutes=2),
            status="active",
            capability_id=catalog.capability_id,
            capability_version=catalog.capability_version,
            team_version=catalog.team_version,
            catalog_fence=catalog.catalog_fence,
            catalog_block_index=10,
            catalog_block_hash="a" * 64,
            package_block_index=11,
            package_block_hash="b" * 64,
            package_ref=catalog.package_ref,
            published_at=catalog.published_at,
        ),
        replayed=False,
        recovered=False,
        claim_credential="claim-credential-value",
    )


class OutcomeProducingExecutor:
    def __init__(self, store: ContentAddressedArtifactStore, catalog: CapabilityCatalogRecord) -> None:
        self.store = store
        self.catalog = catalog
        self.calls = 0

    async def start(self, command, grant) -> AgentRuntimeResult:
        self.calls += 1
        result_id = uuid5(command.event_id, "provider-result")
        evidence = self.store.put(
            b'{"provider":"codex","status":"succeeded"}',
            "application/json",
            namespace="runtime-evidence",
        )
        outcome = ExecutionOutcomeV1(
            schema_name="captain.execution-outcome.v1",
            capability_id=self.catalog.capability_id,
            capability_version=self.catalog.capability_version,
            team_version=self.catalog.team_version,
            correlation_id=command.correlation_id,
            command_id=command.event_id,
            result_id=result_id,
            business_output={"status": "completed"},
            assertion_outcomes=tuple(
                AssertionOutcome(
                    assertion_id=assertion_id,
                    status="passed",
                    evidence_refs=(evidence,),
                )
                for assertion_id in self.catalog.accepted_assertion_ids
            ),
            tool_versions=("captain.runtime@1",),
            workflow_versions=("capability.factory@1",),
            evidence_refs=(evidence,),
            status="succeeded",
        )
        outcome_ref = self.store.put(
            outcome.model_dump_json(by_alias=True).encode("utf-8"),
            "application/json",
            namespace="execution-outcome",
        )
        return AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=result_id,
            command_id=command.event_id,
            correlation_id=command.correlation_id,
            occurred_at=command.occurred_at + timedelta(seconds=2),
            producer="agent-runtime",
            subject_id=command.subject_id,
            subject_version=command.subject_version,
            grant_id=grant.grant_id,
            operation=command.payload.operation,
            status=RuntimeStatus.SUCCEEDED,
            session_id="codex-provider-session",
            artifact_refs=(outcome_ref,),
            evidence_refs=(evidence,),
        )


class FailingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def start(self, command, grant):
        del command, grant
        self.calls += 1
        raise RuntimeError("provider connection dropped")


def runtime(tmp_path: Path, executor, *, now):
    artifacts = ContentAddressedArtifactStore(tmp_path)
    return ClaimAwareCapabilityRuntime(
        executor=executor,
        artifacts=artifacts,
        effects=ContentAddressedCapabilityEffectStore(tmp_path),
        clock=lambda: now,
    )


def test_prepare_is_deterministic_and_binds_authority_to_same_correlation(tmp_path: Path) -> None:
    factory_job = job()
    catalog = authority()
    adapter = runtime(tmp_path, FailingExecutor(), now=factory_job.occurred_at)

    first = asyncio.run(adapter.prepare(factory_job, catalog))
    second = asyncio.run(adapter.prepare(factory_job, catalog))

    assert first == second
    assert first.command.correlation_id == factory_job.correlation_id
    assert first.command.causation_id == catalog.terminal_decision_id
    assert first.grant.command_id == first.command.event_id
    assert first.grant.batch_version == factory_job.subject_version
    assert first.claim_owner_id == "capability-factory-runtime"


def test_prepare_never_predates_capability_publication(tmp_path: Path) -> None:
    factory_job = job()
    catalog = authority().model_copy(
        update={"published_at": factory_job.occurred_at + timedelta(seconds=1)}
    )
    adapter = runtime(tmp_path, FailingExecutor(), now=factory_job.occurred_at)
    package_ref = adapter._artifacts.put(  # noqa: SLF001 - prepares the sealed CAS fixture.
        b"{}", "application/json", namespace="capability-package"
    )
    catalog = catalog.model_copy(update={"package_ref": package_ref})

    plan = asyncio.run(adapter.prepare(factory_job, catalog))

    assert plan.command.occurred_at == catalog.published_at
    assert plan.grant.issued_at == catalog.published_at
    prompt = json.loads(adapter._artifacts.read_bytes(plan.command.payload.prompt_ref))  # noqa: SLF001
    assert "release-validation run" in prompt["instruction"]
    assert "Do not fail solely because a live claim" in prompt["instruction"]


def test_execute_rejects_foreign_claim_before_provider_call(tmp_path: Path) -> None:
    factory_job = job()
    catalog = authority()
    executor = FailingExecutor()
    adapter = runtime(tmp_path, executor, now=factory_job.occurred_at + timedelta(seconds=2))
    plan = asyncio.run(adapter.prepare(factory_job, catalog))
    receipt = claim_for(plan, catalog)
    foreign = receipt.model_copy(
        update={"claim": receipt.claim.model_copy(update={"command_id": factory_job.event_id})}
    )

    with pytest.raises(ValueError, match="claim"):
        asyncio.run(
            adapter.execute(
                plan,
                catalog,
                foreign,
                effect_id=uuid5(plan.command.event_id, "durable-provider-effect"),
            )
        )

    assert executor.calls == 0


def test_execute_rejects_changed_release_decision_before_provider_call(
    tmp_path: Path,
) -> None:
    factory_job = job()
    catalog = authority()
    executor = FailingExecutor()
    adapter = runtime(tmp_path, executor, now=factory_job.occurred_at + timedelta(seconds=2))
    plan = asyncio.run(adapter.prepare(factory_job, catalog))
    receipt = claim_for(plan, catalog)
    changed = catalog.model_copy(
        update={
            "terminal_decision_id": uuid5(
                catalog.terminal_decision_id,
                "replacement-decision",
            )
        }
    )

    with pytest.raises(ValueError, match="claim"):
        asyncio.run(
            adapter.execute(
                plan,
                changed,
                receipt,
                effect_id=uuid5(plan.command.event_id, "durable-provider-effect"),
            )
        )

    assert executor.calls == 0


def test_execute_persists_typed_outcome_and_reuses_without_second_provider_call(
    tmp_path: Path,
) -> None:
    factory_job = job()
    catalog = authority()
    artifacts = ContentAddressedArtifactStore(tmp_path)
    executor = OutcomeProducingExecutor(artifacts, catalog)
    adapter = ClaimAwareCapabilityRuntime(
        executor=executor,
        artifacts=artifacts,
        effects=ContentAddressedCapabilityEffectStore(tmp_path),
        clock=lambda: factory_job.occurred_at + timedelta(seconds=2),
    )
    plan = asyncio.run(adapter.prepare(factory_job, catalog))
    receipt = claim_for(plan, catalog)
    effect_id = uuid5(plan.command.event_id, "durable-provider-effect")

    first = asyncio.run(adapter.execute(plan, catalog, receipt, effect_id=effect_id))
    second = asyncio.run(adapter.execute(plan, catalog, receipt, effect_id=effect_id))

    assert isinstance(first, CapabilityRuntimeExecution)
    assert second == first
    assert executor.calls == 1
    assert first.result.correlation_id == factory_job.correlation_id
    assert first.provider_receipt.origin_claim_id == receipt.claim.claim_id
    assert first.provider_receipt.origin_claim_fencing_token == receipt.claim.fencing_token
    assert asyncio.run(
        adapter.lookup_effect(command_id=plan.command.event_id, effect_id=effect_id)
    ) == first


def test_pending_effect_fails_closed_instead_of_repeating_provider(tmp_path: Path) -> None:
    factory_job = job()
    catalog = authority()
    executor = FailingExecutor()
    adapter = runtime(
        tmp_path,
        executor,
        now=factory_job.occurred_at + timedelta(seconds=2),
    )
    plan = asyncio.run(adapter.prepare(factory_job, catalog))
    receipt = claim_for(plan, catalog)
    effect_id = uuid5(plan.command.event_id, "durable-provider-effect")

    with pytest.raises(RuntimeError, match="provider connection dropped"):
        asyncio.run(adapter.execute(plan, catalog, receipt, effect_id=effect_id))
    replacement = receipt.model_copy(
        update={
            "claim": receipt.claim.model_copy(
                update={
                    "claim_id": uuid5(plan.command.event_id, "replacement-claim"),
                    "fencing_token": receipt.claim.fencing_token + 1,
                }
            ),
            "claim_credential": "replacement-claim-credential",
        }
    )
    with pytest.raises(ClaimAwareRuntimeRecoveryRequired, match="pending"):
        asyncio.run(adapter.execute(plan, catalog, replacement, effect_id=effect_id))

    assert executor.calls == 1
