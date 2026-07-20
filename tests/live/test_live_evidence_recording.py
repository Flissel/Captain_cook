"""Explicitly opted-in acceptance gate for an exported recording evidence set."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import os
from pathlib import Path

import pytest

from docs.live_evidence_reporter import build_live_evidence_report


pytestmark = pytest.mark.live


def _configured_input() -> Path:
    configured = os.environ.get("CAPTAIN_LIVE_EVIDENCE_INPUT", "").strip()
    if not configured:
        pytest.skip(
            "live evidence prerequisite missing: CAPTAIN_LIVE_EVIDENCE_INPUT"
        )
    path = Path(configured)
    if not path.is_file():
        pytest.fail("configured CAPTAIN_LIVE_EVIDENCE_INPUT is not a file")
    return path


def test_recording_evidence_proves_provider_recovery_and_video_gates(
    record_property: Callable[[str, object], None],
) -> None:
    """Mocks cannot satisfy this gate; the configured export must say live."""

    try:
        raw = json.loads(_configured_input().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        pytest.fail(f"live evidence input is unreadable: {type(exc).__name__}")
    if not isinstance(raw, Mapping):
        pytest.fail("live evidence input must contain one JSON object")

    report = build_live_evidence_report(raw)

    assert set(report["gates"].values()) == {"passed"}
    record_property("correlation_id", report["correlation_id"])
    record_property("evidence_mode", report["mode"])
    record_property("video_gate_count", len(report["gates"]))
