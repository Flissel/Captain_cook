from __future__ import annotations

import json
from pathlib import Path

from agenten.delivery.minibook_events import MinibookProjectionEvent


FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "contracts"
    / "minibook_projection.v1.json"
)
FORBIDDEN_TERMS = (
    "token",
    "password",
    "secret",
    "holdout",
    "prompt",
    "transcript",
)


def test_runtime_projection_contract_is_versioned_complete_and_redacted() -> None:
    raw = FIXTURE.read_text(encoding="utf-8")
    documents = json.loads(raw)

    events = [MinibookProjectionEvent.model_validate(item) for item in documents]

    assert len(events) == 8
    assert {event.payload.view for event in events} == {
        "project",
        "plan",
        "blueprint",
        "build",
        "validation",
    }
    assert all(event.schema_name.endswith(".v1") for event in events)
    assert not any(term in raw.lower() for term in FORBIDDEN_TERMS)
    assert "C:\\" not in raw
    assert '"/' not in raw
