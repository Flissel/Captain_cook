from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agenten.delivery.minibook_events import (
    MinibookProjectionEvent,
    redact_projection_payload,
)


FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "contracts"
    / "minibook_projection.v1.json"
)


def test_projection_fixture_round_trips_all_runtime_views() -> None:
    documents = json.loads(FIXTURE.read_text(encoding="utf-8"))

    events = [MinibookProjectionEvent.model_validate(item) for item in documents]

    assert [event.event_type for event in events] == [
        "plan.requested",
        "plan.published",
        "blueprint.published",
        "codex.running",
        "codex.result",
        "n8n.evidence",
        "validation.recorded",
        "replanning.requested",
    ]
    assert [event.subject_version for event in events] == list(range(1, 9))
    assert [event.model_dump(mode="json", by_alias=True) for event in events] == documents


@pytest.mark.parametrize(
    "payload",
    [
        {"view": "plan", "public_title": "Safe", "token": "redacted"},
        {"view": "plan", "public_title": "Safe", "nested": {"password": "x"}},
        {"view": "plan", "public_title": "Safe", "meta": {"api_secret": "x"}},
        {"view": "plan", "public_title": "Safe", "holdout_body": "x"},
        {"view": "plan", "public_title": "Safe", "raw_prompt": "x"},
        {"view": "plan", "public_title": "C:\\private\\workspace\\result.json"},
        {"view": "plan", "public_title": "/home/runner/private/result.json"},
        {"view": "plan", "public_title": "\\\\server\\share\\result.json"},
    ],
)
def test_redaction_fails_closed_for_forbidden_keys_and_paths(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        redact_projection_payload(payload)


def test_projection_payload_is_a_strict_allow_list() -> None:
    with pytest.raises(ValidationError):
        MinibookProjectionEvent.model_validate(
            {
                "schema": "captain.minibook-projection.v1",
                "event_id": "00000000-0000-4000-8000-000000000001",
                "correlation_id": "10000000-0000-4000-8000-000000000001",
                "causation_id": None,
                "occurred_at": "2026-07-18T08:00:00Z",
                "producer": "captain-gateway",
                "subject_id": "runtime-case-1",
                "subject_version": 1,
                "event_type": "plan.requested",
                "payload": {
                    "view": "plan",
                    "public_title": "Runtime plan requested",
                    "raw_transcript": "must never cross the boundary",
                },
            }
        )


def test_projection_subject_rejects_an_absolute_workspace_path() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    document["subject_id"] = "C:\\private\\captain-workspace"

    with pytest.raises(ValidationError):
        MinibookProjectionEvent.model_validate(document)


def test_projection_rejects_oversized_public_text_fields() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    document["payload"]["public_title"] = "x" * 201

    with pytest.raises(ValidationError):
        MinibookProjectionEvent.model_validate(document)


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "Authorization: Bearer fake-review-token-123456",
        "credential=fake-review-credential-123456",
        "password: fake-review-password-123456",
        "raw prompt canary must remain private",
        "raw transcript canary must remain private",
        "complete-log canary must remain private",
        "holdout canary must remain private",
        "artifact at C:\\private\\workspace\\result.json",
        "artifact at \\\\server\\share\\result.json",
        "artifact at /home/runner/private/result.json",
        "artifact at /private",
        "artifact at \\private\\result.json",
        "artifact at file:///home/runner/private/result.json",
    ],
)
def test_redaction_rejects_private_values_inside_allowed_fields(
    unsafe_value: str,
) -> None:
    with pytest.raises(ValueError):
        redact_projection_payload(
            {
                "view": "validation",
                "public_title": "Public validation result",
                "status": "recorded",
                "evidence_summary": unsafe_value,
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("public_title", "Runtime delivery plan published"),
        ("status", "validation-ready"),
        ("evidence_summary", "Public checks passed: 18 of 18."),
        ("assignee_display_name", "Captain Quality Warden"),
    ],
)
def test_redaction_accepts_normal_public_projection_text(
    field: str,
    value: str,
) -> None:
    payload: dict[str, object] = {
        "view": "validation",
        "public_title": "Public validation result",
        "status": "recorded",
    }
    payload[field] = value

    assert redact_projection_payload(payload).model_dump()[field] == value
