from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pydantic import ValidationError
from pymysql.err import OperationalError

from agenten.agent_factory.contracts import FactoryPhase, FactoryRole, PromotedCapability
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.outcome_contracts import (
    ExecutionOutcomeV1,
    FactoryTerminalState,
)
from agenten.agent_factory.release_gate import derive_terminal_decision
from agenten.agent_factory.state_machine import FactoryProjection, apply_block
from agenten.agent_runtime.contracts import CapabilityProfile
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    ProviderEffectReceipt,
)
from agenten.agent_runtime.capabilities import derive_grant
from agenten.validation.contracts import WorkBatch
from gateway.app import create_app
from gateway.auth import GatewayRole, require_actor
from gateway.capability_catalog import (
    CapabilityCatalogRecord,
    CapabilityCompatibilityRequest,
    GatewayCapabilityCatalog,
)
from gateway.contracts import (
    CapabilityExecutionRecord,
    CapabilityExecutionRequest,
    CapabilityReleaseRequest,
    CapabilityWriteReceipt,
    RuntimeBatchAdmission,
    RuntimeCapabilityAuthority,
    RuntimeExecutionClaim,
    RuntimeExecutionClaimRequest,
    RuntimeExecutionClaimReceipt,
    RuntimeResultRecoveryObservation,
    RuntimeResultRecoveryRequest,
    RuntimeReleasedBatchSnapshot,
    RuntimeWriteReceipt,
    canonical_contract_sha256,
)
from gateway.settings import GatewaySettings
from gateway.store import GatewayStore
from tests.agent_factory.test_release_gate import accepted_manifest, capability_e2e
from tests.agent_factory.test_state_machine import (
    NOW,
    accepted_evaluation,
    block,
    v2_job,
)


def _canonical_ref(model: object, name: str) -> ArtifactRef:
    payload = model.model_dump(mode="json", by_alias=True)  # type: ignore[attr-defined]
    content = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    return ArtifactRef(
        uri=f"artifact://gateway/{name}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def release_request() -> CapabilityReleaseRequest:
    factory_job = v2_job()
    evidence = capability_e2e()
    package = accepted_manifest(e2e=evidence)
    decision = derive_terminal_decision(
        factory_job,
        FactoryProjection.from_job(factory_job),
        package,
        accepted_evaluation(),
        evidence,
        NOW,
    )
    assert decision is not None
    autogen_ref = next(
        artifact.reference
        for artifact in package.artifacts
        if artifact.kind == "autogen_source"
    )
    return CapabilityReleaseRequest(
        schema_name="captain.capability-release-request.v1",
        event_id=UUID("00000000-0000-0000-0000-000000000710"),
        causation_id=factory_job.event_id,
        occurred_at=NOW,
        producer="captain",
        decision=decision,
        decision_ref=_canonical_ref(decision, "terminal-decision"),
        package=package,
        package_ref=_canonical_ref(package, "capability-package"),
        release_evidence=evidence,
        promoted_capability=PromotedCapability(
            capability_id=package.capability_id,
            version=package.capability_version,
            status="ready_to_use",
            blueprint_ref=package.team_manifest_ref,
            code_ref=autogen_ref,
            promotion_block_ref=ArtifactRef(
                uri="artifact://gateway/promotion/block-42",
                sha256="f" * 64,
                media_type="application/json",
            ),
        ),
        schema_major=1,
        team_version=1,
        accepted_assertion_ids=tuple(
            outcome.assertion_id for outcome in package.assertion_outcomes
        ),
        integration_intents=(),
        tool_contracts=(),
    )


def test_release_request_binds_canonical_digests_and_all_authority_identities() -> None:
    request = release_request()

    assert request.decision.job_id == request.package.factory_job_id
    assert request.decision.correlation_id == request.package.correlation_id
    assert request.promoted_capability.capability_id == request.package.capability_id

    payload = request.model_dump(mode="json", by_alias=True)
    payload["package_ref"]["sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="package_ref"):
        CapabilityReleaseRequest.model_validate(payload)

    payload = request.model_dump(mode="json", by_alias=True)
    payload["accepted_assertion_ids"] = ["foreign-assertion"]
    with pytest.raises(ValidationError, match="assertion"):
        CapabilityReleaseRequest.model_validate(payload)


def test_catalog_compatibility_is_exact_and_never_returns_private_package_data() -> None:
    record = CapabilityCatalogRecord.from_release(release_request(), catalog_fence=7)
    exact = CapabilityCompatibilityRequest(
        capability_id=record.capability_id,
        minimum_version=record.capability_version,
        schema_major=record.schema_major,
        accepted_assertion_ids=record.accepted_assertion_ids,
        integration_intents=record.integration_intents,
        tool_contracts=record.tool_contracts,
    )

    assert record.satisfies(exact)
    assert not record.satisfies(exact.model_copy(update={"schema_major": 2}))
    assert not record.satisfies(
        exact.model_copy(update={"accepted_assertion_ids": ("other",)})
    )
    assert not record.satisfies(
        exact.model_copy(update={"integration_intents": ("n8n",)})
    )
    assert not record.satisfies(
        exact.model_copy(update={"tool_contracts": ("mcp.n8n@1",)})
    )
    assert not record.model_copy(update={"status": "revoked"}).satisfies(exact)

    rendered = record.model_dump_json().lower()
    assert "private_holdout" not in rendered
    assert "release_evidence" not in rendered
    assert "artifacts" not in rendered


class _CatalogRepository:
    def __init__(self, record: CapabilityCatalogRecord | None) -> None:
        self.record = record

    def find_ready_capability(self, capability_id: str) -> CapabilityCatalogRecord | None:
        if self.record is None or self.record.capability_id != capability_id:
            return None
        return self.record


def test_gateway_catalog_adapter_fails_closed_for_stale_or_unexpressed_requirements() -> None:
    request = release_request()
    record = CapabilityCatalogRecord.from_release(request, catalog_fence=1)
    factory_job = v2_job()

    assert (
        GatewayCapabilityCatalog(_CatalogRepository(record)).compatible_capability(
            factory_job
        )
        == record.promoted_capability
    )
    assert (
        GatewayCapabilityCatalog(
            _CatalogRepository(
                record.model_copy(
                    update={"capability_version": factory_job.subject_version - 1}
                )
            )
        ).compatible_capability(factory_job)
        is None
    )
    assert (
        GatewayCapabilityCatalog(
            _CatalogRepository(
                record.model_copy(update={"tool_contracts": ("mcp.n8n@1",)})
            )
        ).compatible_capability(factory_job)
        is None
    )


def test_execution_claim_request_has_bounded_utc_lease_and_owner_identity() -> None:
    release = release_request()
    claim = RuntimeExecutionClaimRequest(
        schema_name="captain.runtime-execution-claim-request.v1",
        command_id=UUID("00000000-0000-0000-0000-000000000001"),
        owner_id="runtime-host-01",
        lease_seconds=120,
        capability_id=release.package.capability_id,
        capability_version=release.package.capability_version,
    )

    assert claim.owner_id == "runtime-host-01"
    with pytest.raises(ValidationError, match="lease"):
        RuntimeExecutionClaimRequest.model_validate(
            {
                **claim.model_dump(mode="json", by_alias=True),
                "lease_seconds": 3600,
            }
        )


def _runtime_result_recovery_request() -> RuntimeResultRecoveryRequest:
    result = _runtime_result()
    authority = _runtime_capability_authority()
    origin_claim = RuntimeExecutionClaim(
        **authority.model_dump(),
        command_id=result.command_id,
        claim_id=UUID("00000000-0000-0000-0000-000000000811"),
        owner_id="runtime-a",
        fencing_token=1,
        claimed_at=result.occurred_at - timedelta(seconds=30),
        expires_at=result.occurred_at + timedelta(seconds=30),
        status="active",
    )
    effect_id = uuid5(result.command_id, "durable-provider-effect")
    receipt = ProviderEffectReceipt(
        provider_operation_id=f"provider-operation:{effect_id}",
        effect_id=effect_id,
        command_id=result.command_id,
        origin_claim_id=origin_claim.claim_id,
        origin_claim_fencing_token=origin_claim.fencing_token,
        origin_claim_digest=canonical_contract_sha256(origin_claim),
        request_digest="a" * 64,
        result_digest=canonical_contract_sha256(result),
        status=result.status.value,
        idempotency_guaranteed=True,
    )
    observation = RuntimeResultRecoveryObservation(
        schema_name="captain.runtime-result-recovery-observation.v1",
        event_id=uuid5(result.event_id, "runtime-result-recovery:1:2"),
        observed_at=origin_claim.expires_at,
        command_id=result.command_id,
        original_result_id=result.event_id,
        original_result_digest=canonical_contract_sha256(result),
        original_claim_id=origin_claim.claim_id,
        original_claim_digest=canonical_contract_sha256(origin_claim),
        provider_effect_id=effect_id,
        provider_receipt_digest=canonical_contract_sha256(receipt),
        original_claim_fence=1,
        recovery_claim_fence=2,
        correlation_id=result.correlation_id,
        causation_id=result.event_id,
    )
    return RuntimeResultRecoveryRequest(
        schema_name="captain.runtime-result-recovery-request.v1",
        result=result,
        provider_receipt=receipt,
        observation=observation,
    )


def test_runtime_result_recovery_request_binds_immutable_result_and_provider_receipt() -> None:
    request = _runtime_result_recovery_request()
    result = request.result
    observation = request.observation
    effect_id = request.provider_receipt.effect_id

    assert request.result.model_dump_json(by_alias=True) == result.model_dump_json(
        by_alias=True
    )
    payload = request.model_dump(mode="json", by_alias=True)
    payload["observation"]["original_result_digest"] = "0" * 64
    with pytest.raises(ValidationError, match="result digest"):
        RuntimeResultRecoveryRequest.model_validate(payload)

    payload = request.model_dump(mode="json", by_alias=True)
    payload["observation"]["provider_effect_id"] = str(uuid5(effect_id, "changed"))
    with pytest.raises(ValidationError, match="provider effect"):
        RuntimeResultRecoveryRequest.model_validate(payload)

    payload = request.model_dump(mode="json", by_alias=True)
    payload["observation"]["original_claim_digest"] = "0" * 64
    with pytest.raises(ValidationError, match="effect origin claim"):
        RuntimeResultRecoveryRequest.model_validate(payload)

    payload = observation.model_dump(mode="json", by_alias=True)
    payload["event_id"] = str(result.event_id)
    with pytest.raises(ValidationError, match="distinct event id"):
        RuntimeResultRecoveryObservation.model_validate(payload)


def _runtime_command() -> AgentRuntimeCommand:
    fixture = (
        __import__("pathlib").Path(__file__).parents[1]
        / "fixtures"
        / "contracts"
        / "agent_runtime_command.v1.json"
    )
    return AgentRuntimeCommand.model_validate_json(fixture.read_text(encoding="utf-8"))


def _runtime_result() -> AgentRuntimeResult:
    fixture = (
        __import__("pathlib").Path(__file__).parents[1]
        / "fixtures"
        / "contracts"
        / "agent_runtime_result.v1.json"
    )
    return AgentRuntimeResult.model_validate_json(fixture.read_text(encoding="utf-8"))


def _released_batch() -> WorkBatch:
    command = _runtime_command()
    return WorkBatch(
        batch_id="batch-1",
        title="Released runtime work",
        goal="Execute the admitted command.",
        subtask_ids=["subtask-1"],
        target="python",
        capability_tags=[command.payload.capability_profile.value],
        acceptance_criteria=[
            {
                "assertion_id": "runtime-result",
                "kind": "status_equals",
                "expected": "succeeded",
            }
        ],
    )


def _runtime_capability_authority() -> RuntimeCapabilityAuthority:
    release = release_request()
    catalog = CapabilityCatalogRecord.from_release(release, catalog_fence=7)
    return RuntimeCapabilityAuthority(
        capability_id=catalog.capability_id,
        capability_version=catalog.capability_version,
        team_version=catalog.team_version,
        catalog_fence=catalog.catalog_fence,
        catalog_block_index=31,
        catalog_block_hash="c" * 64,
        package_block_index=30,
        package_block_hash="b" * 64,
        package_ref=catalog.package_ref,
        published_at=catalog.published_at,
    )


def test_runtime_batch_admission_binds_the_exact_release_head() -> None:
    command = _runtime_command()
    admission = RuntimeBatchAdmission(
        command_id=command.event_id,
        batch_id="batch-1",
        batch_version=command.subject_version,
        batch_block_index=11,
        batch_block_hash="a" * 64,
        release_fence=12,
        admitted_at=NOW,
    )
    snapshot = RuntimeReleasedBatchSnapshot(
        admission=admission,
        batch=_released_batch(),
    )

    assert snapshot.admission.batch_block_hash == "a" * 64
    with pytest.raises(ValidationError, match="batch"):
        RuntimeReleasedBatchSnapshot.model_validate(
            {
                "admission": admission.model_dump(mode="json"),
                "batch": _released_batch().model_copy(
                    update={"batch_id": "batch-other"}
                ).model_dump(mode="json"),
            }
        )


def test_gateway_admission_and_grant_use_one_immutable_batch_head() -> None:
    command = _runtime_command()
    batch = _released_batch()
    parent = {
        "index": 11,
        "hash": "a" * 64,
        "data": batch.model_dump(mode="json"),
    }
    admission = GatewayStore._build_runtime_batch_admission(
        command=command,
        parent=parent,
        release_fence=12,
        admitted_at=NOW,
    )
    grant = derive_grant(command, batch, NOW)

    GatewayStore._assert_runtime_admission_head(
        admission,
        command=command,
        parent=parent,
    )
    GatewayStore._assert_grant_matches_admission(grant, admission)
    with pytest.raises(HTTPException, match="release head"):
        GatewayStore._assert_runtime_admission_head(
            admission,
            command=command,
            parent={**parent, "hash": "b" * 64},
        )
    children = [{"index": 12, "block_type": "batch_approved"}]
    GatewayStore._assert_runtime_admission_is_current(
        admission,
        parent=parent,
        children=children,
        projection=SimpleNamespace(status="pending"),
    )
    with pytest.raises(HTTPException, match="currently released"):
        GatewayStore._assert_runtime_admission_is_current(
            admission,
            parent=parent,
            children=[*children, {"index": 13, "block_type": "batch_approved"}],
            projection=SimpleNamespace(status="pending"),
        )
    with pytest.raises(HTTPException, match="currently released"):
        GatewayStore._assert_runtime_admission_is_current(
            admission,
            parent=parent,
            children=children,
            projection=SimpleNamespace(status="done"),
        )


def test_gateway_recomputes_ready_release_and_requires_prior_promotion() -> None:
    request = release_request()
    factory_job = v2_job()
    promotion = block(
        FactoryPhase.CAPABILITY_PROMOTED,
        assertions=factory_job.acceptance_assertion_ids,
    )

    GatewayStore._authoritative_capability_release(
        job=factory_job,
        projection=FactoryProjection.from_job(factory_job),
        promotion=promotion,
        evaluation=accepted_evaluation(),
        request=request,
        now=request.decision.decided_at,
    )
    with pytest.raises(HTTPException, match="promotion"):
        GatewayStore._authoritative_capability_release(
            job=factory_job,
            projection=FactoryProjection.from_job(factory_job),
            promotion=None,
            evaluation=accepted_evaluation(),
            request=request,
            now=request.decision.decided_at,
        )


def test_gateway_derives_release_clock_team_tools_and_promotion_authority() -> None:
    request = release_request()
    factory_job = v2_job()
    promotion = block(
        FactoryPhase.CAPABILITY_PROMOTED,
        assertions=factory_job.acceptance_assertion_ids,
    )
    submitted = request.model_copy(
        update={
            "team_version": 99,
            "tool_contracts": ("caller-invented",),
            "promoted_capability": request.promoted_capability.model_copy(
                update={
                    "promotion_block_ref": ArtifactRef(
                        uri="artifact://caller/invented-promotion",
                        sha256="0" * 64,
                        media_type="application/json",
                    )
                }
            ),
        }
    )
    gateway_now = NOW + timedelta(seconds=30)

    authoritative = GatewayStore._authoritative_capability_release(
        job=factory_job,
        projection=FactoryProjection.from_job(factory_job),
        promotion=promotion,
        evaluation=accepted_evaluation(),
        request=submitted,
        now=gateway_now,
    )

    assert authoritative.occurred_at == gateway_now
    assert authoritative.decision.decided_at == gateway_now
    assert authoritative.team_version == authoritative.package.capability_version
    assert authoritative.tool_contracts == ()
    assert authoritative.promoted_capability.promotion_block_ref == ArtifactRef(
        uri=f"artifact://gateway/factory-promotion/{promotion.event_id}",
        sha256=canonical_contract_sha256(promotion),
        media_type="application/json",
    )


def test_gateway_release_uses_stored_retry_projection() -> None:
    request = release_request()
    factory_job = v2_job()
    promotion = block(
        FactoryPhase.CAPABILITY_PROMOTED,
        assertions=factory_job.acceptance_assertion_ids,
    )
    retried_projection = FactoryProjection.from_job(factory_job).model_copy(
        update={"attempt": 2}
    )

    with pytest.raises(HTTPException, match="ready_to_use"):
        GatewayStore._authoritative_capability_release(
            job=factory_job,
            projection=retried_projection,
            promotion=promotion,
            evaluation=accepted_evaluation(),
            request=request,
            now=NOW,
        )


def test_gateway_release_uses_gateway_clock_for_deadline() -> None:
    request = release_request()
    factory_job = v2_job()
    promotion = block(
        FactoryPhase.CAPABILITY_PROMOTED,
        assertions=factory_job.acceptance_assertion_ids,
    )

    with pytest.raises(HTTPException, match="ready_to_use"):
        GatewayStore._authoritative_capability_release(
            job=factory_job,
            projection=FactoryProjection.from_job(factory_job),
            promotion=promotion,
            evaluation=accepted_evaluation(),
            request=request,
            now=factory_job.deadline_at,
        )
    with pytest.raises(HTTPException, match="factory job"):
        GatewayStore._authoritative_capability_release(
            job=factory_job.model_copy(update={"job_id": UUID(int=999)}),
            projection=FactoryProjection.from_job(factory_job),
            promotion=promotion,
            evaluation=accepted_evaluation(),
            request=request,
            now=request.decision.decided_at,
        )


def test_gateway_v2_lease_decision_uses_authoritative_clock() -> None:
    factory_job = v2_job()
    projection = apply_block(
        FactoryProjection.from_job(factory_job),
        block(FactoryPhase.FORGE_REQUESTED),
        now=NOW,
    )
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/architect",
        now=NOW,
    )
    assert lease.capability_profile is CapabilityProfile.FACTORY_ARCHITECT

    GatewayStore._assert_lease_is_next_action(
        lease,
        projection,
        now=factory_job.deadline_at - timedelta(microseconds=1),
    )
    with pytest.raises(HTTPException, match="next authorized"):
        GatewayStore._assert_lease_is_next_action(
            lease,
            projection,
            now=factory_job.deadline_at,
        )


def test_release_write_retry_is_bounded_and_recovers_transient_conflicts() -> None:
    attempts = 0

    def succeeds_on_third_attempt() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OperationalError(1213, "deterministic deadlock")
        return "published"

    assert GatewayStore._retry_write(succeeds_on_third_attempt) == "published"
    assert attempts == 3

    attempts = 0

    def never_succeeds() -> str:
        nonlocal attempts
        attempts += 1
        raise OperationalError(1213, "deterministic deadlock")

    with pytest.raises(OperationalError):
        GatewayStore._retry_write(never_succeeds)
    assert attempts == 3


def test_terminal_decision_public_write_retries_its_once_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GatewayStore.__new__(GatewayStore)
    decision = release_request().decision.model_copy(
        update={
            "state": FactoryTerminalState.BLOCKED,
            "reasons": ("package_validation_blocked",),
            "evidence_refs": (),
        }
    )
    attempts = 0

    def transient_then_replay(_decision: object) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OperationalError(1213, "deterministic terminal race")
        return True

    monkeypatch.setattr(
        store,
        "_record_factory_terminal_decision_once",
        transient_then_replay,
        raising=False,
    )
    receipt = store.record_factory_terminal_decision(decision)

    assert attempts == 3
    assert receipt.replayed is True


def test_runtime_claim_public_write_retries_its_once_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GatewayStore.__new__(GatewayStore)
    authority = _runtime_capability_authority()
    request = RuntimeExecutionClaimRequest(
        schema_name="captain.runtime-execution-claim-request.v1",
        command_id=_runtime_command().event_id,
        owner_id="runtime-a",
        lease_seconds=60,
        capability_id=authority.capability_id,
        capability_version=authority.capability_version,
    )
    expected = object()
    attempts = 0

    def transient_then_claim(_request: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OperationalError(1213, "deterministic claim/result race")
        return expected

    monkeypatch.setattr(
        store,
        "_claim_runtime_execution_once",
        transient_then_claim,
        raising=False,
    )

    assert store.claim_runtime_execution(request) is expected
    assert attempts == 3


def test_execution_claim_policy_is_restart_safe_owner_bound_and_fenced() -> None:
    authority = _runtime_capability_authority()
    first_request = RuntimeExecutionClaimRequest(
        schema_name="captain.runtime-execution-claim-request.v1",
        command_id=_runtime_command().event_id,
        owner_id="runtime-a",
        lease_seconds=60,
        capability_id=authority.capability_id,
        capability_version=authority.capability_version,
    )
    first = GatewayStore._resolve_runtime_execution_claim(
        existing=None,
        request=first_request,
        now=NOW,
        result_recorded=False,
        credential_factory=lambda: "first-one-time-credential",
        authority=authority,
    )
    replay = GatewayStore._resolve_runtime_execution_claim(
        existing=first.claim,
        request=first_request,
        now=NOW + timedelta(seconds=1),
        result_recorded=False,
        credential_factory=lambda: "must-not-be-issued-on-replay",
        authority=authority,
    )

    assert first.claim.fencing_token == 1
    assert first.claim_credential == "first-one-time-credential"
    assert replay.replayed is True
    assert replay.claim_credential is None
    assert "credential" not in first.claim.model_dump(mode="json")
    with pytest.raises(HTTPException, match="owned"):
        GatewayStore._resolve_runtime_execution_claim(
            existing=first.claim,
            request=first_request.model_copy(update={"owner_id": "runtime-b"}),
            now=NOW + timedelta(seconds=1),
            result_recorded=False,
            credential_factory=lambda: "unused",
            authority=authority,
        )

    recovered = GatewayStore._resolve_runtime_execution_claim(
        existing=first.claim,
        request=first_request.model_copy(update={"owner_id": "runtime-b"}),
        now=NOW + timedelta(seconds=61),
        result_recorded=False,
        credential_factory=lambda: "recovery-one-time-credential",
        authority=authority,
    )
    assert recovered.claim.fencing_token == 2
    assert recovered.recovered is True
    assert recovered.claim_credential == "recovery-one-time-credential"
    with pytest.raises(HTTPException, match="stale"):
        GatewayStore._assert_execution_claim_fence(
            recovered.claim,
            owner_id="runtime-a",
            fencing_token=1,
        )
    with pytest.raises(HTTPException, match="expired"):
        GatewayStore._assert_execution_claim_fence(
            first.claim,
            owner_id="runtime-a",
            fencing_token=1,
            now=NOW + timedelta(seconds=61),
        )


def test_execution_claim_recovery_keeps_its_frozen_catalog_after_head_upgrade() -> None:
    authority = _runtime_capability_authority()
    request = RuntimeExecutionClaimRequest(
        schema_name="captain.runtime-execution-claim-request.v1",
        command_id=_runtime_command().event_id,
        owner_id="runtime-a",
        lease_seconds=60,
        capability_id=authority.capability_id,
        capability_version=authority.capability_version,
    )
    first = GatewayStore._resolve_runtime_execution_claim(
        existing=None,
        request=request,
        now=NOW,
        result_recorded=False,
        credential_factory=lambda: "first-one-time-credential",
        authority=authority,
    )
    upgraded_head = authority.model_copy(
        update={
            "capability_version": authority.capability_version + 1,
            "catalog_fence": authority.catalog_fence + 1,
            "catalog_block_index": authority.catalog_block_index + 2,
            "catalog_block_hash": "d" * 64,
            "package_block_index": authority.package_block_index + 2,
            "package_block_hash": "e" * 64,
        }
    )

    recovered = GatewayStore._resolve_runtime_execution_claim(
        existing=first.claim,
        request=request.model_copy(update={"owner_id": "runtime-b"}),
        now=NOW + timedelta(seconds=61),
        result_recorded=False,
        credential_factory=lambda: "recovery-one-time-credential",
        authority=upgraded_head,
    )

    assert GatewayStore._claim_capability_authority(recovered.claim) == authority
    assert recovered.claim.fencing_token == 2
    assert recovered.recovered is True


def test_execution_completion_requires_one_time_credential_and_fence() -> None:
    authority = _runtime_capability_authority()
    request = RuntimeExecutionClaimRequest(
        schema_name="captain.runtime-execution-claim-request.v1",
        command_id=_runtime_command().event_id,
        owner_id="runtime-a",
        lease_seconds=60,
        capability_id=authority.capability_id,
        capability_version=authority.capability_version,
    )
    receipt = GatewayStore._resolve_runtime_execution_claim(
        existing=None,
        request=request,
        now=NOW,
        result_recorded=False,
        credential_factory=lambda: "only-the-acquirer-knows-this",
        authority=authority,
    )
    assert receipt.claim_credential is not None
    credential_sha256 = hashlib.sha256(
        receipt.claim_credential.encode("utf-8")
    ).hexdigest()

    GatewayStore._assert_execution_claim_completion_authority(
        receipt.claim,
        owner_id=request.owner_id,
        fencing_token=receipt.claim.fencing_token,
        credential=receipt.claim_credential,
        credential_sha256=credential_sha256,
    )
    with pytest.raises(HTTPException, match="stale"):
        GatewayStore._assert_execution_claim_completion_authority(
            receipt.claim,
            owner_id=request.owner_id,
            fencing_token=receipt.claim.fencing_token,
            credential="shared-worker-token-is-not-the-claim-secret",
            credential_sha256=credential_sha256,
        )
    with pytest.raises(HTTPException, match="stale"):
        GatewayStore._assert_execution_claim_completion_authority(
            receipt.claim,
            owner_id=request.owner_id,
            fencing_token=receipt.claim.fencing_token + 1,
            credential=receipt.claim_credential,
            credential_sha256=credential_sha256,
        )


def test_result_time_must_fall_inside_the_execution_claim_lease() -> None:
    authority = _runtime_capability_authority()
    claim = RuntimeExecutionClaim(
        **authority.model_dump(),
        command_id=_runtime_command().event_id,
        claim_id=UUID("00000000-0000-0000-0000-000000000801"),
        owner_id="runtime-a",
        fencing_token=1,
        claimed_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
        status="active",
    )

    GatewayStore._assert_result_occurred_within_claim(
        claim,
        occurred_at=NOW,
    )
    with pytest.raises(HTTPException, match="lease window"):
        GatewayStore._assert_result_occurred_within_claim(
            claim,
            occurred_at=NOW - timedelta(microseconds=1),
        )


def test_runtime_result_recovery_requires_historical_claim_and_distinct_current_lease() -> None:
    request = _runtime_result_recovery_request()
    authority = _runtime_capability_authority()
    original_claim = RuntimeExecutionClaim(
        **authority.model_dump(),
        command_id=request.result.command_id,
        claim_id=UUID("00000000-0000-0000-0000-000000000811"),
        owner_id="runtime-a",
        fencing_token=1,
        claimed_at=request.result.occurred_at - timedelta(seconds=30),
        expires_at=request.result.occurred_at + timedelta(seconds=30),
        status="active",
    )
    current_claim = RuntimeExecutionClaim(
        **authority.model_dump(),
        command_id=request.result.command_id,
        claim_id=UUID("00000000-0000-0000-0000-000000000812"),
        owner_id="runtime-b",
        fencing_token=2,
        claimed_at=original_claim.expires_at,
        expires_at=original_claim.expires_at + timedelta(minutes=1),
        status="active",
    )
    request = RuntimeResultRecoveryRequest.model_validate(
        {
            **request.model_dump(mode="json", by_alias=True),
            "observation": {
                **request.observation.model_dump(mode="json", by_alias=True),
                "observed_at": current_claim.claimed_at.isoformat(),
            },
        }
    )

    GatewayStore._assert_runtime_result_recovery_claims(
        request,
        original_claim=original_claim,
        current_claim=current_claim,
        command_block_index=71,
        original_claim_parent_index=71,
    )
    with pytest.raises(HTTPException, match="lineage"):
        GatewayStore._assert_runtime_result_recovery_claims(
            request,
            original_claim=original_claim,
            current_claim=current_claim,
            command_block_index=71,
            original_claim_parent_index=72,
        )
    with pytest.raises(HTTPException, match="frozen capability"):
        GatewayStore._assert_runtime_result_recovery_claims(
            request,
            original_claim=original_claim,
            current_claim=current_claim.model_copy(
                update={"catalog_fence": current_claim.catalog_fence + 1}
            ),
            command_block_index=71,
            original_claim_parent_index=71,
        )


def test_runtime_claim_rejects_prepublish_commands_and_freezes_exact_catalog() -> None:
    authority = _runtime_capability_authority()
    release = release_request()
    catalog = CapabilityCatalogRecord.from_release(
        release,
        catalog_fence=authority.catalog_fence,
    )
    request = RuntimeExecutionClaimRequest(
        schema_name="captain.runtime-execution-claim-request.v1",
        command_id=_runtime_command().event_id,
        owner_id="runtime-a",
        lease_seconds=60,
        capability_id=authority.capability_id,
        capability_version=authority.capability_version,
    )
    command = _runtime_command().model_copy(
        update={"occurred_at": catalog.published_at + timedelta(seconds=1)}
    )
    admission = RuntimeBatchAdmission(
        command_id=command.event_id,
        batch_id="batch-1",
        batch_version=command.subject_version,
        batch_block_index=11,
        batch_block_hash="a" * 64,
        release_fence=12,
        admitted_at=catalog.published_at + timedelta(seconds=1),
    )

    GatewayStore._assert_runtime_capability_claimable(
        request=request,
        command=command,
        admission=admission,
        catalog=catalog,
    )
    with pytest.raises(HTTPException, match="predates"):
        GatewayStore._assert_runtime_capability_claimable(
            request=request,
            command=command.model_copy(
                update={"occurred_at": catalog.published_at - timedelta(microseconds=1)}
            ),
            admission=admission,
            catalog=catalog,
        )
    with pytest.raises(HTTPException, match="predates"):
        GatewayStore._assert_runtime_capability_claimable(
            request=request,
            command=command,
            admission=admission.model_copy(
                update={
                    "admitted_at": catalog.published_at - timedelta(microseconds=1)
                }
            ),
            catalog=catalog,
        )

    claim = GatewayStore._resolve_runtime_execution_claim(
        existing=None,
        request=request,
        now=catalog.published_at + timedelta(seconds=2),
        result_recorded=False,
        authority=authority,
        credential_factory=lambda: "catalog-bound-one-time-credential",
    ).claim
    assert claim.capability_id == catalog.capability_id
    assert claim.capability_version == catalog.capability_version
    assert claim.catalog_fence == catalog.catalog_fence
    assert claim.package_block_hash == authority.package_block_hash
    with pytest.raises(HTTPException, match="frozen"):
        GatewayStore._assert_frozen_capability_authority(
            claim,
            authority=authority.model_copy(
                update={"catalog_fence": authority.catalog_fence + 1}
            ),
        )
    with pytest.raises(HTTPException, match="lease window"):
        GatewayStore._assert_result_occurred_within_claim(
            claim,
            occurred_at=claim.expires_at,
        )


def capability_execution_request() -> CapabilityExecutionRequest:
    release = release_request()
    command = _runtime_command()
    result = _runtime_result()
    payload = json.loads(
        (
            __import__("pathlib").Path(__file__).parents[1]
            / "fixtures"
            / "contracts"
            / "execution_outcome.v1.json"
        ).read_text(encoding="utf-8")
    )
    payload.update(
        {
            "capability_id": release.package.capability_id,
            "capability_version": release.package.capability_version,
            "team_version": release.team_version,
            "correlation_id": str(command.correlation_id),
            "command_id": str(command.event_id),
            "result_id": str(result.event_id),
            "assertion_outcomes": [
                outcome.model_dump(mode="json")
                for outcome in release.package.assertion_outcomes
            ],
        }
    )
    outcome = ExecutionOutcomeV1.model_validate(payload)
    return CapabilityExecutionRequest(
        schema_name="captain.capability-execution-request.v1",
        event_id=UUID("00000000-0000-0000-0000-000000000711"),
        causation_id=result.event_id,
        occurred_at=result.occurred_at,
        producer="captain",
        outcome=outcome,
        outcome_ref=_canonical_ref(outcome, "execution-outcome"),
        claim_owner_id="runtime-a",
        claim_fencing_token=1,
    )


def test_execution_outcome_is_bound_to_catalog_command_result_and_claim() -> None:
    request = capability_execution_request()
    catalog = CapabilityCatalogRecord.from_release(release_request(), catalog_fence=1)
    authority = _runtime_capability_authority().model_copy(
        update={
            "capability_id": catalog.capability_id,
            "capability_version": catalog.capability_version,
            "team_version": catalog.team_version,
            "catalog_fence": catalog.catalog_fence,
            "package_ref": catalog.package_ref,
            "published_at": catalog.published_at,
        }
    )
    claim = RuntimeExecutionClaim(
        **authority.model_dump(),
        command_id=request.outcome.command_id,
        claim_id=UUID("00000000-0000-0000-0000-000000000801"),
        owner_id=request.claim_owner_id,
        fencing_token=request.claim_fencing_token,
        claimed_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
        status="active",
    )

    GatewayStore._assert_capability_execution_binding(
        request,
        catalog=catalog,
        command=_runtime_command(),
        result=_runtime_result(),
        claim=claim,
    )
    with pytest.raises(HTTPException, match="stale"):
        GatewayStore._assert_capability_execution_binding(
            request.model_copy(update={"claim_fencing_token": 2}),
            catalog=catalog,
            command=_runtime_command(),
            result=_runtime_result(),
            claim=claim,
        )
    with pytest.raises(HTTPException, match="frozen"):
        GatewayStore._assert_capability_execution_binding(
            request,
            catalog=catalog,
            command=_runtime_command(),
            result=_runtime_result(),
            claim=claim.model_copy(
                update={"catalog_fence": claim.catalog_fence + 1}
            ),
        )


def test_gateway_store_exposes_durable_capability_execution_round_trip() -> None:
    assert callable(GatewayStore.record_capability_execution)
    assert callable(GatewayStore.capability_execution)


class _AuthorityStore:
    def __init__(self) -> None:
        self.release = release_request()
        self.catalog = CapabilityCatalogRecord.from_release(
            self.release,
            catalog_fence=1,
        )
        self.execution_request = capability_execution_request()
        self.execution = CapabilityExecutionRecord.from_request(
            self.execution_request,
            catalog_fence=1,
        )
        self.recovery_request = _runtime_result_recovery_request()
        self.release_calls = 0
        self.recovery_calls = 0
        self.nonrelease_terminal = self.release.decision.model_copy(
            update={
                "decision_id": UUID("00000000-0000-0000-0000-000000000799"),
                "job_id": UUID("00000000-0000-0000-0000-000000000798"),
                "state": FactoryTerminalState.BLOCKED,
                "reasons": ("package_validation_blocked",),
                "evidence_refs": (),
            }
        )

    def publish_capability_release(self, request: CapabilityReleaseRequest):
        assert request == self.release
        self.release_calls += 1
        return CapabilityWriteReceipt(
            record_id=f"{request.package.capability_id}:{request.package.capability_version}",
            replayed=self.release_calls > 1,
        )

    def factory_terminal_decision(self, job_id):
        if job_id == self.release.decision.job_id:
            return self.release.decision
        if job_id == self.nonrelease_terminal.job_id:
            return self.nonrelease_terminal
        return None

    def record_factory_terminal_decision(self, decision):
        assert decision == self.nonrelease_terminal
        return CapabilityWriteReceipt(
            record_id=str(decision.decision_id),
            replayed=False,
        )

    def capability(self, capability_id, version=None):
        del version
        return self.catalog if capability_id == self.catalog.capability_id else None

    def compatible_capability(self, request):
        return self.catalog if self.catalog.satisfies(request) else None

    def record_capability_execution(self, request):
        assert request == self.execution_request
        return CapabilityWriteReceipt(
            record_id=str(request.outcome.command_id),
            replayed=False,
        )

    def capability_execution(self, command_id):
        return self.execution if command_id == self.execution.command_id else None

    def recover_runtime_result(
        self,
        request,
        *,
        execution_owner_id,
        execution_fencing_token,
        execution_claim_credential,
    ):
        assert request == self.recovery_request
        assert execution_owner_id == "runtime-a"
        assert execution_fencing_token == 2
        assert execution_claim_credential == "one-time-recovery-credential"
        self.recovery_calls += 1
        return RuntimeWriteReceipt(
            operation_id=request.result.command_id,
            replayed=self.recovery_calls > 1,
        )

    def runtime_result_recovery(self, command_id):
        if command_id != self.recovery_request.result.command_id:
            return None
        return self.recovery_request.observation


class _Mirror:
    def enqueue_nowait(self, _):
        return None


def _application(store: _AuthorityStore, actor: GatewayRole):
    settings = GatewaySettings(
        ledger_dsn=SecretStr("mysql://unused/unused"),
        captain_gateway_token=SecretStr("captain-test-token"),
        worker_gateway_token=SecretStr("worker-test-token"),
    )
    app = create_app(
        gateway_store=store,
        mirror=_Mirror(),
        settings=settings,
    )

    async def selected_actor(_: Request) -> GatewayRole:
        return actor

    app.dependency_overrides[require_actor] = selected_actor
    return app


def test_authority_routes_are_captain_write_and_reader_read_scoped() -> None:
    store = _AuthorityStore()
    release = store.release.model_dump(mode="json", by_alias=True)
    recovery = store.recovery_request.model_dump(mode="json", by_alias=True)
    recovery_headers = {
        "X-Runtime-Owner-ID": "runtime-a",
        "X-Runtime-Fencing-Token": "2",
        "X-Runtime-Claim-Credential": "one-time-recovery-credential",
    }
    with TestClient(_application(store, GatewayRole.WORKER)) as worker:
        assert worker.post("/v1/factory/terminal-decisions", json=release).status_code == 403
        assert worker.post("/v1/capabilities", json=release).status_code == 403
        assert worker.post(
            "/v1/runtime/result-recoveries",
            json=recovery,
            headers=recovery_headers,
        ).status_code == 403
        assert worker.get(
            f"/v1/factory/terminal-decisions/{store.release.decision.job_id}"
        ).status_code == 200
        assert worker.get(
            f"/v1/capabilities/{store.catalog.capability_id}"
        ).status_code == 200

    with TestClient(_application(store, GatewayRole.CAPTAIN)) as captain:
        first = captain.post("/v1/capabilities", json=release)
        replay = captain.post("/v1/factory/terminal-decisions", json=release)
        execution = captain.post(
            "/v1/capability-executions",
            json=store.execution_request.model_dump(mode="json", by_alias=True),
        )
        recovered = captain.post(
            "/v1/runtime/result-recoveries",
            json=recovery,
            headers=recovery_headers,
        )
        recovery_replay = captain.post(
            "/v1/runtime/result-recoveries",
            json=recovery,
            headers=recovery_headers,
        )
        recovery_readback = captain.get(
            f"/v1/runtime/result-recoveries/{store.recovery_request.result.command_id}"
        )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert execution.status_code == 201
    assert recovered.status_code == 201
    assert recovery_replay.status_code == 200
    assert recovery_readback.json()["event_id"] == str(
        store.recovery_request.observation.event_id
    )


def test_nonrelease_terminal_decision_has_its_own_captain_api() -> None:
    store = _AuthorityStore()
    with TestClient(_application(store, GatewayRole.CAPTAIN)) as captain:
        created = captain.post(
            "/v1/factory/terminal-decisions",
            json=store.nonrelease_terminal.model_dump(mode="json", by_alias=True),
        )
        recovered = captain.get(
            f"/v1/factory/terminal-decisions/{store.nonrelease_terminal.job_id}"
        )

    assert created.status_code == 201
    assert recovered.status_code == 200
    assert recovered.json()["state"] == "blocked"
