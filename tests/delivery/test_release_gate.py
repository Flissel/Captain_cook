from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from gateway.contracts import DeliveryEventEnvelope
from gateway.release_policy import evaluate_release_readiness


NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)


def _event(
    event_type: str,
    *,
    batch_id: str,
    payload: dict[str, object],
) -> DeliveryEventEnvelope:
    trace: dict[str, object] = {
        "project_id": "project-1",
        "run_id": "candidate-1",
        "trace_id": f"trace-{batch_id}",
        "batch_id": batch_id,
    }
    if event_type == "artifact_built":
        trace["artifact_id"] = str(payload["artifact_id"])
    if event_type == "deploy":
        trace["artifact_id"] = "artifact-1"
    if event_type == "validation_run":
        trace.update({"artifact_id": "artifact-1", "case_id": "case-1"})
    if event_type == "codex_session_finished":
        trace.update(
            {
                "worker_id": "worker-1",
                "claim_id": "claim-1",
                "fencing_token": 1,
                "session_id": "session-1",
            }
        )
    return DeliveryEventEnvelope.model_validate(
        {
            "event_id": uuid4(),
            "event_type": event_type,
            "occurred_at": NOW,
            "actor": "captain",
            "trace": trace,
            "payload": {"event_type": event_type, **payload},
        }
    )


def _complete_run(*, batch_id: str, index: int) -> list[DeliveryEventEnvelope]:
    return [
        _event(
            "codex_session_finished",
            batch_id=batch_id,
            payload={
                "session_id": "session-1",
                "process_ref": "artifact://processes/session-1",
                "started_at": NOW,
                "ended_at": NOW,
                "outcome": "succeeded",
                "exit_code": 0,
                "behavioral_repair_increment": 0,
            },
        ),
        _event(
            "artifact_built",
            batch_id=batch_id,
            payload={
                "artifact_id": "artifact-1",
                "artifact_version": "1",
                "sha256": "a" * 64,
                "artifact_type": "n8n-workflow",
                "sealed_ref": "artifact://sealed/artifact-1",
            },
        ),
        _event(
            "deploy",
            batch_id=batch_id,
            payload={
                "deployment_id": f"deployment-{index}",
                "target": "n8n",
                "artifact_version": "1",
                "external_deployment_ref": f"artifact://n8n/{index}",
                "result": "succeeded",
            },
        ),
        _event(
            "validation_run",
            batch_id=batch_id,
            payload={
                "validation_id": f"validation-{index}",
                "layer": "live",
                "case_ids": ["case-1"],
                "assertion_results": {"execution": "passed"},
                "evidence_refs": [f"artifact://validation/{index}"],
                "artifact_version": "1",
                "passed": True,
            },
        ),
        _event(
            "e2e_run",
            batch_id=batch_id,
            payload={
                "e2e_run_id": f"e2e-{index}",
                "run_index": index,
                "clean": True,
                "trace_complete": True,
                "evidence_refs": [f"artifact://e2e/{index}"],
            },
        ),
    ]


def test_release_requires_three_complete_provider_backed_runs() -> None:
    events = [
        *_complete_run(batch_id="batch-1", index=1),
        *_complete_run(batch_id="batch-2", index=2),
        *_complete_run(batch_id="batch-3", index=3),
    ]

    readiness = evaluate_release_readiness(events)

    assert readiness.ready is True
    assert set(readiness.clean_e2e_run_ids) == {"e2e-1", "e2e-2", "e2e-3"}
    assert readiness.reasons == ()


def test_release_rejects_clean_e2e_without_provider_completion() -> None:
    events = [
        *_complete_run(batch_id="batch-1", index=1),
        *_complete_run(batch_id="batch-2", index=2),
        *_complete_run(batch_id="batch-3", index=3),
    ]
    events = [event for event in events if event.event_type != "validation_run"]

    readiness = evaluate_release_readiness(events)

    assert readiness.ready is False
    assert set(readiness.reasons) == {
        "e2e:e2e-1:validation_missing_or_failed",
        "e2e:e2e-2:validation_missing_or_failed",
        "e2e:e2e-3:validation_missing_or_failed",
    }
