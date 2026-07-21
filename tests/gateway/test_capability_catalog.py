from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import UUID

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pydantic import ValidationError

from agenten.agent_factory.contracts import FactoryPhase, PromotedCapability
from agenten.agent_factory.outcome_contracts import (
    ExecutionOutcomeV1,
    FactoryTerminalState,
)
from agenten.agent_factory.release_gate import derive_terminal_decision
from agenten.agent_factory.state_machine import FactoryProjection
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
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
    RuntimeExecutionClaim,
    RuntimeExecutionClaimRequest,
    RuntimeExecutionClaimReceipt,
    RuntimeReleasedBatchSnapshot,
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
    claim = RuntimeExecutionClaimRequest(
        schema_name="captain.runtime-execution-claim-request.v1",
        command_id=UUID("00000000-0000-0000-0000-000000000001"),
        owner_id="runtime-host-01",
        lease_seconds=120,
    )

    assert claim.owner_id == "runtime-host-01"
    with pytest.raises(ValidationError, match="lease"):
        RuntimeExecutionClaimRequest.model_validate(
            {
                **claim.model_dump(mode="json", by_alias=True),
                "lease_seconds": 3600,
            }
        )


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


def test_gateway_recomputes_ready_release_and_requires_prior_promotion() -> None:
    request = release_request()
    factory_job = v2_job()
    promotion = block(
        FactoryPhase.CAPABILITY_PROMOTED,
        assertions=factory_job.acceptance_assertion_ids,
    )

    GatewayStore._assert_capability_release_ready(
        job=factory_job,
        promotion=promotion,
        evaluation=accepted_evaluation(),
        request=request,
    )
    with pytest.raises(HTTPException, match="promotion"):
        GatewayStore._assert_capability_release_ready(
            job=factory_job,
            promotion=None,
            evaluation=accepted_evaluation(),
            request=request,
        )
    with pytest.raises(HTTPException, match="factory job"):
        GatewayStore._assert_capability_release_ready(
            job=factory_job.model_copy(update={"job_id": UUID(int=999)}),
            promotion=promotion,
            evaluation=accepted_evaluation(),
            request=request,
        )


def test_execution_claim_policy_is_restart_safe_owner_bound_and_fenced() -> None:
    first_request = RuntimeExecutionClaimRequest(
        schema_name="captain.runtime-execution-claim-request.v1",
        command_id=_runtime_command().event_id,
        owner_id="runtime-a",
        lease_seconds=60,
    )
    first = GatewayStore._resolve_runtime_execution_claim(
        existing=None,
        request=first_request,
        now=NOW,
        result_recorded=False,
    )
    replay = GatewayStore._resolve_runtime_execution_claim(
        existing=first.claim,
        request=first_request,
        now=NOW + timedelta(seconds=1),
        result_recorded=False,
    )

    assert first.claim.fencing_token == 1
    assert replay.replayed is True
    with pytest.raises(HTTPException, match="owned"):
        GatewayStore._resolve_runtime_execution_claim(
            existing=first.claim,
            request=first_request.model_copy(update={"owner_id": "runtime-b"}),
            now=NOW + timedelta(seconds=1),
            result_recorded=False,
        )

    recovered = GatewayStore._resolve_runtime_execution_claim(
        existing=first.claim,
        request=first_request.model_copy(update={"owner_id": "runtime-b"}),
        now=NOW + timedelta(seconds=61),
        result_recorded=False,
    )
    assert recovered.claim.fencing_token == 2
    assert recovered.recovered is True
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
    claim = RuntimeExecutionClaim(
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
        self.release_calls = 0
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
    with TestClient(_application(store, GatewayRole.WORKER)) as worker:
        assert worker.post("/v1/factory/terminal-decisions", json=release).status_code == 403
        assert worker.post("/v1/capabilities", json=release).status_code == 403
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

    assert first.status_code == 201
    assert replay.status_code == 200
    assert execution.status_code == 201


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
