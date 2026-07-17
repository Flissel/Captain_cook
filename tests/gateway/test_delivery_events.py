from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from gateway.contracts import (
    DeliveryEventEnvelope,
    ReleaseProjection,
    TraceContext,
    project_release,
)


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
TRACE = {
    "project_id": "project-1",
    "run_id": "run-1",
    "trace_id": "trace-1",
}


EVENT_CASES = (
    (
        "codex_task",
        {"batch_id": "batch-1"},
        {
            "task_id": "task-1",
            "target": "build workflow",
            "context_sha256": "a" * 64,
            "workspace_ref": "artifact://workspaces/task-1",
            "permissions": ("filesystem.read",),
            "budget": 100,
        },
        "task_id",
    ),
    (
        "codex_session",
        {"batch_id": "batch-1", "session_id": "session-1"},
        {
            "session_id": "session-1",
            "process_ref": "artifact://processes/session-1",
            "started_at": NOW,
            "ended_at": NOW + timedelta(minutes=1),
            "exit_class": "completed",
        },
        "session_id",
    ),
    (
        "artifact_built",
        {"batch_id": "batch-1", "artifact_id": "artifact-1"},
        {
            "artifact_id": "artifact-1",
            "artifact_version": "v1",
            "sha256": "b" * 64,
            "artifact_type": "workflow",
            "sealed_ref": "artifact://sealed/artifact-1",
        },
        "artifact_id",
    ),
    (
        "deploy",
        {"batch_id": "batch-1", "artifact_id": "artifact-1"},
        {
            "deployment_id": "deploy-1",
            "target": "n8n",
            "artifact_version": "v1",
            "external_deployment_ref": "artifact://deployments/deploy-1",
            "result": "succeeded",
        },
        "deployment_id",
    ),
    (
        "validation_run",
        {"batch_id": "batch-1", "artifact_id": "artifact-1", "case_id": "case-1"},
        {
            "validation_id": "validation-1",
            "layer": "holdout",
            "case_ids": ("case-1",),
            "assertion_results": {"schema": "passed"},
            "evidence_refs": ("artifact://evidence/validation-1",),
            "artifact_version": "v1",
            "passed": True,
        },
        "validation_id",
    ),
    (
        "repair_request",
        {"batch_id": "batch-1"},
        {
            "repair_id": "repair-1",
            "iteration": 1,
            "failure_class": "assertion_failed",
            "report_ref": "artifact://reports/repair-1",
        },
        "repair_id",
    ),
    (
        "batch_done",
        {"batch_id": "batch-1"},
        {"outcome": "succeeded"},
        "outcome",
    ),
    (
        "e2e_run",
        {"batch_id": "batch-1"},
        {
            "e2e_run_id": "e2e-1",
            "run_index": 1,
            "clean": True,
            "trace_complete": True,
            "evidence_refs": ("artifact://evidence/e2e-1",),
        },
        "e2e_run_id",
    ),
    (
        "evaluation",
        {"batch_id": "batch-1", "case_id": "case-1"},
        {
            "evaluation_id": "evaluation-1",
            "hard_passed": True,
            "semantic_score": 0.95,
            "safety_passed": True,
        },
        "evaluation_id",
    ),
    (
        "release_decision",
        {},
        {
            "decision": "accepted",
            "policy_version": "2026-07-17",
            "reasons": ("three clean runs",),
        },
        "policy_version",
    ),
    (
        "registry_mirror",
        {"artifact_id": "artifact-1"},
        {
            "capability_id": "capability-1",
            "capability_version": "v1",
            "outcome": "mirrored",
        },
        "capability_id",
    ),
)


@pytest.mark.parametrize(("event_type", "trace_fields", "payload", "identifier"), EVENT_CASES)
def test_delivery_event_envelope_accepts_each_discriminated_payload(
    event_type: str,
    trace_fields: dict[str, str],
    payload: dict[str, object],
    identifier: str,
) -> None:
    event = DeliveryEventEnvelope.model_validate(
        {
            "event_id": uuid4(),
            "event_type": event_type,
            "occurred_at": NOW,
            "actor": "gateway",
            "trace": {**TRACE, **trace_fields},
            "payload": {"event_type": event_type, **payload},
        }
    )

    assert event.event_type == event_type
    assert getattr(event.payload, identifier) == payload[identifier]


@pytest.mark.parametrize(("event_type", "trace_fields", "payload", "identifier"), EVENT_CASES)
def test_delivery_event_payload_requires_its_event_specific_identifier(
    event_type: str,
    trace_fields: dict[str, str],
    payload: dict[str, object],
    identifier: str,
) -> None:
    with pytest.raises(ValidationError):
        DeliveryEventEnvelope.model_validate(
            {
                "event_id": uuid4(),
                "event_type": event_type,
                "occurred_at": NOW,
                "actor": "gateway",
                "trace": {**TRACE, **trace_fields},
                "payload": {
                    "event_type": event_type,
                    **{key: value for key, value in payload.items() if key != identifier},
                },
            }
        )


def test_delivery_event_requires_non_empty_trace_identity() -> None:
    with pytest.raises(ValidationError):
        TraceContext(project_id="", run_id="run-1", trace_id="trace-1")

    with pytest.raises(ValidationError):
        DeliveryEventEnvelope.model_validate(
            {
                "event_id": uuid4(),
                "event_type": "batch_done",
                "occurred_at": NOW,
                "actor": "gateway",
                "trace": {"project_id": "project-1", "run_id": "run-1"},
                "payload": {"event_type": "batch_done", "outcome": "failed"},
            }
        )


def test_delivery_event_rejects_a_payload_for_a_different_event_type() -> None:
    with pytest.raises(ValidationError, match="event_type"):
        DeliveryEventEnvelope.model_validate(
            {
                "event_id": uuid4(),
                "event_type": "batch_done",
                "occurred_at": NOW,
                "actor": "gateway",
                "trace": {**TRACE, "batch_id": "batch-1"},
                "payload": {
                    "event_type": "repair_request",
                    "repair_id": "repair-1",
                    "iteration": 1,
                    "failure_class": "assertion_failed",
                    "report_ref": "artifact://reports/repair-1",
                },
            }
        )


@pytest.mark.parametrize(
    "assertion_results, passed",
    (
        ({"schema": "failed"}, True),
        ({"schema": "passed"}, False),
    ),
)
def test_validation_run_rejects_contradictory_assertion_evidence(
    assertion_results: dict[str, str],
    passed: bool,
) -> None:
    with pytest.raises(ValidationError, match="passed"):
        DeliveryEventEnvelope.model_validate(
            {
                "event_id": uuid4(),
                "event_type": "validation_run",
                "occurred_at": NOW,
                "actor": "validator",
                "trace": {
                    **TRACE,
                    "batch_id": "batch-1",
                    "artifact_id": "artifact-1",
                    "case_id": "case-1",
                },
                "payload": {
                    "event_type": "validation_run",
                    "validation_id": "validation-1",
                    "layer": "holdout",
                    "case_ids": ("case-1",),
                    "assertion_results": assertion_results,
                    "evidence_refs": ("artifact://evidence/validation-1",),
                    "artifact_version": "v1",
                    "passed": passed,
                },
            }
        )


def test_validation_run_assertion_evidence_is_immutable_after_mapping_input() -> None:
    event = DeliveryEventEnvelope.model_validate(
        {
            "event_id": uuid4(),
            "event_type": "validation_run",
            "occurred_at": NOW,
            "actor": "validator",
            "trace": {
                **TRACE,
                "batch_id": "batch-1",
                "artifact_id": "artifact-1",
                "case_id": "case-1",
            },
            "payload": {
                "event_type": "validation_run",
                "validation_id": "validation-1",
                "layer": "holdout",
                "case_ids": ("case-1",),
                "assertion_results": {"schema": "passed"},
                "evidence_refs": ("artifact://evidence/validation-1",),
                "artifact_version": "v1",
                "passed": True,
            },
        }
    )

    assert event.payload.assertion_results[0].assertion_id == "schema"
    assert event.payload.assertion_results[0].outcome == "passed"
    with pytest.raises(TypeError):
        event.payload.assertion_results[0] = event.payload.assertion_results[0]
    with pytest.raises(ValidationError):
        event.payload.assertion_results[0].outcome = "failed"


def test_delivery_event_rejects_session_trace_and_payload_id_mismatch() -> None:
    with pytest.raises(ValidationError, match="session_id"):
        DeliveryEventEnvelope.model_validate(
            {
                "event_id": uuid4(),
                "event_type": "codex_session",
                "occurred_at": NOW,
                "actor": "gateway",
                "trace": {
                    **TRACE,
                    "batch_id": "batch-1",
                    "session_id": "session-1",
                },
                "payload": {
                    "event_type": "codex_session",
                    "session_id": "session-2",
                    "process_ref": "artifact://processes/session-2",
                    "started_at": NOW,
                    "ended_at": NOW,
                    "exit_class": "completed",
                },
            }
        )


def test_delivery_event_rejects_artifact_trace_and_payload_id_mismatch() -> None:
    with pytest.raises(ValidationError, match="artifact_id"):
        DeliveryEventEnvelope.model_validate(
            {
                "event_id": uuid4(),
                "event_type": "artifact_built",
                "occurred_at": NOW,
                "actor": "gateway",
                "trace": {
                    **TRACE,
                    "batch_id": "batch-1",
                    "artifact_id": "artifact-1",
                },
                "payload": {
                    "event_type": "artifact_built",
                    "artifact_id": "artifact-2",
                    "artifact_version": "v1",
                    "sha256": "b" * 64,
                    "artifact_type": "workflow",
                    "sealed_ref": "artifact://sealed/artifact-2",
                },
            }
        )


def test_delivery_event_rejects_validation_case_missing_from_trace() -> None:
    with pytest.raises(ValidationError, match="case_id"):
        DeliveryEventEnvelope.model_validate(
            {
                "event_id": uuid4(),
                "event_type": "validation_run",
                "occurred_at": NOW,
                "actor": "validator",
                "trace": {
                    **TRACE,
                    "batch_id": "batch-1",
                    "artifact_id": "artifact-1",
                    "case_id": "case-2",
                },
                "payload": {
                    "event_type": "validation_run",
                    "validation_id": "validation-1",
                    "layer": "holdout",
                    "case_ids": ("case-1",),
                    "assertion_results": {"schema": "passed"},
                    "evidence_refs": ("artifact://evidence/validation-1",),
                    "artifact_version": "v1",
                    "passed": True,
                },
            }
        )


def _e2e_event(
    run_id: str,
    *,
    index: int,
    clean: bool = True,
    trace_complete: bool = True,
) -> DeliveryEventEnvelope:
    return DeliveryEventEnvelope.model_validate(
        {
            "event_id": UUID(int=index),
            "event_type": "e2e_run",
            "occurred_at": NOW + timedelta(minutes=index),
            "actor": "evaluator",
            "trace": {**TRACE, "batch_id": "batch-1"},
            "payload": {
                "event_type": "e2e_run",
                "e2e_run_id": run_id,
                "run_index": index,
                "clean": clean,
                "trace_complete": trace_complete,
                "evidence_refs": (f"artifact://evidence/{run_id}",),
            },
        }
    )


def test_release_projection_stays_blocked_until_three_distinct_clean_e2e_runs() -> None:
    first = _e2e_event("e2e-1", index=1)
    duplicate = _e2e_event("e2e-1", index=2)
    incomplete = _e2e_event("e2e-2", index=3, trace_complete=False)
    dirty = _e2e_event("e2e-3", index=4, clean=False)
    third = _e2e_event("e2e-4", index=5)
    second = _e2e_event("e2e-5", index=6)

    blocked = project_release((first, duplicate, incomplete, dirty, third))
    ready = project_release((second, third, first))

    assert blocked == ReleaseProjection(
        status="blocked",
        clean_e2e_run_ids=("e2e-1", "e2e-4"),
        missing_clean_e2e_runs=1,
    )
    assert ready == ReleaseProjection(
        status="ready",
        clean_e2e_run_ids=("e2e-1", "e2e-4", "e2e-5"),
        missing_clean_e2e_runs=0,
    )


def test_delivery_event_models_are_frozen() -> None:
    event = _e2e_event("e2e-1", index=1)

    with pytest.raises(ValidationError):
        event.actor = "other"


@pytest.mark.parametrize(
    ("event_type", "payload"),
    (
        (
            "codex_session_started",
            {
                "session_id": "session-1",
                "process_ref": "artifact://processes/session-1",
                "started_at": NOW,
                "iteration": 1,
                "command_sha256": "a" * 64,
                "workspace_sha256": "b" * 64,
            },
        ),
        (
            "codex_session_event",
            {
                "session_id": "session-1",
                "source_sequence": 0,
                "lifecycle": "turn_completed",
                "input_tokens": 10,
                "cached_input_tokens": 2,
                "output_tokens": 3,
            },
        ),
        (
            "codex_session_warning",
            {
                "session_id": "session-1",
                "source_sequence": 1,
                "warning_type": "malformed_json",
                "line_sha256": "c" * 64,
            },
        ),
        (
            "codex_session_finished",
            {
                "session_id": "session-1",
                "process_ref": "artifact://processes/session-1",
                "started_at": NOW,
                "ended_at": NOW + timedelta(seconds=1),
                "outcome": "infrastructure_failure",
                "exit_code": 23,
                "behavioral_repair_increment": 0,
            },
        ),
    ),
)
def test_codex_session_delivery_events_are_truthful_frozen_contracts(
    event_type: str,
    payload: dict[str, object],
) -> None:
    event = DeliveryEventEnvelope.model_validate(
        {
            "event_id": uuid4(),
            "event_type": event_type,
            "occurred_at": NOW,
            "actor": "worker-1",
            "trace": {
                **TRACE,
                "batch_id": "batch-1",
                "worker_id": "worker-1",
                "claim_id": "claim-1",
                "fencing_token": 7,
                "session_id": "session-1",
            },
            "payload": {"event_type": event_type, **payload},
        }
    )

    assert event.payload.session_id == "session-1"
    with pytest.raises(ValidationError):
        event.payload.session_id = "different"


@pytest.mark.parametrize(
    "payload",
    (
        {
            "outcome": "infrastructure_failure",
            "behavioral_repair_increment": 1,
        },
        {
            "outcome": "cancelled",
            "behavioral_repair_increment": 0,
        },
        {
            "outcome": "succeeded",
            "behavioral_repair_increment": 0,
            "cancellation_reason": "operator",
        },
    ),
)
def test_codex_terminal_outcome_enforces_repair_and_cancellation_invariants(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        DeliveryEventEnvelope.model_validate(
            {
                "event_id": uuid4(),
                "event_type": "codex_session_finished",
                "occurred_at": NOW,
                "actor": "worker-1",
                "trace": {
                    **TRACE,
                    "batch_id": "batch-1",
                    "worker_id": "worker-1",
                    "claim_id": "claim-1",
                    "fencing_token": 7,
                    "session_id": "session-1",
                },
                "payload": {
                    "event_type": "codex_session_finished",
                    "session_id": "session-1",
                    "process_ref": "artifact://processes/session-1",
                    "started_at": NOW,
                    "ended_at": NOW,
                    "exit_code": None,
                    **payload,
                },
            }
        )


@pytest.mark.parametrize(
    "payload_update",
    (
        {"lifecycle": "item_completed", "item_id": None, "item_type": None},
        {"lifecycle": "item_completed", "input_tokens": 1},
        {"lifecycle": "turn_completed", "item_id": "item-1", "item_type": "message"},
        {"lifecycle": "turn_started", "output_tokens": 1},
        {"lifecycle": "failed", "item_id": "item-1"},
        {"lifecycle": "started", "cached_input_tokens": 1},
    ),
)
def test_codex_lifecycle_payload_rejects_fields_owned_by_other_lifecycles(
    payload_update: dict[str, object],
) -> None:
    payload = {
        "event_type": "codex_session_event",
        "session_id": "session-1",
        "source_sequence": 3,
        **payload_update,
    }
    if payload_update["lifecycle"] == "item_completed":
        payload.setdefault("item_id", "item-1")
        payload.setdefault("item_type", "message")
    with pytest.raises(ValidationError):
        DeliveryEventEnvelope.model_validate(
            {
                "event_id": uuid4(),
                "event_type": "codex_session_event",
                "occurred_at": NOW,
                "actor": "worker-1",
                "trace": {
                    **TRACE,
                    "batch_id": "batch-1",
                    "worker_id": "worker-1",
                    "claim_id": "claim-1",
                    "fencing_token": 7,
                    "session_id": "session-1",
                },
                "payload": payload,
            }
        )


def test_codex_warning_requires_sequence_and_forbids_lifecycle_metadata() -> None:
    with pytest.raises(ValidationError):
        DeliveryEventEnvelope.model_validate(
            {
                "event_id": uuid4(),
                "event_type": "codex_session_warning",
                "occurred_at": NOW,
                "actor": "worker-1",
                "trace": {
                    **TRACE,
                    "batch_id": "batch-1",
                    "worker_id": "worker-1",
                    "claim_id": "claim-1",
                    "fencing_token": 7,
                    "session_id": "session-1",
                },
                "payload": {
                    "event_type": "codex_session_warning",
                    "session_id": "session-1",
                    "source_sequence": 0,
                    "warning_type": "malformed_json",
                    "line_sha256": "a" * 64,
                    "input_tokens": 1,
                },
            }
        )
