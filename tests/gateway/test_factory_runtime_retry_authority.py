from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agenten.agent_factory.codex_build_execution import (
    FactoryCodexBuildInterruptionBindings,
)
from agenten.agent_factory.codex_build_recovery import (
    FactoryCodexBuildCheckpointV1,
    canonical_factory_codex_model,
)
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.factory_runtime_retry_authority import (
    CaptainRuntimeRetryAuthorizationIssuer,
    FilesystemFactoryRuntimeRetryAuthority,
)
from tests.agent_factory.test_state_machine import job_v3


NOW = datetime(2026, 7, 31, 20, 10, tzinfo=timezone.utc)


def _interrupted_fixture(tmp_path: Path, *, resume_ordinal: int = 0):
    job = job_v3(mode="demo").model_copy(
        update={"deadline_at": NOW + timedelta(hours=2)}
    )
    invocation_id = uuid4()
    idempotency_key = "8" * 64
    terminal_bytes = json.dumps(
        {
            "schema": "captain.codex-session-receipt.v1",
            "status": "timed_out",
            "exit_code": 124,
            "resume_ordinal": resume_ordinal,
            "process_cleanup_status": "verified_cancelled",
            "workspace_ref": "workspace://factory/retry/test",
            "base_revision": "a" * 40,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    terminal_sha = hashlib.sha256(terminal_bytes).hexdigest()
    checkpoint = FactoryCodexBuildCheckpointV1(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        attempt=1,
        invocation_id=invocation_id,
        workspace_ref="workspace://factory/retry/test",
        workspace_root=(tmp_path / "workspace").resolve(),
        base_revision="a" * 40,
        brief_sha256="b" * 64,
        scaffold_manifest_sha256="c" * 64,
        phase="implementation_interrupted",
        resume_ordinal=resume_ordinal,
        terminal_receipt_sha256=terminal_sha,
        parent_terminal_receipt_sha256=("1" * 64 if resume_ordinal > 0 else None),
        parent_journal_sha256=("2" * 64 if resume_ordinal > 0 else None),
        runtime_retry_authorization_uri=(
            "artifact://factory/runtime-retry/prior"
            if resume_ordinal > 0
            else None
        ),
        runtime_retry_authorization_sha256=(
            "3" * 64 if resume_ordinal > 0 else None
        ),
        runtime_retry_authorization_binding_sha256=(
            "4" * 64 if resume_ordinal > 0 else None
        ),
        runtime_retry_authorization_issued_at=(
            NOW - timedelta(minutes=5) if resume_ordinal > 0 else None
        ),
        runtime_retry_authorization_expires_at=(
            NOW + timedelta(minutes=5) if resume_ordinal > 0 else None
        ),
        updated_at=NOW - timedelta(minutes=1),
    )
    state_root = tmp_path / ".captain-cook" / "codex"
    checkpoint_path = state_root / "checkpoints" / f"{invocation_id.hex}.json"
    receipt_suffix = "" if resume_ordinal == 0 else f".resume-{resume_ordinal}"
    terminal_path = (
        state_root / "sessions" / f"{idempotency_key}{receipt_suffix}.json"
    )
    checkpoint_path.parent.mkdir(parents=True)
    terminal_path.parent.mkdir(parents=True)
    checkpoint_path.write_bytes(canonical_factory_codex_model(checkpoint))
    terminal_path.write_bytes(terminal_bytes)
    checkpoint_sha = hashlib.sha256(
        canonical_factory_codex_model(checkpoint)
    ).hexdigest()
    binding = FactoryCodexBuildInterruptionBindings(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=1,
        invocation_id=invocation_id,
        idempotency_key=idempotency_key,
        lease_id="factory-retry-lease",
        workspace_ref=checkpoint.workspace_ref,
        base_revision=checkpoint.base_revision,
        scaffold_manifest_sha256=checkpoint.scaffold_manifest_sha256,
        brief_sha256=checkpoint.brief_sha256,
    )
    return (
        job,
        state_root,
        checkpoint_path,
        terminal_path,
        binding,
        ArtifactRef(
            uri=f"artifact://factory/codex-checkpoint/{checkpoint_sha}",
            sha256=checkpoint_sha,
            media_type="application/json",
        ),
        ArtifactRef(
            uri=f"artifact://factory/codex-terminal-receipt/{terminal_sha}",
            sha256=terminal_sha,
            media_type="application/json",
        ),
    )


def _evidence_failure_fixture(tmp_path: Path):
    (
        job,
        state_root,
        checkpoint_path,
        terminal_path,
        binding,
        _,
        _,
    ) = _interrupted_fixture(tmp_path)
    terminal_bytes = json.dumps(
        {
            "schema": "captain.codex-session-error-receipt.v1",
            "status": "evidence_failed",
            "failure_kind": "record_size_limit_exceeded",
            "resume_ordinal": 0,
            "process_cleanup_status": "verified_cancelled",
            "workspace_ref": binding.workspace_ref,
            "base_revision": binding.base_revision,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    terminal_sha = hashlib.sha256(terminal_bytes).hexdigest()
    checkpoint = FactoryCodexBuildCheckpointV1.model_validate_json(
        checkpoint_path.read_bytes()
    ).model_copy(
        update={
            "phase": "implementation_failed",
            "terminal_receipt_sha256": terminal_sha,
            "implementation_failure_reason": "evidence_failure",
        }
    )
    checkpoint_path.write_bytes(canonical_factory_codex_model(checkpoint))
    terminal_path.write_bytes(terminal_bytes)
    checkpoint_sha = hashlib.sha256(
        canonical_factory_codex_model(checkpoint)
    ).hexdigest()
    return (
        job,
        state_root,
        checkpoint_path,
        terminal_path,
        binding,
        ArtifactRef(
            uri=f"artifact://factory/codex-checkpoint/{checkpoint_sha}",
            sha256=checkpoint_sha,
            media_type="application/json",
        ),
        ArtifactRef(
            uri=f"artifact://factory/codex-terminal-receipt/{terminal_sha}",
            sha256=terminal_sha,
            media_type="application/json",
        ),
    )


def test_captain_issues_and_loads_exact_interrupted_runtime_retry(
    tmp_path: Path,
) -> None:
    (
        job,
        state_root,
        checkpoint_path,
        terminal_path,
        binding,
        checkpoint_ref,
        terminal_ref,
    ) = _interrupted_fixture(tmp_path)
    authority_root = tmp_path / ".captain-cook" / "runtime-retries"
    issuer = CaptainRuntimeRetryAuthorizationIssuer(
        authority_root=authority_root,
        codex_state_root=state_root,
    )

    issued = issuer.issue(
        checkpoint_path=checkpoint_path,
        terminal_receipt_path=terminal_path,
        binding=binding,
        checkpoint_ref=checkpoint_ref,
        terminal_receipt_ref=terminal_ref,
        resume_ordinal=1,
        maximum_runtime_seconds=600,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=20),
    )
    authority = FilesystemFactoryRuntimeRetryAuthority(
        authority_root=authority_root,
        checkpoint_root=state_root / "checkpoints",
    )

    assert authority.active(
        job,
        FactoryAction(kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR, attempt=1),
        SimpleNamespace(job=job),
        NOW + timedelta(seconds=1),
    ) == issued
    assert issued.authorization_ref.uri.endswith(issued.authorization_ref.sha256)


def test_captain_issues_second_retry_from_resume_receipt(tmp_path: Path) -> None:
    (
        job,
        state_root,
        checkpoint_path,
        terminal_path,
        binding,
        checkpoint_ref,
        terminal_ref,
    ) = _interrupted_fixture(tmp_path, resume_ordinal=1)
    authority_root = tmp_path / ".captain-cook" / "runtime-retries"
    issuer = CaptainRuntimeRetryAuthorizationIssuer(
        authority_root=authority_root,
        codex_state_root=state_root,
    )

    issued = issuer.issue(
        checkpoint_path=checkpoint_path,
        terminal_receipt_path=terminal_path,
        binding=binding,
        checkpoint_ref=checkpoint_ref,
        terminal_receipt_ref=terminal_ref,
        resume_ordinal=2,
        maximum_runtime_seconds=600,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=20),
    )
    authority = FilesystemFactoryRuntimeRetryAuthority(
        authority_root=authority_root,
        checkpoint_root=state_root / "checkpoints",
    )

    assert authority.active(
        job,
        FactoryAction(kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR, attempt=1),
        SimpleNamespace(job=job),
        NOW + timedelta(seconds=1),
    ) == issued


def test_captain_issues_exact_record_limit_evidence_retry(tmp_path: Path) -> None:
    (
        job,
        state_root,
        checkpoint_path,
        terminal_path,
        binding,
        checkpoint_ref,
        terminal_ref,
    ) = _evidence_failure_fixture(tmp_path)
    authority_root = tmp_path / ".captain-cook" / "runtime-retries"
    issuer = CaptainRuntimeRetryAuthorizationIssuer(
        authority_root=authority_root,
        codex_state_root=state_root,
    )

    issued = issuer.issue(
        checkpoint_path=checkpoint_path,
        terminal_receipt_path=terminal_path,
        binding=binding,
        checkpoint_ref=checkpoint_ref,
        terminal_receipt_ref=terminal_ref,
        resume_ordinal=1,
        maximum_runtime_seconds=600,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=20),
    )
    authority = FilesystemFactoryRuntimeRetryAuthority(
        authority_root=authority_root,
        checkpoint_root=state_root / "checkpoints",
    )

    assert authority.active(
        job,
        FactoryAction(kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR, attempt=1),
        SimpleNamespace(job=job),
        NOW + timedelta(seconds=1),
    ) == issued


def test_captain_rejects_other_evidence_failure_kind(tmp_path: Path) -> None:
    (
        _,
        state_root,
        checkpoint_path,
        terminal_path,
        binding,
        checkpoint_ref,
        _,
    ) = _evidence_failure_fixture(tmp_path)
    payload = json.loads(terminal_path.read_bytes())
    payload["failure_kind"] = "invalid_json_object"
    changed = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    terminal_path.write_bytes(changed)
    checkpoint = FactoryCodexBuildCheckpointV1.model_validate_json(
        checkpoint_path.read_bytes()
    ).model_copy(
        update={"terminal_receipt_sha256": hashlib.sha256(changed).hexdigest()}
    )
    checkpoint_path.write_bytes(canonical_factory_codex_model(checkpoint))
    checkpoint_sha = hashlib.sha256(
        canonical_factory_codex_model(checkpoint)
    ).hexdigest()
    issuer = CaptainRuntimeRetryAuthorizationIssuer(
        authority_root=tmp_path / ".captain-cook" / "runtime-retries",
        codex_state_root=state_root,
    )

    with pytest.raises(ValueError, match="terminal receipt"):
        issuer.issue(
            checkpoint_path=checkpoint_path,
            terminal_receipt_path=terminal_path,
            binding=binding,
            checkpoint_ref=ArtifactRef(
                uri=f"artifact://factory/codex-checkpoint/{checkpoint_sha}",
                sha256=checkpoint_sha,
                media_type="application/json",
            ),
            terminal_receipt_ref=ArtifactRef(
                uri=("artifact://factory/codex-terminal-receipt/" + hashlib.sha256(changed).hexdigest()),
                sha256=hashlib.sha256(changed).hexdigest(),
                media_type="application/json",
            ),
            resume_ordinal=1,
            maximum_runtime_seconds=600,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=20),
        )


def test_retry_issuer_rejects_changed_terminal_or_checkpoint_binding(
    tmp_path: Path,
) -> None:
    (
        _,
        state_root,
        checkpoint_path,
        terminal_path,
        binding,
        checkpoint_ref,
        terminal_ref,
    ) = _interrupted_fixture(tmp_path)
    issuer = CaptainRuntimeRetryAuthorizationIssuer(
        authority_root=tmp_path / ".captain-cook" / "runtime-retries",
        codex_state_root=state_root,
    )
    terminal_path.write_bytes(b"changed")

    with pytest.raises(ValueError, match="terminal receipt"):
        issuer.issue(
            checkpoint_path=checkpoint_path,
            terminal_receipt_path=terminal_path,
            binding=binding,
            checkpoint_ref=checkpoint_ref,
            terminal_receipt_ref=terminal_ref,
            resume_ordinal=1,
            maximum_runtime_seconds=600,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=20),
        )
