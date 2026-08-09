from __future__ import annotations

from contextlib import contextmanager
from uuid import NAMESPACE_URL, uuid5
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
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
    RuntimeCostEvidenceV1,
    RuntimeOperation,
    RuntimeResumeCostAuthorityV1,
    RuntimeResumeCostSettlementV1,
    RuntimeResumeCostSettlementRequestV1,
    RuntimeStatus,
)
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
            "captain_runtime_url": "https://runtime.test",
            "captain_runtime_token": "runtime-resume-test-token",
        }
    )


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
        reservation_id=authority.reservation_id,
        job_id=authority.job_id,
        run_id=authority.run_id,
        input_id=authority.input_id,
        correlation_id=authority.correlation_id,
        capability_id=authority.capability_id,
        capability_version=authority.capability_version,
        provider="openai",
        model="gpt-5.6-terra",
        input_units=100,
        output_units=50,
        actual_cost_usd=Decimal("0.25"),
        started_at=datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
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
        Decimal("0.75"),
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
