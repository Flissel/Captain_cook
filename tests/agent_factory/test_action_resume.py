from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenten.agent_factory.action_resume import FactoryActionResumeCoordinator
from agenten.agent_factory.forge_contracts import FactoryActionRequestV1, FactoryActionResponseV1


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parents[1] / "fixtures" / "contracts"


class ConsumingStore:
    def __init__(self, request: FactoryActionRequestV1, response: FactoryActionResponseV1) -> None:
        self.request = request
        self.response = response
        self.calls: list[dict[str, object]] = []

    def consume(self, request_id, **kwargs):
        self.calls.append({"request_id": request_id, **kwargs})
        return self.response

    def get(self, request_id, *, tenant_id, now):
        assert request_id == self.request.request_id
        assert tenant_id == "tenant-a"
        return self.request.model_copy(update={"status": "answered"})


class RecordingProjection:
    def __init__(self) -> None:
        self.values: list[dict[str, object]] = []

    def project_factory_action(self, projection: dict[str, object]) -> None:
        self.values.append(projection)


class RecordingScheduler:
    def __init__(self) -> None:
        self.values: list[dict[str, object]] = []

    def schedule_factory_continuation(self, **continuation: object) -> None:
        self.values.append(continuation)


def test_resume_consumes_exact_checkpoint_projects_redacted_status_and_schedules_once() -> None:
    request = FactoryActionRequestV1.model_validate_json(
        (FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")
    )
    response = FactoryActionResponseV1.model_validate_json(
        (FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8")
    )
    store = ConsumingStore(request, response)
    projection = RecordingProjection()
    scheduler = RecordingScheduler()
    coordinator = FactoryActionResumeCoordinator(
        store=store,
        projection=projection,
        scheduler=scheduler,
        clock=lambda: NOW,
    )

    assert coordinator.resume(request_id=request.request_id, tenant_id="tenant-a") == response
    assert store.calls[0]["safe_checkpoint_sha256"] == request.safe_checkpoint_sha256
    assert scheduler.values == [
        {
            "factory_job_id": request.factory_job_id,
            "creation_job_id": request.creation_job_id,
            "correlation_id": request.correlation_id,
            "expected_subject_version": request.expected_subject_version,
            "safe_checkpoint_ref": request.safe_checkpoint_ref,
            "approved_scope": response.approved_scope,
            "credential_ref": response.credential_ref,
            "answer_metadata": response.answer_metadata,
        }
    ]
    serialized = repr(projection.values).lower()
    assert projection.values[0]["status"] == "consumed"
    assert "credential://" not in serialized
    assert "credential_alias" not in serialized


def test_resume_does_not_schedule_when_store_rejects_fence() -> None:
    request = FactoryActionRequestV1.model_validate_json(
        (FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")
    )
    response = FactoryActionResponseV1.model_validate_json(
        (FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8")
    )
    store = ConsumingStore(request, response)
    store.consume = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("checkpoint mismatch"))  # type: ignore[method-assign]
    scheduler = RecordingScheduler()
    coordinator = FactoryActionResumeCoordinator(
        store=store,
        projection=RecordingProjection(),
        scheduler=scheduler,
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="checkpoint mismatch"):
        coordinator.resume(request_id=request.request_id, tenant_id="tenant-a")
    assert scheduler.values == []
