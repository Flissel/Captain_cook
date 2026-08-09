from __future__ import annotations

from uuid import UUID, uuid5

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from gateway.app import create_app
from gateway.capability_assembly_contracts import CapabilityAssemblyAdvanceRequest
from gateway.settings import GatewaySettings
from tests.gateway.test_capability_assembly_controller import (
    ASSEMBLY_ID,
    NS,
    _controller,
    _id,
    _submission,
)


CAPTAIN_TOKEN = "captain-assembly-test-token"
WORKER_TOKEN = "worker-assembly-test-token"
PROVIDER_TOKEN = "provider-assembly-test-token"


class _PortProbe:
    def __init__(self, calls: dict[str, int], mutate) -> None:
        self.calls = calls
        self.mutate = mutate

    def _touch(self, method: str) -> None:
        self.calls[method] += 1
        self.mutate()
        raise AssertionError(f"malformed controller method ran: {method}")

    def submit(self, request):
        self._touch("submit")

    def projection(self, assembly_id):
        self._touch("projection")

    async def advance(self, request):
        self._touch("advance")

    def final_evidence(self, assembly_id):
        self._touch("final_evidence")


class _NonCallableSubmit(_PortProbe):
    submit = None


class _TwoArgumentSubmit(_PortProbe):
    def submit(self, request, extra):
        self._touch("submit")


class _AsyncSubmit(_PortProbe):
    async def submit(self, request):
        self._touch("submit")


class _OptionalSubmit(_PortProbe):
    def submit(self, request=None):
        self._touch("submit")


class _NoArgumentProjection(_PortProbe):
    def projection(self):
        self._touch("projection")


class _AsyncProjection(_PortProbe):
    async def projection(self, assembly_id):
        self._touch("projection")


class _VariadicProjection(_PortProbe):
    def projection(self, *assembly_ids):
        self._touch("projection")


class _TwoArgumentFinalEvidence(_PortProbe):
    def final_evidence(self, assembly_id, extra):
        self._touch("final_evidence")


class _KeywordOnlyFinalEvidence(_PortProbe):
    def final_evidence(self, *, assembly_id):
        self._touch("final_evidence")


class _AsyncFinalEvidence(_PortProbe):
    async def final_evidence(self, assembly_id):
        self._touch("final_evidence")


class _SyncAdvance(_PortProbe):
    def advance(self, request):
        self._touch("advance")


class _TwoArgumentAdvance(_PortProbe):
    async def advance(self, request, extra):
        self._touch("advance")


class _RecordingController:
    def __init__(self, controller, calls: dict[str, int]) -> None:
        self.controller = controller
        self.calls = calls

    def submit(self, request):
        self.calls["submit"] += 1
        return self.controller.submit(request)

    def projection(self, assembly_id):
        self.calls["projection"] += 1
        return self.controller.projection(assembly_id)

    async def advance(self, request):
        self.calls["advance"] += 1
        return await self.controller.advance(request)

    def final_evidence(self, assembly_id):
        self.calls["final_evidence"] += 1
        return self.controller.final_evidence(assembly_id)


def _settings() -> GatewaySettings:
    return GatewaySettings(
        ledger_dsn="mysql://ignored:ignored@127.0.0.1:3306/ignored",
        captain_gateway_token=SecretStr(CAPTAIN_TOKEN),
        worker_gateway_token=SecretStr(WORKER_TOKEN),
        provider_receipt_gateway_token=SecretStr(PROVIDER_TOKEN),
    )


def _headers(token: str = CAPTAIN_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _client(controller_factory=None) -> TestClient:
    return TestClient(
        create_app(
            settings=_settings(),
            capability_assembly_controller_factory=controller_factory,
        )
    )


def _submit_payload(*, assembly_id: UUID = ASSEMBLY_ID) -> dict[str, object]:
    return _submission(assembly_id=assembly_id).model_dump(mode="json", by_alias=True)


def _advance_payload(controller, operation: str, *, maximum_slices: int = 1):
    return CapabilityAssemblyAdvanceRequest(
        assembly_id=ASSEMBLY_ID,
        operation_id=_id(operation),
        request_sha256=controller.projection(ASSEMBLY_ID).request_sha256,
        maximum_slices=maximum_slices,
    ).model_dump(mode="json", by_alias=True)


@pytest.mark.parametrize(
    "controller_type",
    [
        _NonCallableSubmit,
        _TwoArgumentSubmit,
        _AsyncSubmit,
        _OptionalSubmit,
        _NoArgumentProjection,
        _AsyncProjection,
        _VariadicProjection,
        _TwoArgumentFinalEvidence,
        _KeywordOnlyFinalEvidence,
        _AsyncFinalEvidence,
        _SyncAdvance,
        _TwoArgumentAdvance,
    ],
)
def test_malformed_controller_binding_fails_before_any_method_or_state_effect(
    controller_type,
) -> None:
    valid_controller, repository, *_ = _controller()
    calls = {
        "factory": 0,
        "submit": 0,
        "projection": 0,
        "advance": 0,
        "final_evidence": 0,
    }

    def factory():
        calls["factory"] += 1
        return controller_type(
            calls,
            lambda: valid_controller.submit(_submission()),
        )

    client = _client(factory)
    health = client.get("/v1/capability-assemblies/health", headers=_headers())
    submit = client.post(
        "/v1/capability-assemblies",
        json=_submit_payload(),
        headers=_headers(),
    )

    assert health.status_code == 503
    assert submit.status_code == 503
    assert health.json() == submit.json() == {
        "detail": "capability assembly service unavailable"
    }
    assert calls == {
        "factory": 1,
        "submit": 0,
        "projection": 0,
        "advance": 0,
        "final_evidence": 0,
    }
    assert repository.history(ASSEMBLY_ID) == ()


def test_factory_is_resolved_once_and_health_never_touches_controller() -> None:
    controller, *_ = _controller()
    calls = {
        "factory": 0,
        "submit": 0,
        "projection": 0,
        "advance": 0,
        "final_evidence": 0,
    }
    recording = _RecordingController(controller, calls)

    def factory():
        calls["factory"] += 1
        return recording

    client = _client(factory)
    assert calls == {
        "factory": 1,
        "submit": 0,
        "projection": 0,
        "advance": 0,
        "final_evidence": 0,
    }

    for _ in range(3):
        response = client.get(
            "/v1/capability-assemblies/health",
            headers=_headers(),
        )
        assert response.status_code == 200

    assert calls == {
        "factory": 1,
        "submit": 0,
        "projection": 0,
        "advance": 0,
        "final_evidence": 0,
    }

    submitted = client.post(
        "/v1/capability-assemblies",
        json=_submit_payload(),
        headers=_headers(),
    )
    projected = client.get(
        f"/v1/capability-assemblies/{ASSEMBLY_ID}",
        headers=_headers(),
    )

    assert submitted.status_code == 200
    assert projected.status_code == 200
    assert calls == {
        "factory": 1,
        "submit": 1,
        "projection": 1,
        "advance": 0,
        "final_evidence": 0,
    }


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({}, 401),
        (_headers("wrong-token"), 401),
        (_headers(WORKER_TOKEN), 403),
        (_headers(PROVIDER_TOKEN), 401),
    ],
)
def test_health_is_captain_only_and_never_exposes_controller_state(
    headers: dict[str, str],
    expected_status: int,
) -> None:
    controller, *_ = _controller()

    response = _client(lambda: controller).get(
        "/v1/capability-assemblies/health",
        headers=headers,
    )

    assert response.status_code == expected_status
    assert str(ASSEMBLY_ID) not in response.text
    assert "checkpoint" not in response.text.lower()


def test_health_returns_only_fixed_safe_metadata_for_injected_controller() -> None:
    controller, *_ = _controller()

    response = _client(lambda: controller).get(
        "/v1/capability-assemblies/health",
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "schema": "captain.capability-assembly-api-health.v1",
        "status": "ready",
        "composition": "injected",
    }


def test_missing_or_failed_controller_binding_is_fixed_redacted_503() -> None:
    missing = _client().get(
        "/v1/capability-assemblies/health",
        headers=_headers(),
    )
    incomplete = _client(lambda: None).get(
        "/v1/capability-assemblies/health",
        headers=_headers(),
    )

    factory_calls = 0

    def unavailable():
        nonlocal factory_calls
        factory_calls += 1
        raise RuntimeError(
            "C:\\private\\environment.env provider-body=private"
        )

    failed_client = _client(unavailable)
    failed = failed_client.get(
        "/v1/capability-assemblies/health",
        headers=_headers(),
    )
    failed_again = failed_client.post(
        "/v1/capability-assemblies",
        json=_submit_payload(),
        headers=_headers(),
    )

    assert missing.status_code == 503
    assert incomplete.status_code == 503
    assert failed.status_code == 503
    assert failed_again.status_code == 503
    expected = {"detail": "capability assembly service unavailable"}
    assert missing.json() == expected
    assert incomplete.json() == expected
    assert failed.json() == expected
    assert failed_again.json() == expected
    assert factory_calls == 1
    assert "private" not in failed.text
    assert "environment" not in failed.text


def test_submit_is_idempotent_and_conflicting_key_is_redacted_409() -> None:
    controller, *_ = _controller()
    client = _client(lambda: controller)
    path = "/v1/capability-assemblies"
    payload = _submit_payload()

    first = client.post(path, json=payload, headers=_headers())
    replay = client.post(path, json=payload, headers=_headers())
    conflict = client.post(
        path,
        json=_submit_payload(assembly_id=uuid5(NS, "conflicting-assembly")),
        headers=_headers(),
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["revision"] == 1
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "capability assembly conflict"}


def test_projection_is_captain_only_and_not_found_is_fixed_404() -> None:
    controller, *_ = _controller()
    controller.submit(_submission())
    client = _client(lambda: controller)
    path = f"/v1/capability-assemblies/{ASSEMBLY_ID}"

    worker = client.get(path, headers=_headers(WORKER_TOKEN))
    provider = client.get(path, headers=_headers(PROVIDER_TOKEN))
    missing = client.get(
        f"/v1/capability-assemblies/{uuid5(NS, 'missing-assembly')}",
        headers=_headers(),
    )
    found = client.get(path, headers=_headers())

    assert worker.status_code == 403
    assert provider.status_code == 401
    assert str(ASSEMBLY_ID) not in worker.text
    assert str(ASSEMBLY_ID) not in provider.text
    assert missing.status_code == 404
    assert missing.json() == {"detail": "capability assembly not found"}
    assert found.status_code == 200
    assert found.json()["assembly_id"] == str(ASSEMBLY_ID)


def test_advance_requires_exact_path_binding_and_one_slice() -> None:
    controller, *_ = _controller()
    controller.submit(_submission())
    client = _client(lambda: controller)
    path = f"/v1/capability-assemblies/{ASSEMBLY_ID}/advances"

    two_slices = client.post(
        path,
        json=_advance_payload(controller, "two-slices", maximum_slices=2),
        headers=_headers(),
    )
    mismatched = _advance_payload(controller, "mismatch")
    mismatched["assembly_id"] = str(uuid5(NS, "foreign-assembly"))
    wrong_path = client.post(path, json=mismatched, headers=_headers())
    valid_payload = _advance_payload(controller, "one-slice")
    first = client.post(path, json=valid_payload, headers=_headers())
    replay = client.post(path, json=valid_payload, headers=_headers())

    assert two_slices.status_code == 422
    assert two_slices.json() == {"detail": "capability assembly advance requires one slice"}
    assert wrong_path.status_code == 409
    assert wrong_path.json() == {"detail": "capability assembly conflict"}
    assert first.status_code == 200
    assert first.json()["executed_slices"] == 1
    assert first.json()["end_completed_slices"] == 1
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert controller.projection(ASSEMBLY_ID).completed_slices == 1


def test_validation_errors_are_fixed_and_never_echo_input_or_environment() -> None:
    controller, *_ = _controller()
    client = _client(lambda: controller)
    payload = _submit_payload()
    payload["inputs"][0]["repository_path"] = "C:\\private\\environment.env"

    response = client.post(
        "/v1/capability-assemblies",
        json=payload,
        headers=_headers(),
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid capability assembly request"}
    assert "private" not in response.text
    assert "environment" not in response.text


def test_restart_with_same_repository_preserves_projection_and_bounded_progress() -> None:
    first_controller, repository, *_ = _controller()
    first_client = _client(lambda: first_controller)
    first_client.post(
        "/v1/capability-assemblies",
        json=_submit_payload(),
        headers=_headers(),
    )
    advanced = first_client.post(
        f"/v1/capability-assemblies/{ASSEMBLY_ID}/advances",
        json=_advance_payload(first_controller, "before-restart"),
        headers=_headers(),
    )
    assert advanced.status_code == 200

    restarted_controller, *_ = _controller(repository)
    with _client(lambda: restarted_controller) as restarted:
        recovered = restarted.get(
            f"/v1/capability-assemblies/{ASSEMBLY_ID}",
            headers=_headers(),
        )

    assert recovered.status_code == 200
    assert recovered.json()["completed_slices"] == 1
    assert recovered.json()["revision"] > 1


def test_final_evidence_is_409_until_complete_then_returns_typed_evidence() -> None:
    controller, *_ = _controller()
    controller.submit(_submission())
    client = _client(lambda: controller)
    path = f"/v1/capability-assemblies/{ASSEMBLY_ID}/final-evidence"

    incomplete = client.get(path, headers=_headers())
    assert incomplete.status_code == 409
    assert incomplete.json() == {"detail": "capability assembly incomplete"}

    for ordinal in range(24):
        response = client.post(
            f"/v1/capability-assemblies/{ASSEMBLY_ID}/advances",
            json=_advance_payload(controller, f"complete-{ordinal}"),
            headers=_headers(),
        )
        assert response.status_code == 200
        assert response.json()["executed_slices"] == 1

    complete = client.get(path, headers=_headers())

    assert complete.status_code == 200
    assert complete.json()["schema"] == "captain.capability-live-demo-evidence.v1"
    assert complete.json()["run_id"] == str(ASSEMBLY_ID)
    assert len(complete.json()["capabilities"]) == 3
