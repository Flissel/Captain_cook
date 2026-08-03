from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "issue-factory-runtime-retry.py"


def _module():
    spec = spec_from_file_location("issue_factory_runtime_retry", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload() -> dict[str, object]:
    return {
        "schema": "captain.business-demo-factory-operator.v1",
        "database": "captain_test",
        "status": "codex_build_interrupted",
        "exit_code": 124,
        "reason": "codex_timed_out",
        "checkpoint_ref": {
            "uri": f"artifact://factory/codex-checkpoint/{'a' * 64}",
            "sha256": "a" * 64,
            "media_type": "application/json",
        },
        "terminal_receipt_ref": {
            "uri": f"artifact://factory/codex-terminal-receipt/{'b' * 64}",
            "sha256": "b" * 64,
            "media_type": "application/json",
        },
        "next_resume_ordinal": 1,
        "captain_authorization_binding": {
            "job_id": "71000000-0000-0000-0000-000000000001",
            "correlation_id": "72000000-0000-0000-0000-000000000001",
            "subject_version": 3,
            "attempt": 1,
            "invocation_id": "73000000-0000-0000-0000-000000000001",
            "idempotency_key": "c" * 64,
            "lease_id": "factory-lease-1",
            "workspace_ref": "workspace://business-benchmark-factory-v3/job/tool/1/time",
            "base_revision": "d" * 40,
            "scaffold_manifest_sha256": "e" * 64,
            "brief_sha256": "f" * 64,
        },
    }


def test_retry_issuer_input_accepts_only_exact_retryable_checkpoint() -> None:
    module = _module()

    binding, checkpoint, terminal, ordinal = module._load_interruption(
        json.dumps(_payload())
    )

    assert binding.invocation_id.hex == "73000000000000000000000000000001"
    assert binding.idempotency_key == "c" * 64
    assert checkpoint.sha256 == "a" * 64
    assert terminal.sha256 == "b" * 64
    assert ordinal == 1


@pytest.mark.parametrize(
    ("field", "value"),
    (("reason", "resume_authorization_required"), ("exit_code", 0)),
)
def test_retry_issuer_input_rejects_nonterminal_or_mismatched_evidence(
    field: str, value: object
) -> None:
    module = _module()
    payload = _payload()
    payload[field] = value

    with pytest.raises(ValueError, match="not retryable|do not match"):
        module._load_interruption(json.dumps(payload))


def test_retry_issuer_input_rejects_malformed_captain_binding() -> None:
    module = _module()
    payload = _payload()
    binding = payload["captain_authorization_binding"]
    assert isinstance(binding, dict)
    binding["idempotency_key"] = "not-a-digest"

    with pytest.raises(ValueError, match="invalid"):
        module._load_interruption(json.dumps(payload))


@pytest.mark.parametrize(
    ("failure_kind", "expected_reason"),
    (
        ("FactoryCodexEvidenceFailure", "evidence_failure"),
        ("FactoryCodexOutputCaptureError", "required_output_invalid"),
        ("FactoryDispatchError", "runtime_failed"),
    ),
)
def test_retry_issuer_maps_only_bounded_terminal_failure_kinds(
    failure_kind: str, expected_reason: str
) -> None:
    module = _module()

    assert module._runtime_failure_reason(failure_kind) == expected_reason


def test_retry_issuer_rejects_unrecognized_terminal_failure_kind() -> None:
    module = _module()

    with pytest.raises(ValueError, match="not retryable"):
        module._runtime_failure_reason("UnexpectedFailure")
