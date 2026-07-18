from __future__ import annotations

import json
from pathlib import Path
import ast

from agenten.delivery.minibook_events import MinibookProjectionEvent


FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "contracts"
    / "minibook_projection.v2.json"
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
    assert all(event.schema_name.endswith(".v2") for event in events)
    assert not any(
        key in document["payload"]
        for document in documents
        for key in (
            "public_title",
            "status",
            "assignee_display_name",
            "evidence_summary",
        )
    )
    assert not any(term in raw.lower() for term in FORBIDDEN_TERMS)
    assert "C:\\" not in raw
    assert '"/' not in raw


def test_captain_projection_uses_no_minibook_or_worker_package_internals() -> None:
    repository_root = Path(__file__).parents[2]
    production_files = (
        repository_root / "agenten" / "delivery" / "minibook_client.py",
        repository_root / "agenten" / "delivery" / "minibook_events.py",
        repository_root / "agenten" / "delivery" / "projection_cursor.py",
        repository_root / "agenten" / "delivery" / "projector.py",
    )

    imports: set[str] = set()
    for path in production_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)

    assert not any(
        name == prefix or name.startswith(f"{prefix}.")
        for name in imports
        for prefix in ("minibook", "hermes", "minibook.swarm")
    )
