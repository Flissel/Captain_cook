from __future__ import annotations

from contextlib import contextmanager
import hashlib
from uuid import NAMESPACE_URL, uuid5
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from gateway.capability_live_pipeline import CapabilityResumeExecutionRequest
from gateway.capability_resume_client import (
    GatewayCapabilityResumeClient,
    GatewayCapabilityResumeClientError,
)
from gateway.app import create_app
from gateway.capability_resume_contracts import CapabilityResumeTerminalV1
from gateway.capability_resume_service import CapabilityResumeDispatchFailed
from gateway.settings import GatewaySettings
from agenten.agent_runtime.contracts import (
    AgentRuntimeResult,
    ArtifactRef,
    RuntimeCostEvidenceV1,
    RuntimeProviderCostSettlementV1,
    RuntimeOperation,
    RuntimeResumeCostAuthorityV1,
    RuntimeResumeCostSettlementV1,
    RuntimeResumeCostSettlementRequestV1,
    RuntimeStatus,
    RuntimeUsagePricingSnapshotV1,
    RuntimeProviderUsageReceiptV1,
    canonical_json_bytes,
)
from agenten.provider_proxy.contracts import ProviderBudgetContextV1
from tests.agent_runtime.test_service import (
    FakeCapabilityPolicy,
    FakeCodex,
    FakeCostAuthority,
    artifact,
    batch_for,
    resume_command_with_cost,
)
from gateway.store import GatewayStore


CAPTAIN_TOKEN = "captain-resume-test-token"
WORKER_TOKEN = "worker-resume-test-token"
PRICING = RuntimeUsagePricingSnapshotV1(
    schema_name="captain.runtime-usage-pricing-snapshot.v1",
    snapshot_id="openai-test-2026-08-09",
    provider="openai",
    model="gpt-5.6-terra",
    input_cost_per_million_usd=Decimal("1.25"),
    cached_input_cost_per_million_usd=Decimal("0.125"),
    output_cost_per_million_usd=Decimal("10.00"),
    effective_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    expires_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
)


class RecordingResumeService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def authorize(self, request):
        from gateway.capability_live_pipeline import CapabilityResumeAuthorization

        self.calls.append("authorize")
        return CapabilityResumeAuthorization(
            job_id=request.job_id,
            reservation_id=request.reservation_id,
            correlation_id=request.correlation_id,
            capability_id=request.capability_id,
            capability_version=request.capability_version,
            authorization_receipt_id=uuid5(NAMESPACE_URL, "resume-api-authorization"),
            evidence_refs=(
                {
                    "uri": "artifact://sha256/" + "1" * 64,
                    "sha256": "1" * 64,
                    "media_type": "application/json",
                },
            ),
        )

    async def dispatch(self, authorization):
        self.calls.append("dispatch")
        return self._execution(authorization)

    @staticmethod
    def _execution(authorization):
        from gateway.capability_live_pipeline import CapabilityExecutionAuthority

        return CapabilityExecutionAuthority(
            job_id=authorization.job_id,
            correlation_id=authorization.correlation_id,
            capability_id=authorization.capability_id,
            capability_version=authorization.capability_version,
            execution_authority_id=authorization.authorization_receipt_id,
            execution_command_id=uuid5(NAMESPACE_URL, "resume-api-command"),
            execution_result_id=uuid5(NAMESPACE_URL, "resume-api-result"),
            evidence_refs=authorization.evidence_refs,
            booked_cost_usd="1.25",
            terminal_status="succeeded",
            replayed=False,
        )

    def readback(self, authorization):
        self.calls.append("readback")
        return self._execution(authorization)


def _settings() -> GatewaySettings:
    return GatewaySettings.model_validate(
        {
            "ledger_dsn": "mysql://ignored:ignored@127.0.0.1:3306/ignored",
            "captain_gateway_token": CAPTAIN_TOKEN,
            "worker_gateway_token": WORKER_TOKEN,
            "provider_receipt_gateway_token": "provider-receipt-test-token",
            "captain_runtime_url": "https://runtime.test",
            "captain_runtime_token": "runtime-resume-test-token",
        }
    )


def _provider_settlement(command, result) -> dict[str, object]:
    evidence = result.cost_evidence
    receipt = {
        "schema": "captain.runtime-provider-usage-receipt.v1",
        "receipt_id": str(evidence.receipt_id),
        "request_id": str(command.event_id),
        "command_id": str(command.causation_id or command.event_id),
        "result_id": str(result.event_id),
        "reservation_id": str(command.payload.budget_reservation_id),
        "job_id": str(command.payload.cost_job_id),
        "run_id": str(command.payload.cost_run_id),
        "input_id": command.payload.cost_input_id,
        "correlation_id": str(command.correlation_id),
        "capability_id": command.payload.cost_capability_id,
        "capability_version": command.payload.cost_capability_version,
        "session_id": result.session_id,
        "provider": "openai",
        "model": PRICING.model,
        "input_units": 100_000,
        "cached_input_units": 0,
        "output_units": 12_500,
        "actual_cost_usd": "0.25",
        "pricing_snapshot_id": PRICING.snapshot_id,
        "pricing_snapshot_sha256": PRICING.snapshot_sha256,
        "started_at": "2026-08-09T12:00:00Z",
        "ended_at": "2026-08-09T12:00:01Z",
    }
    receipt_bytes = canonical_json_bytes(
        RuntimeProviderUsageReceiptV1.model_validate(receipt)
    )
    return {
        "schema": "captain.runtime-provider-cost-settlement.v1",
        "settlement_id": str(uuid5(command.event_id, "provider-cost-settlement")),
        "provider_call_id": "provider-call-one",
        "receipt": receipt,
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "settled_at": "2026-08-09T12:00:02Z",
    }


def _provider_bound_runtime(command):
    grant = FakeCapabilityPolicy().derive(command, batch_for(command), command.occurred_at)
    result = FakeCodex(["command_accepted", "grant_recorded"])._result(command, grant)
    result = result.model_copy(
        update={
            "cost_evidence": result.cost_evidence.model_copy(
                update={
                    "pricing_snapshot_id": PRICING.snapshot_id,
                    "pricing_snapshot_sha256": PRICING.snapshot_sha256,
                    "started_at": datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
                    "ended_at": datetime(2026, 8, 9, 12, 0, 1, tzinfo=timezone.utc),
                }
            )
        }
    )
    return grant, result


def _authorization_request() -> dict[str, object]:
    return {
        "job_id": str(uuid5(NAMESPACE_URL, "resume-api-job")),
        "reservation_id": str(uuid5(NAMESPACE_URL, "resume-api-reservation")),
        "run_id": str(uuid5(NAMESPACE_URL, "resume-api-run")),
        "input_id": "input-one",
        "maximum_cost_usd": "2.00",
        "booked_cost_usd": "1.25",
        "correlation_id": str(uuid5(NAMESPACE_URL, "resume-api-correlation")),
        "capability_id": "claims-capability",
        "capability_version": 3,
        "original_execution_command_id": str(
            uuid5(NAMESPACE_URL, "resume-api-original-command")
        ),
        "original_execution_result_id": str(
            uuid5(NAMESPACE_URL, "resume-api-original-result")
        ),
    }


def _client(service: RecordingResumeService) -> TestClient:
    return TestClient(
        create_app(settings=_settings(), capability_resume_service=service)
    )


def test_resume_authorization_requires_the_captain_token() -> None:
    service = RecordingResumeService()
    client = _client(service)
    path = "/v1/capability-resumes/authorizations"

    assert client.post(path, json=_authorization_request()).status_code == 401
    assert client.post(
        path,
        json=_authorization_request(),
        headers={"Authorization": "Bearer wrong"},
    ).status_code == 401
    assert client.post(
        path,
        json=_authorization_request(),
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
    ).status_code == 403
    assert service.calls == []


def test_only_provider_receipt_role_can_record_provider_settlement() -> None:
    command = resume_command_with_cost()
    _, result = _provider_bound_runtime(command)

    class Store:
        def __init__(self) -> None:
            self.calls = 0

        def record_runtime_provider_settlement(self, settlement):
            self.calls += 1
            return SimpleNamespace(
                operation_id=settlement.receipt.request_id,
                replayed=False,
            )

        def record_runtime_result(self, _result):
            raise HTTPException(
                status_code=409,
                detail="runtime provider settlement is unavailable",
            )

    store = Store()
    client = TestClient(
        create_app(
            settings=_settings(),
            gateway_store=store,
            capability_resume_service=RecordingResumeService(),
        )
    )
    path = "/v1/capability-resumes/provider-settlements"
    payload = _provider_settlement(command, result)

    assert client.post(path, json=payload).status_code == 401
    assert client.post(
        path,
        json=payload,
        headers={"Authorization": f"Bearer {CAPTAIN_TOKEN}"},
    ).status_code == 403
    assert client.post(
        path,
        json=payload,
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
    ).status_code == 403
    assert client.post(
        path,
        json=payload,
        headers={"Authorization": "Bearer runtime-resume-test-token"},
    ).status_code == 401
    accepted = client.post(
        path,
        json=payload,
        headers={"Authorization": "Bearer provider-receipt-test-token"},
    )

    assert accepted.status_code == 201
    assert store.calls == 1
    assert client.post(
        "/v1/runtime/results",
        json=result.model_dump(mode="json", by_alias=True),
        headers={"Authorization": "Bearer provider-receipt-test-token"},
    ).status_code == 401
    assert client.post(
        "/v1/runtime/results",
        json=result.model_dump(mode="json", by_alias=True),
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
    ).status_code == 409


def test_provider_receipt_credential_cannot_equal_runtime_credential() -> None:
    with pytest.raises(ValueError, match="distinct authority"):
        GatewaySettings.model_validate(
            {
                "ledger_dsn": "mysql://ignored:ignored@127.0.0.1:3306/ignored",
                "captain_gateway_token": CAPTAIN_TOKEN,
                "worker_gateway_token": WORKER_TOKEN,
                "provider_receipt_gateway_token": "shared-runtime-token",
                "captain_runtime_url": "https://runtime.test",
                "captain_runtime_token": "shared-runtime-token",
            }
        )


def test_resume_authorize_dispatch_and_readback_are_authenticated_typed_routes() -> None:
    service = RecordingResumeService()
    client = _client(service)
    headers = {"Authorization": f"Bearer {CAPTAIN_TOKEN}"}

    authorized = client.post(
        "/v1/capability-resumes/authorizations",
        json=_authorization_request(),
        headers=headers,
    )
    dispatched = client.post(
        "/v1/capability-resumes/dispatches",
        json=authorized.json(),
        headers=headers,
    )
    readback = client.post(
        "/v1/capability-resumes/readbacks",
        json=authorized.json(),
        headers=headers,
    )

    assert authorized.status_code == 201
    assert dispatched.status_code == 201
    assert readback.status_code == 200
    assert dispatched.json() == readback.json()
    assert service.calls == ["authorize", "dispatch", "readback"]


@pytest.mark.asyncio
async def test_resume_http_client_implements_authorize_dispatch_readback_port() -> None:
    service = RecordingResumeService()
    app = create_app(settings=_settings(), capability_resume_service=service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
    ) as http:
        client = GatewayCapabilityResumeClient(
            base_url="http://gateway.test",
            token=CAPTAIN_TOKEN,
            client=http,
        )
        request = CapabilityResumeExecutionRequest.model_validate(
            {
                **_authorization_request(),
                "job_id": uuid5(NAMESPACE_URL, "resume-api-job"),
                    "reservation_id": uuid5(NAMESPACE_URL, "resume-api-reservation"),
                    "run_id": uuid5(NAMESPACE_URL, "resume-api-run"),
                "correlation_id": uuid5(NAMESPACE_URL, "resume-api-correlation"),
                "original_execution_command_id": uuid5(
                    NAMESPACE_URL, "resume-api-original-command"
                ),
                "original_execution_result_id": uuid5(
                    NAMESPACE_URL, "resume-api-original-result"
                ),
            },
            strict=True,
        )

        authorization = await client.authorize_resume(request)
        dispatched = await client.dispatch_resume(authorization)
        readback = await client.readback_resume(authorization)

    assert dispatched == readback
    assert service.calls == ["authorize", "dispatch", "readback"]


@pytest.mark.asyncio
async def test_resume_http_client_error_does_not_retain_token_or_response_body() -> None:
    service = RecordingResumeService()
    app = create_app(settings=_settings(), capability_resume_service=service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
    ) as http:
        client = GatewayCapabilityResumeClient(
            base_url="http://gateway.test",
            token="wrong-secret-token",
            client=http,
        )
        request = CapabilityResumeExecutionRequest.model_validate(
            {
                **_authorization_request(),
                "job_id": uuid5(NAMESPACE_URL, "resume-api-job"),
                    "reservation_id": uuid5(NAMESPACE_URL, "resume-api-reservation"),
                    "run_id": uuid5(NAMESPACE_URL, "resume-api-run"),
                "correlation_id": uuid5(NAMESPACE_URL, "resume-api-correlation"),
                "original_execution_command_id": uuid5(
                    NAMESPACE_URL, "resume-api-original-command"
                ),
                "original_execution_result_id": uuid5(
                    NAMESPACE_URL, "resume-api-original-result"
                ),
            },
            strict=True,
        )

        with pytest.raises(GatewayCapabilityResumeClientError) as captured:
            await client.authorize_resume(request)

    message = str(captured.value)
    assert "status 401" in message
    assert "wrong-secret-token" not in message
    assert "invalid or missing bearer token" not in message


def test_default_app_composes_authenticated_runtime_resume_executor(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class RuntimeExecutor:
        def __init__(self, base_url: str, token: str, client: object) -> None:
            captured.update(base_url=base_url, token=token, client=client)

        async def execute(self, command):
            raise AssertionError("preflight must not execute a runtime command")

    monkeypatch.setattr("gateway.app.AgentRuntimeHttpExecutor", RuntimeExecutor)
    app = create_app(settings=_settings(), gateway_store=object())

    with TestClient(app):
        app.state.get_capability_resume_service()

    assert captured["base_url"] == "https://runtime.test"
    assert captured["token"] == "runtime-resume-test-token"


@pytest.mark.parametrize("operation", ("dispatch", "readback"))
def test_resume_terminal_runtime_failure_is_translated_to_502(operation: str) -> None:
    service = RecordingResumeService()
    authorization = service.authorize(
        CapabilityResumeExecutionRequest.model_validate(
            {
                **_authorization_request(),
                "job_id": uuid5(NAMESPACE_URL, "resume-api-job"),
                "reservation_id": uuid5(NAMESPACE_URL, "resume-api-reservation"),
                "run_id": uuid5(NAMESPACE_URL, "resume-api-run"),
                "correlation_id": uuid5(NAMESPACE_URL, "resume-api-correlation"),
                "original_execution_command_id": uuid5(
                    NAMESPACE_URL, "resume-api-original-command"
                ),
                "original_execution_result_id": uuid5(
                    NAMESPACE_URL, "resume-api-original-result"
                ),
            },
            strict=True,
        )
    )
    failed = service._execution(authorization).model_copy(
        update={"terminal_status": "failed"}
    )
    error = CapabilityResumeDispatchFailed(
        CapabilityResumeTerminalV1(
            authorization_receipt_id=authorization.authorization_receipt_id,
            execution=failed,
        )
    )

    async def dispatch(_authorization):
        raise error

    def readback(_authorization):
        raise error

    service.dispatch = dispatch  # type: ignore[method-assign]
    service.readback = readback  # type: ignore[method-assign]
    response = _client(service).post(
        f"/v1/capability-resumes/{'dispatches' if operation == 'dispatch' else 'readbacks'}",
        json=authorization.model_dump(mode="json"),
        headers={"Authorization": f"Bearer {CAPTAIN_TOKEN}"},
    )

    assert response.status_code == 502
    assert response.json()["detail"]["failure_id"] == str(failed.execution_result_id)


def test_runtime_cost_authority_route_requires_captain_auth_and_validates_command() -> None:
    command = resume_command_with_cost()

    class CostStore:
        calls = 0

        def validate_capability_resume_cost_authority(self, observed, *, now):
            self.calls += 1
            assert observed == command
            return RuntimeResumeCostAuthorityV1(
                schema_name="captain.runtime-resume-cost-authority.v1",
                authorization_receipt_id=uuid5(NAMESPACE_URL, "authority-one"),
                cost_authority_ref=command.payload.cost_authority_ref,
                reservation_id=command.payload.budget_reservation_id,
                job_id=command.payload.cost_job_id,
                run_id=command.payload.cost_run_id,
                input_id=command.payload.cost_input_id,
                correlation_id=command.correlation_id,
                capability_id=command.payload.cost_capability_id,
                capability_version=command.payload.cost_capability_version,
                command_id=command.event_id,
                ceiling_usd=Decimal("0.75"),
                expires_at=datetime(2026, 8, 9, 13, tzinfo=timezone.utc),
                hard_ceiling_enforced=True,
                metering_mode="provider_usage_receipt",
            )

    store = CostStore()
    client = TestClient(
        create_app(
            settings=_settings(),
            gateway_store=store,
            capability_resume_service=RecordingResumeService(),
        )
    )
    path = "/v1/capability-resumes/cost-authorities/validate"
    payload = command.model_dump(mode="json", by_alias=True)

    assert client.post(path, json=payload).status_code == 401
    assert client.post(
        path,
        json=payload,
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
    ).status_code == 403
    accepted = client.post(
        path,
        json=payload,
        headers={"Authorization": f"Bearer {CAPTAIN_TOKEN}"},
    )

    assert accepted.status_code == 200
    assert accepted.json()["command_id"] == str(command.event_id)
    assert store.calls == 1


def test_runtime_cost_settlement_route_books_typed_actual_usage() -> None:
    command = resume_command_with_cost()
    authority = RuntimeResumeCostAuthorityV1(
        schema_name="captain.runtime-resume-cost-authority.v1",
        authorization_receipt_id=uuid5(NAMESPACE_URL, "authority-one"),
        cost_authority_ref=command.payload.cost_authority_ref,
        reservation_id=command.payload.budget_reservation_id,
        job_id=command.payload.cost_job_id,
        run_id=command.payload.cost_run_id,
        input_id=command.payload.cost_input_id,
        correlation_id=command.correlation_id,
        capability_id=command.payload.cost_capability_id,
        capability_version=command.payload.cost_capability_version,
        command_id=command.event_id,
        ceiling_usd=Decimal("0.75"),
        expires_at=datetime(2026, 8, 9, 13, tzinfo=timezone.utc),
        hard_ceiling_enforced=True,
        metering_mode="provider_usage_receipt",
    )
    cost = RuntimeCostEvidenceV1(
        schema_name="captain.runtime-cost-evidence.v1",
        receipt_id=uuid5(command.event_id, "provider-usage"),
        command_id=command.event_id,
        result_id=uuid5(command.event_id, "runtime-result"),
        original_command_id=command.causation_id or command.event_id,
        reservation_id=authority.reservation_id,
        job_id=authority.job_id,
        run_id=authority.run_id,
        input_id=authority.input_id,
        correlation_id=authority.correlation_id,
        capability_id=authority.capability_id,
        capability_version=authority.capability_version,
        provider="openai",
        model="gpt-5.6-terra",
        input_units=100_000,
        cached_input_units=0,
        output_units=12_500,
        actual_cost_usd=Decimal("0.25"),
        pricing_snapshot_id=PRICING.snapshot_id,
        pricing_snapshot_sha256=PRICING.snapshot_sha256,
        started_at=datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 9, 12, 0, 1, tzinfo=timezone.utc),
        evidence_ref=artifact("provider-usage", "f"),
    )
    result = AgentRuntimeResult(
        schema_name="captain.agent-runtime-result.v1",
        event_id=uuid5(command.event_id, "runtime-result"),
        command_id=command.event_id,
        correlation_id=command.correlation_id,
        occurred_at=datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
        producer="agent-runtime",
        subject_id=command.subject_id,
        subject_version=command.subject_version,
        grant_id="grant-resume",
        operation=RuntimeOperation.CODEX_RESUME,
        status=RuntimeStatus.SUCCEEDED,
        session_id="session-resume",
        evidence_refs=(artifact("runtime-terminal", "d"),),
        cost_evidence=cost,
    )

    class CostStore:
        calls = 0

        def settle_capability_resume_cost(self, request, *, now):
            self.calls += 1
            assert request.command == command
            assert request.result == result
            assert request.authority == authority
            return RuntimeResumeCostSettlementV1(
                schema_name="captain.runtime-resume-cost-settlement.v1",
                settlement_id=uuid5(command.event_id, "cost-settlement"),
                command_id=command.event_id,
                reservation_id=authority.reservation_id,
                disposition="accounted",
                actual_cost_usd=Decimal("0.25"),
                accounted_cost_usd=Decimal("0.25"),
                evidence_refs=(cost.evidence_ref,),
            )

    store = CostStore()
    client = TestClient(
        create_app(
            settings=_settings(),
            gateway_store=store,
            capability_resume_service=RecordingResumeService(),
        )
    )
    response = client.post(
        "/v1/capability-resumes/cost-authorities/settle",
        json={
            "command": command.model_dump(mode="json", by_alias=True),
            "result": result.model_dump(mode="json", by_alias=True),
            "authority": authority.model_dump(mode="json", by_alias=True),
        },
        headers={"Authorization": f"Bearer {CAPTAIN_TOKEN}"},
    )

    assert response.status_code == 200
    assert response.json()["actual_cost_usd"] == "0.25"
    assert store.calls == 1


@pytest.mark.asyncio
async def test_gateway_derives_actual_overrun_and_unmetered_resume_accounting() -> None:
    derive = getattr(
        GatewayStore, "_derive_capability_resume_cost_settlement", None
    )
    assert derive is not None, "Gateway resume cost accounting is unavailable"
    command = resume_command_with_cost()
    authority = await FakeCostAuthority().authorize(command)
    grant = FakeCapabilityPolicy().derive(command, batch_for(command), command.occurred_at)
    result = await FakeCodex(["command_accepted", "grant_recorded"]).resume(
        command, grant
    )

    accounted = derive(
        RuntimeResumeCostSettlementRequestV1(
            command=command,
            result=result,
            authority=authority,
        )
    )
    overrun = derive(
        RuntimeResumeCostSettlementRequestV1(
            command=command,
            result=result.model_copy(
                update={
                    "cost_evidence": result.cost_evidence.model_copy(
                        update={"actual_cost_usd": Decimal("0.80")}
                    )
                }
            ),
            authority=authority,
        )
    )
    unmetered = derive(
        RuntimeResumeCostSettlementRequestV1(
            command=command,
            result=result.model_copy(update={"cost_evidence": None}),
            authority=authority,
        )
    )

    assert (accounted.disposition, accounted.accounted_cost_usd) == (
        "accounted",
        Decimal("0.25"),
    )
    assert (overrun.disposition, overrun.accounted_cost_usd) == (
        "overrun",
        Decimal("0.80"),
    )
    assert (unmetered.disposition, unmetered.accounted_cost_usd) == (
        "unmetered",
        Decimal("0.75"),
    )


def test_gateway_cost_settlement_replay_survives_closed_reservation() -> None:
    command = resume_command_with_cost()
    authority = RuntimeResumeCostAuthorityV1(
        schema_name="captain.runtime-resume-cost-authority.v1",
        authorization_receipt_id=uuid5(NAMESPACE_URL, "authority-one"),
        cost_authority_ref=command.payload.cost_authority_ref,
        reservation_id=command.payload.budget_reservation_id,
        job_id=command.payload.cost_job_id,
        run_id=command.payload.cost_run_id,
        input_id=command.payload.cost_input_id,
        correlation_id=command.correlation_id,
        capability_id=command.payload.cost_capability_id,
        capability_version=command.payload.cost_capability_version,
        command_id=command.event_id,
        ceiling_usd=Decimal("0.75"),
        expires_at=datetime(2026, 8, 9, 13, tzinfo=timezone.utc),
        hard_ceiling_enforced=True,
        metering_mode="provider_usage_receipt",
    )
    grant = FakeCapabilityPolicy().derive(command, batch_for(command), command.occurred_at)
    result = FakeCodex(["command_accepted", "grant_recorded"])._result(command, grant)
    receipt = RuntimeProviderUsageReceiptV1(
        schema_name="captain.runtime-provider-usage-receipt.v1",
        receipt_id=result.cost_evidence.receipt_id,
        request_id=command.event_id,
        command_id=command.causation_id or command.event_id,
        result_id=result.event_id,
        reservation_id=authority.reservation_id,
        job_id=authority.job_id,
        run_id=authority.run_id,
        input_id=authority.input_id,
        correlation_id=authority.correlation_id,
        capability_id=authority.capability_id,
        capability_version=authority.capability_version,
        session_id=result.session_id,
        provider="openai",
        model=PRICING.model,
        input_units=100_000,
        cached_input_units=0,
        output_units=12_500,
        actual_cost_usd=Decimal("0.25"),
        pricing_snapshot_id=PRICING.snapshot_id,
        pricing_snapshot_sha256=PRICING.snapshot_sha256,
        started_at=datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 9, 12, 0, 1, tzinfo=timezone.utc),
    )
    receipt_bytes = canonical_json_bytes(receipt)
    receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    receipt_ref = ArtifactRef(
        uri=f"artifact://sha256/{receipt_digest}",
        sha256=receipt_digest,
        media_type="application/json",
    )
    result = result.model_copy(
        update={
            "cost_evidence": result.cost_evidence.model_copy(
                update={
                    "pricing_snapshot_id": PRICING.snapshot_id,
                    "pricing_snapshot_sha256": PRICING.snapshot_sha256,
                    "started_at": receipt.started_at,
                    "ended_at": receipt.ended_at,
                    "evidence_ref": receipt_ref,
                }
            )
        }
    )
    request = RuntimeResumeCostSettlementRequestV1(
        command=command, result=result, authority=authority
    )
    settlement = GatewayStore._derive_capability_resume_cost_settlement(request)

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

    class Storage:
        @contextmanager
        def transaction(self):
            yield Connection()

    store = GatewayStore.__new__(GatewayStore)
    store.storage = Storage()
    store._runtime_usage_artifacts = SimpleNamespace(
        read_bytes=lambda ref: receipt_bytes
    )
    store._runtime_usage_pricing_snapshots = (PRICING,)
    store._runtime_block_by_json_value = lambda *_args, **_kwargs: {
        "data": _provider_settlement(command, result)
    }
    store._factory_budget_event = lambda *_args, **_kwargs: {
        "payload": settlement.model_dump(mode="json", by_alias=True)
    }
    store._validate_capability_resume_cost_authority_with_cursor = (
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("closed reservation must not be revalidated on exact replay")
        )
    )

    assert store.settle_capability_resume_cost(
        request, now=datetime(2026, 8, 10, tzinfo=timezone.utc)
    ) == settlement


def test_production_runtime_cost_authority_does_not_claim_an_absent_cli_hard_ceiling() -> None:
    command = resume_command_with_cost()
    command = command.model_copy(
        update={
            "payload": command.payload.model_copy(
                update={
                    "cost_authority_ref": (
                        "gateway://capability-resume-authorizations/"
                        f"{uuid5(NAMESPACE_URL, 'production-authority')}"
                    )
                }
            )
        }
    )
    request = {
        "reservation_id": str(command.payload.budget_reservation_id),
        "job_id": str(command.payload.cost_job_id),
        "run_id": str(command.payload.cost_run_id),
        "input_id": command.payload.cost_input_id,
        "correlation_id": str(command.correlation_id),
        "capability_id": command.payload.cost_capability_id,
        "capability_version": command.payload.cost_capability_version,
    }

    class Cursor:
        def execute(self, *_args):
            return None

        def fetchone(self):
            return {
                "payload": {
                    "resume_command": command.model_dump(mode="json", by_alias=True),
                    "request": request,
                }
            }

    store = GatewayStore.__new__(GatewayStore)
    store._runtime_block_by_json_value = lambda *_args, **_kwargs: {
        "data": command.model_dump(mode="json", by_alias=True)
    }
    store._factory_reservation = lambda *_args, **_kwargs: SimpleNamespace(
        reservation_id=command.payload.budget_reservation_id,
        job_id=command.payload.cost_job_id,
        correlation_id=command.correlation_id,
        requested_usd=command.payload.maximum_cost_usd,
        expires_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    store._reservation_is_closed = lambda *_args, **_kwargs: False

    authority = store._validate_capability_resume_cost_authority_with_cursor(
        Cursor(), command, now=datetime(2026, 8, 9, tzinfo=timezone.utc)
    )

    assert authority.hard_ceiling_enforced is False


def test_gateway_claims_hard_ceiling_only_for_exact_active_proxy_binding() -> None:
    command = resume_command_with_cost()
    receipt_id = uuid5(NAMESPACE_URL, "bound-production-authority")
    expires = datetime(2026, 8, 10, tzinfo=timezone.utc)
    policy_sha, price_sha = "a" * 64, "b" * 64
    context = ProviderBudgetContextV1(
        schema="captain.provider-budget-context.v1",
        command_id=command.event_id,
        original_command_id=command.causation_id or command.event_id,
        result_id=uuid5(command.event_id, "captain.runtime-result"),
        reservation_id=command.payload.budget_reservation_id,
        job_id=command.payload.cost_job_id,
        run_id=command.payload.cost_run_id,
        input_id=command.payload.cost_input_id,
        correlation_id=command.correlation_id,
        capability_id=command.payload.cost_capability_id,
        capability_version=command.payload.cost_capability_version,
        session_id="codex-session-bound",
        cost_authority_ref=f"gateway://capability-resume-authorizations/{receipt_id}",
        ceiling_usd=command.payload.maximum_cost_usd,
        expires_at=expires,
        policy_sha256=policy_sha,
        price_card_sha256=price_sha,
    )
    command = command.model_copy(update={"payload": command.payload.model_copy(update={
        "cost_authority_ref": context.cost_authority_ref,
        "provider_proxy_url": "http://127.0.0.1:18091/v1",
        "provider_policy_sha256": policy_sha,
        "provider_price_card_sha256": price_sha,
        "provider_context_sha256": context.sha256,
        "provider_proxy_executable_sha256": "c" * 64,
        "provider_proxy_code_sha256": "d" * 64,
        "provider_paid_command_sha256": "e" * 64,
        "provider_hard_ceiling_enforced": True,
        "provider_session_id": context.session_id,
        "provider_result_id": context.result_id,
    })})
    request = {
        "reservation_id": str(command.payload.budget_reservation_id),
        "job_id": str(command.payload.cost_job_id), "run_id": str(command.payload.cost_run_id),
        "input_id": command.payload.cost_input_id, "correlation_id": str(command.correlation_id),
        "capability_id": command.payload.cost_capability_id,
        "capability_version": command.payload.cost_capability_version,
    }

    class Cursor:
        def execute(self, *_args): return None
        def fetchone(self):
            return {"payload": {"resume_command": command.model_dump(mode="json", by_alias=True), "request": request}}
        def __enter__(self): return self
        def __exit__(self, *_args): return None

    class Connection:
        def cursor(self): return Cursor()

    class Storage:
        @contextmanager
        def transaction(self): yield Connection()

    store = GatewayStore.__new__(GatewayStore)
    store.storage = Storage()
    store._runtime_provider_proxy_bindings = frozenset({(
        "http://127.0.0.1:18091/v1", policy_sha, price_sha,
    )})
    store._runtime_provider_bundle_bindings = frozenset({(
        "http://127.0.0.1:18091", policy_sha, price_sha, context.sha256,
        "c" * 64, "d" * 64, "e" * 64, True,
    )})
    store._runtime_block_by_json_value = lambda *_args, **_kwargs: {
        "data": command.model_dump(mode="json", by_alias=True)
    }
    store._factory_reservation = lambda *_args, **_kwargs: SimpleNamespace(
        reservation_id=command.payload.budget_reservation_id,
        job_id=command.payload.cost_job_id, correlation_id=command.correlation_id,
        requested_usd=command.payload.maximum_cost_usd, expires_at=expires,
    )
    store._reservation_is_closed = lambda *_args, **_kwargs: False

    authority = store._validate_capability_resume_cost_authority_with_cursor(
        Cursor(), command, now=datetime(2026, 8, 9, tzinfo=timezone.utc)
    )

    assert authority.hard_ceiling_enforced is True
    assert authority.provider_context_sha256 == context.sha256
    assert authority.provider_proxy_executable_sha256 == "c" * 64
    assert authority.provider_proxy_code_sha256 == "d" * 64
    assert authority.provider_paid_command_sha256 == "e" * 64
    assert store.validate_provider_proxy_authority(
        context, now=datetime(2026, 8, 9, tzinfo=timezone.utc)
    ) == context
    assert store.resolve_provider_proxy_authority(
        f"gateway://provider-budget-contexts/{context.sha256}",
        now=datetime(2026, 8, 9, tzinfo=timezone.utc),
    ) == context

    with pytest.raises(HTTPException, match="provider authority is unavailable"):
        store.validate_provider_proxy_authority(
            context.model_copy(update={"session_id": "forged-session"}),
            now=datetime(2026, 8, 9, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize("mutation", ["valid", "forged_zero", "unknown_model", "digest"])
def test_gateway_reprices_cas_usage_and_rejects_forgery(
    mutation: str,
) -> None:
    verify = getattr(GatewayStore, "_verify_runtime_provider_usage", None)
    assert verify is not None, "Gateway provider usage verifier is missing"
    command = resume_command_with_cost()
    result_id = uuid5(command.event_id, "runtime-result")
    receipt = RuntimeProviderUsageReceiptV1(
        schema_name="captain.runtime-provider-usage-receipt.v1",
        receipt_id=uuid5(command.event_id, "provider-usage"),
        request_id=command.event_id,
        command_id=command.causation_id or command.event_id,
        result_id=result_id,
        reservation_id=command.payload.budget_reservation_id,
        job_id=command.payload.cost_job_id,
        run_id=command.payload.cost_run_id,
        input_id=command.payload.cost_input_id,
        correlation_id=command.correlation_id,
        capability_id=command.payload.cost_capability_id,
        capability_version=command.payload.cost_capability_version,
        session_id="runtime-session",
        provider="openai",
        model=("unknown-model" if mutation == "unknown_model" else PRICING.model),
        input_units=100_000,
        cached_input_units=0,
        output_units=12_500,
        actual_cost_usd=(Decimal("0") if mutation == "forged_zero" else Decimal("0.25")),
        pricing_snapshot_id=PRICING.snapshot_id,
        pricing_snapshot_sha256=PRICING.snapshot_sha256,
        started_at=datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 9, 12, 0, 1, tzinfo=timezone.utc),
    )
    content = canonical_json_bytes(receipt)
    digest = hashlib.sha256(content).hexdigest()
    evidence = RuntimeCostEvidenceV1(
        schema_name="captain.runtime-cost-evidence.v1",
        receipt_id=receipt.receipt_id,
        command_id=command.event_id,
        result_id=result_id,
        original_command_id=receipt.command_id,
        reservation_id=receipt.reservation_id,
        job_id=receipt.job_id,
        run_id=receipt.run_id,
        input_id=receipt.input_id,
        correlation_id=receipt.correlation_id,
        capability_id=receipt.capability_id,
        capability_version=receipt.capability_version,
        provider=receipt.provider,
        model=receipt.model,
        input_units=receipt.input_units,
        cached_input_units=receipt.cached_input_units,
        output_units=receipt.output_units,
        actual_cost_usd=receipt.actual_cost_usd,
        pricing_snapshot_id=receipt.pricing_snapshot_id,
        pricing_snapshot_sha256=receipt.pricing_snapshot_sha256,
        started_at=receipt.started_at,
        ended_at=receipt.ended_at,
        evidence_ref=artifact(
            "usage-receipt",
            "0" if mutation == "digest" else digest[0],
        ).model_copy(
            update={
                "uri": f"artifact://sha256/{'0' * 64 if mutation == 'digest' else digest}",
                "sha256": "0" * 64 if mutation == "digest" else digest,
                "media_type": "application/json",
            }
        ),
    )

    arguments = {
        "command": command,
        "result_id": result_id,
        "evidence": evidence,
        "receipt_bytes": content,
        "pricing_snapshots": (PRICING,),
    }
    if mutation == "valid":
        assert verify(**arguments) == Decimal("0.250000")
    else:
        with pytest.raises(HTTPException, match="usage"):
            verify(
                **arguments
            )


@pytest.mark.parametrize(
    "settlement_state", ["missing", "wrong_result", "forged_zero"]
)
def test_cost_bound_runtime_result_requires_exact_preexisting_provider_settlement(
    settlement_state: str,
) -> None:
    command = resume_command_with_cost()
    grant, result = _provider_bound_runtime(command)
    settlement = _provider_settlement(command, result)
    if settlement_state == "wrong_result":
        settlement["receipt"] = {
            **settlement["receipt"],
            "result_id": str(uuid5(NAMESPACE_URL, "wrong-provider-result")),
        }
    elif settlement_state == "forged_zero":
        settlement["receipt"] = {
            **settlement["receipt"],
            "input_units": 0,
            "output_units": 0,
            "actual_cost_usd": "0",
        }
    inserted: list[dict[str, object]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

    class Storage:
        @contextmanager
        def transaction(self):
            yield Connection()

    def block(_cursor, *, block_type, **_kwargs):
        if block_type == "agent_runtime_command":
            return {"index": 1, "data": command.model_dump(mode="json", by_alias=True)}
        if block_type == "agent_runtime_grant":
            return {"index": 2, "data": grant.model_dump(mode="json", by_alias=True)}
        if block_type == "runtime_provider_cost_settlement":
            return None if settlement_state == "missing" else {"data": settlement}
        return None

    store = GatewayStore.__new__(GatewayStore)
    store.storage = Storage()
    store._runtime_block_by_json_value = block
    store._runtime_result_block = lambda *_args, **_kwargs: None
    store._next_index = lambda _cursor: 3
    store._new_block = lambda *_args, **kwargs: kwargs
    store._insert = lambda _cursor, value: inserted.append(value)

    with pytest.raises(HTTPException, match="provider settlement"):
        store._record_runtime_result_once(result)

    assert inserted == []


def test_matching_preexisting_provider_settlement_allows_result_once() -> None:
    command = resume_command_with_cost()
    grant, result = _provider_bound_runtime(command)
    settlement = _provider_settlement(command, result)
    inserted: list[dict[str, object]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

    class Storage:
        @contextmanager
        def transaction(self):
            yield Connection()

    def block(_cursor, *, block_type, **_kwargs):
        values = {
            "agent_runtime_command": {
                "index": 1,
                "data": command.model_dump(mode="json", by_alias=True),
            },
            "agent_runtime_grant": {
                "index": 2,
                "data": grant.model_dump(mode="json", by_alias=True),
            },
            "runtime_provider_cost_settlement": {"data": settlement},
        }
        return values.get(block_type)

    store = GatewayStore.__new__(GatewayStore)
    store.storage = Storage()
    store._runtime_block_by_json_value = block
    store._runtime_result_block = lambda *_args, **_kwargs: (
        {
            "data": result.model_dump(mode="json", by_alias=True),
        }
        if inserted
        else None
    )
    store._next_index = lambda _cursor: 3
    store._new_block = lambda *_args, **kwargs: kwargs
    store._insert = lambda _cursor, value: inserted.append(value)
    store._retry_write = lambda operation: operation()

    first = store.record_runtime_result(result)
    replay = store.record_runtime_result(result)

    assert len(inserted) == 1
    assert inserted[0]["block_type"] == "agent_runtime_result"
    assert first.replayed is False
    assert replay.replayed is True


def test_provider_usage_may_be_contained_by_wider_runtime_process_window() -> None:
    command = resume_command_with_cost()
    _, result = _provider_bound_runtime(command)
    settlement = _provider_settlement(command, result)
    evidence = result.cost_evidence.model_copy(
        update={
            "started_at": datetime(2026, 8, 9, 11, 59, 59, tzinfo=timezone.utc),
            "ended_at": datetime(2026, 8, 9, 12, 0, 2, tzinfo=timezone.utc),
        }
    )
    result = result.model_copy(update={"cost_evidence": evidence})
    store = GatewayStore.__new__(GatewayStore)
    store._runtime_block_by_json_value = lambda *_args, **_kwargs: {
        "data": settlement
    }

    accepted = store._require_provider_settlement_for_result(
        object(), command, result
    )

    assert accepted.receipt.started_at >= evidence.started_at
    assert accepted.receipt.ended_at <= evidence.ended_at


def test_runtime_cas_receipt_cannot_establish_budget_settlement_truth() -> None:
    command = resume_command_with_cost()
    _, result = _provider_bound_runtime(command)
    provider = _provider_settlement(command, result)
    receipt_bytes = canonical_json_bytes(
        RuntimeProviderUsageReceiptV1.model_validate(provider["receipt"])
    )
    digest = hashlib.sha256(receipt_bytes).hexdigest()
    result = result.model_copy(
        update={
            "cost_evidence": result.cost_evidence.model_copy(
                update={
                    "evidence_ref": ArtifactRef(
                        uri=f"artifact://sha256/{digest}",
                        sha256=digest,
                        media_type="application/json",
                    )
                }
            )
        }
    )
    authority = RuntimeResumeCostAuthorityV1(
        schema_name="captain.runtime-resume-cost-authority.v1",
        authorization_receipt_id=uuid5(NAMESPACE_URL, "provider-authority"),
        cost_authority_ref=command.payload.cost_authority_ref,
        reservation_id=command.payload.budget_reservation_id,
        job_id=command.payload.cost_job_id,
        run_id=command.payload.cost_run_id,
        input_id=command.payload.cost_input_id,
        correlation_id=command.correlation_id,
        capability_id=command.payload.cost_capability_id,
        capability_version=command.payload.cost_capability_version,
        command_id=command.event_id,
        ceiling_usd=command.payload.maximum_cost_usd,
        expires_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        hard_ceiling_enforced=True,
        metering_mode="provider_usage_receipt",
    )
    request = RuntimeResumeCostSettlementRequestV1(
        command=command,
        result=result,
        authority=authority,
    )

    class Artifacts:
        def read_bytes(self, _reference):
            return receipt_bytes

    store = GatewayStore.__new__(GatewayStore)
    store._runtime_usage_artifacts = Artifacts()
    store._runtime_usage_pricing_snapshots = (PRICING,)

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

    class Storage:
        @contextmanager
        def transaction(self):
            yield Connection()

    store.storage = Storage()
    store._runtime_block_by_json_value = lambda *_args, **_kwargs: None

    with pytest.raises(HTTPException, match="provider settlement"):
        store._verified_capability_resume_cost_request(request)


def test_provider_authority_settlement_is_independently_repriced() -> None:
    command = resume_command_with_cost()
    _, result = _provider_bound_runtime(command)
    settlement = RuntimeProviderCostSettlementV1.model_validate(
        _provider_settlement(command, result)
    )
    store = GatewayStore.__new__(GatewayStore)
    store._runtime_usage_pricing_snapshots = (PRICING,)

    store._validate_runtime_provider_settlement(settlement, command)

    unknown_receipt = settlement.receipt.model_copy(update={"model": "unknown-model"})
    unknown = settlement.model_copy(
        update={
            "receipt": unknown_receipt,
            "receipt_sha256": hashlib.sha256(
                canonical_json_bytes(unknown_receipt)
            ).hexdigest(),
        }
    )
    with pytest.raises(HTTPException, match="provider settlement binding"):
        store._validate_runtime_provider_settlement(unknown, command)
