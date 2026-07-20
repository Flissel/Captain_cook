"""Build a compact, fail-closed report from already captured live evidence.

The reporter never calls a provider or service.  Its input must be exported by
the explicitly opted-in live gates; it only validates linkage and redaction for
the recording artifact.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn
from uuid import UUID


class EvidenceRejected(ValueError):
    """The supplied material is incomplete, inconsistent, or not redacted."""


_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|authorization|password|secret|token|credential)", re.IGNORECASE
)
_SECRET_VALUE = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/-]{12,}", re.IGNORECASE)
_ABSOLUTE_PATH = re.compile(r"(?:\b[A-Za-z]:\\|/(?:home|Users|root)/)")


def _reject(message: str) -> NoReturn:
    raise EvidenceRejected(message)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _reject(f"{label} must be an object")
    return value


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _reject(f"{label} must be non-empty text")
    return value


def _require_ref(value: object, label: str) -> str:
    reference = _require_text(value, label)
    if not reference.startswith("artifact://"):
        _reject(f"{label} must be an opaque artifact reference")
    return reference


def _assert_redacted(value: object, *, path: str = "evidence") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or _SECRET_KEY.search(key):
                _reject(f"redaction violation at {path}: secret-like field")
            _assert_redacted(child, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _assert_redacted(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and (
        _SECRET_VALUE.search(value) or _ABSOLUTE_PATH.search(value)
    ):
        _reject(f"redaction violation at {path}: secret-like value or host path")


def _require_correlation(stage: Mapping[str, object], expected: str, label: str) -> None:
    candidate = _require_text(stage.get("correlation_id"), f"{label}.correlation_id")
    try:
        normalized = str(UUID(candidate))
    except ValueError:
        _reject(f"{label}.correlation_id must be a UUID")
    if normalized != expected:
        _reject(f"{label}.correlation_id does not match the recording correlation_id")


def _require_outcome(
    stage: Mapping[str, object], expected: str, label: str
) -> None:
    if stage.get("outcome") != expected:
        _reject(f"{label}.outcome must be {expected!r}")


def build_live_evidence_report(raw: Mapping[str, object]) -> dict[str, object]:
    """Validate live evidence and return only recording-safe proof pointers."""

    _assert_redacted(raw)
    if raw.get("schema") != "captain.live-demo-evidence.v1":
        _reject("unsupported live evidence schema")
    if raw.get("mode") != "provider-backed-live":
        _reject("mode must be provider-backed-live; mocks cannot satisfy this gate")

    correlation = _require_text(raw.get("correlation_id"), "correlation_id")
    try:
        correlation = str(UUID(correlation))
    except ValueError:
        _reject("correlation_id must be a UUID")

    runtime = _mapping(raw.get("runtime"), "runtime")
    gateway = _mapping(raw.get("gateway_release"), "gateway_release")
    n8n = _mapping(raw.get("n8n_execution"), "n8n_execution")
    minibook = _mapping(raw.get("minibook_readback"), "minibook_readback")
    recovery = _mapping(raw.get("recovery"), "recovery")
    stages = {
        "runtime": runtime,
        "gateway_release": gateway,
        "n8n_execution": n8n,
        "minibook_readback": minibook,
        "recovery": recovery,
    }
    for label, stage in stages.items():
        _require_correlation(stage, correlation, label)

    _require_outcome(runtime, "succeeded", "runtime")
    if gateway.get("decision") != "accepted":
        _reject("gateway_release.decision must be accepted")
    _require_outcome(n8n, "succeeded", "n8n_execution")
    _require_outcome(minibook, "read-back", "minibook_readback")
    if recovery.get("expected_failure_observed") is not True:
        _reject("recovery must include the expected failure")
    _require_outcome(recovery, "recovered", "recovery")

    normal_runs_value = raw.get("normal_runs")
    if not isinstance(normal_runs_value, list) or len(normal_runs_value) != 3:
        _reject("normal_runs must contain exactly three follow-up runs")
    normal_refs: list[str] = []
    for expected_number, value in enumerate(normal_runs_value, start=1):
        run = _mapping(value, f"normal_runs[{expected_number - 1}]")
        _require_correlation(run, correlation, f"normal_runs[{expected_number - 1}]")
        if run.get("run_number") != expected_number:
            _reject("normal_runs must be ordered consecutive runs 1, 2, 3")
        _require_outcome(run, "succeeded", f"normal_runs[{expected_number - 1}]")
        normal_refs.append(_require_ref(run.get("run_ref"), "normal run reference"))
    if len(set(normal_refs)) != 3:
        _reject("normal_runs must use three distinct evidence references")

    return {
        "schema": "captain.live-demo-report.v1",
        "mode": "provider-backed-live",
        "correlation_id": correlation,
        "gates": {
            "runtime": "passed",
            "gateway_release": "passed",
            "n8n_execution": "passed",
            "minibook_readback": "passed",
            "controlled_recovery": "passed",
            "three_normal_follow_up_runs": "passed",
        },
        "evidence_refs": {
            "runtime": _require_ref(runtime.get("session_ref"), "runtime.session_ref"),
            "gateway_release": _require_ref(
                gateway.get("decision_ref"), "gateway_release.decision_ref"
            ),
            "n8n_execution": _require_ref(
                n8n.get("execution_ref"), "n8n_execution.execution_ref"
            ),
            "minibook_readback": _require_ref(
                minibook.get("post_ref"), "minibook_readback.post_ref"
            ),
            "controlled_recovery": _require_ref(
                recovery.get("run_ref"), "recovery.run_ref"
            ),
            "normal_runs": normal_refs,
        },
    }


def write_live_evidence_report(
    raw: Mapping[str, object], output_path: str | Path
) -> dict[str, object]:
    """Validate first, then atomically replace the recording-safe report."""

    report = build_live_evidence_report(raw)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    raw = json.loads(arguments.input.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        _reject("live evidence input must contain one JSON object")
    write_live_evidence_report(raw, arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
