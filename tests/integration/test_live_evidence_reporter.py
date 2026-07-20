from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest

from docs.live_evidence_reporter import (
    EvidenceRejected,
    build_live_evidence_report,
    write_live_evidence_report,
)


CORRELATION_ID = UUID("4d53b3a5-252d-4b67-bd4d-3168df61b46a")


def _raw_evidence() -> dict[str, object]:
    correlation_id = str(CORRELATION_ID)
    return {
        "schema": "captain.live-demo-evidence.v1",
        "mode": "provider-backed-live",
        "correlation_id": correlation_id,
        "runtime": {
            "correlation_id": correlation_id,
            "provider": "openai-codex",
            "session_ref": "artifact://codex/sessions/session-redacted",
            "outcome": "succeeded",
        },
        "gateway_release": {
            "correlation_id": correlation_id,
            "decision_ref": "artifact://gateway/release/decision-redacted",
            "decision": "accepted",
        },
        "n8n_execution": {
            "correlation_id": correlation_id,
            "workflow_ref": "artifact://n8n/workflows/workflow-redacted",
            "execution_ref": "artifact://n8n/executions/execution-redacted",
            "outcome": "succeeded",
        },
        "minibook_readback": {
            "correlation_id": correlation_id,
            "post_ref": "artifact://minibook/posts/post-redacted",
            "outcome": "read-back",
        },
        "recovery": {
            "correlation_id": correlation_id,
            "run_ref": "artifact://gateway/recovery/recovery-redacted",
            "expected_failure_observed": True,
            "outcome": "recovered",
        },
        "normal_runs": [
            {
                "correlation_id": correlation_id,
                "run_number": index,
                "run_ref": f"artifact://gateway/e2e/run-{index}",
                "outcome": "succeeded",
            }
            for index in range(1, 4)
        ],
    }


def test_report_proves_one_redacted_correlation_across_all_video_gates() -> None:
    report = build_live_evidence_report(_raw_evidence())

    assert report == {
        "schema": "captain.live-demo-report.v1",
        "mode": "provider-backed-live",
        "correlation_id": str(CORRELATION_ID),
        "gates": {
            "runtime": "passed",
            "gateway_release": "passed",
            "n8n_execution": "passed",
            "minibook_readback": "passed",
            "controlled_recovery": "passed",
            "three_normal_follow_up_runs": "passed",
        },
        "evidence_refs": {
            "runtime": "artifact://codex/sessions/session-redacted",
            "gateway_release": "artifact://gateway/release/decision-redacted",
            "n8n_execution": "artifact://n8n/executions/execution-redacted",
            "minibook_readback": "artifact://minibook/posts/post-redacted",
            "controlled_recovery": "artifact://gateway/recovery/recovery-redacted",
            "normal_runs": [
                "artifact://gateway/e2e/run-1",
                "artifact://gateway/e2e/run-2",
                "artifact://gateway/e2e/run-3",
            ],
        },
    }


def test_generator_writes_only_the_compact_redacted_report(tmp_path) -> None:
    output = tmp_path / "live-demo-report.json"

    report = write_live_evidence_report(_raw_evidence(), output)

    assert output.read_text(encoding="utf-8").endswith("\n")
    assert '"schema": "captain.live-demo-report.v1"' in output.read_text(encoding="utf-8")
    assert "workflow_ref" not in output.read_text(encoding="utf-8")
    assert report == build_live_evidence_report(_raw_evidence())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["n8n_execution"].update(correlation_id=str(UUID(int=1))), "correlation_id"),
        (lambda value: value.update(mode="deterministic-offline"), "provider-backed-live"),
        (lambda value: value["normal_runs"].pop(), "exactly three"),
        (lambda value: value["recovery"].update(expected_failure_observed=False), "expected failure"),
        (lambda value: value["gateway_release"].update(decision="blocked"), "accepted"),
    ],
)
def test_report_fails_closed_for_incomplete_or_mock_evidence(mutation, message: str) -> None:
    evidence = deepcopy(_raw_evidence())
    mutation(evidence)

    with pytest.raises(EvidenceRejected, match=message):
        build_live_evidence_report(evidence)


@pytest.mark.parametrize(
    "leak",
    [
        {"api_key": "super-secret"},
        {"note": "Bearer abcdefghijklmnopqrstuvwxyz"},
        {"workspace": "C:\\Users\\operator\\private"},
    ],
)
def test_report_rejects_secret_like_or_host_local_material(leak: dict[str, str]) -> None:
    evidence = _raw_evidence()
    evidence["runtime"].update(leak)

    with pytest.raises(EvidenceRejected, match="redaction"):
        build_live_evidence_report(evidence)
