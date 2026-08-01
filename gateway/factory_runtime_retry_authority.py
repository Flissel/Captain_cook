"""Captain-owned persistence for exact Codex runtime retry authority."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from agenten.agent_factory.codex_build_execution import (
    FactoryCodexBuildInterruptionBindings,
)
from agenten.agent_factory.codex_build_recovery import (
    FactoryCodexBuildCheckpointV1,
    canonical_factory_codex_model,
)
from agenten.agent_factory.contracts import FactoryJob
from agenten.agent_factory.skill_sequence import FactoryRuntimeRetryAuthorizationV1
from agenten.agent_factory.skill_workflow_contracts import (
    factory_runtime_retry_evidence_binding,
    factory_runtime_retry_evidence_binding_sha256,
)
from agenten.agent_factory.state_machine import (
    FactoryAction,
    FactoryActionKind,
    FactoryProjection,
)
from agenten.agent_runtime.contracts import ArtifactRef


class CaptainRuntimeRetryAuthorizationIssuer:
    """Mint one bounded retry only from verified resumable local evidence."""

    def __init__(self, *, authority_root: Path, codex_state_root: Path) -> None:
        self._authority_root = _private_root(authority_root)
        self._codex_state_root = _private_root(codex_state_root)
        self._checkpoint_root = (self._codex_state_root / "checkpoints").resolve()
        self._session_root = (self._codex_state_root / "sessions").resolve()
        self._authority_root.mkdir(parents=True, exist_ok=True)

    def issue(
        self,
        *,
        checkpoint_path: Path,
        terminal_receipt_path: Path,
        binding: FactoryCodexBuildInterruptionBindings,
        checkpoint_ref: ArtifactRef,
        terminal_receipt_ref: ArtifactRef,
        resume_ordinal: int,
        maximum_runtime_seconds: int,
        issued_at: datetime,
        expires_at: datetime,
    ) -> FactoryRuntimeRetryAuthorizationV1:
        checkpoint_file = _exact_file(
            checkpoint_path,
            root=self._checkpoint_root,
            expected_name=f"{binding.invocation_id.hex}.json",
            label="checkpoint",
        )
        checkpoint = _read_checkpoint(checkpoint_file)
        receipt_suffix = (
            ""
            if checkpoint.resume_ordinal == 0
            else f".resume-{checkpoint.resume_ordinal}"
        )
        session_file = _exact_file(
            terminal_receipt_path,
            root=self._session_root,
            expected_name=f"{binding.idempotency_key}{receipt_suffix}.json",
            label="terminal receipt",
        )
        _require_resumable_binding(
            checkpoint=checkpoint,
            binding=binding,
            checkpoint_ref=checkpoint_ref,
            terminal_receipt_ref=terminal_receipt_ref,
            terminal_receipt_path=session_file,
            resume_ordinal=resume_ordinal,
        )
        _require_utc(issued_at, "retry issuance")
        _require_utc(expires_at, "retry expiry")
        if (
            expires_at <= issued_at
            or isinstance(maximum_runtime_seconds, bool)
            or maximum_runtime_seconds < 1
            or maximum_runtime_seconds > 900
            or maximum_runtime_seconds
            > int((expires_at - issued_at).total_seconds())
        ):
            raise ValueError("runtime retry authorization window is invalid")
        placeholder = ArtifactRef(
            uri=f"artifact://factory/runtime-retry/{'0' * 64}",
            sha256="0" * 64,
            media_type="application/json",
        )
        authorization = FactoryRuntimeRetryAuthorizationV1(
            schema="captain.factory-runtime-retry-authorization.v1",
            authorization_ref=placeholder,
            producer="captain",
            status="succeeded",
            job_id=binding.job_id,
            correlation_id=binding.correlation_id,
            subject_version=binding.subject_version,
            attempt=binding.attempt,
            invocation_id=binding.invocation_id,
            idempotency_key=binding.idempotency_key,
            lease_id=binding.lease_id,
            checkpoint_ref=checkpoint_ref,
            terminal_receipt_ref=terminal_receipt_ref,
            workspace_ref=binding.workspace_ref,
            base_revision=binding.base_revision,
            scaffold_manifest_sha256=binding.scaffold_manifest_sha256,
            brief_sha256=binding.brief_sha256,
            resume_ordinal=resume_ordinal,
            maximum_runtime_seconds=maximum_runtime_seconds,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        digest = factory_runtime_retry_evidence_binding_sha256(
            factory_runtime_retry_evidence_binding(authorization)
        )
        authorization = authorization.model_copy(
            update={
                "authorization_ref": ArtifactRef(
                    uri=f"artifact://factory/runtime-retry/{digest}",
                    sha256=digest,
                    media_type="application/json",
                )
            }
        )
        target = (
            self._authority_root
            / str(binding.job_id)
            / f"{resume_ordinal}-{digest}.json"
        ).resolve()
        _require_within(target, self._authority_root, "authorization")
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_once(target, canonical_factory_codex_model(authorization))
        return authorization


class FilesystemFactoryRuntimeRetryAuthority:
    """Load the one authority matching the current immutable checkpoint phase."""

    def __init__(self, *, authority_root: Path, checkpoint_root: Path) -> None:
        self._authority_root = _private_root(authority_root)
        self._checkpoint_root = _private_root(checkpoint_root)

    def active(
        self,
        job: FactoryJob,
        action: FactoryAction,
        projection: FactoryProjection,
        now: datetime,
    ) -> FactoryRuntimeRetryAuthorizationV1 | None:
        _require_utc(now, "runtime retry lookup")
        if (
            action.kind is not FactoryActionKind.DISPATCH_TOOL_INTEGRATOR
            or action.attempt < 1
            or projection.job != job
        ):
            return None
        directory = (self._authority_root / str(job.job_id)).resolve()
        _require_within(directory, self._authority_root, "authorization")
        if not directory.is_dir():
            return None
        authorizations = tuple(
            _read_authorization(path)
            for path in sorted(directory.glob("*.json"))
            if path.is_file()
        )
        matching: list[FactoryRuntimeRetryAuthorizationV1] = []
        for authorization in authorizations:
            if (
                authorization.job_id != job.job_id
                or authorization.correlation_id != job.correlation_id
                or authorization.subject_version != job.subject_version
                or authorization.attempt != action.attempt
            ):
                continue
            checkpoint_path = (
                self._checkpoint_root / f"{authorization.invocation_id.hex}.json"
            ).resolve()
            _require_within(checkpoint_path, self._checkpoint_root, "checkpoint")
            checkpoint = _read_checkpoint(checkpoint_path)
            if (
                checkpoint.job_id != job.job_id
                or checkpoint.correlation_id != job.correlation_id
                or checkpoint.attempt != action.attempt
            ):
                raise ValueError("runtime retry checkpoint job binding changed")
            if checkpoint.phase == "implementation_interrupted":
                eligible = (
                    authorization.resume_ordinal == checkpoint.resume_ordinal + 1
                    and authorization.issued_at <= now < authorization.expires_at
                )
            elif (
                checkpoint.phase == "implementation_failed"
                and checkpoint.implementation_failure_reason
                in {"evidence_failure", "required_output_invalid"}
            ):
                eligible = (
                    authorization.resume_ordinal == checkpoint.resume_ordinal + 1
                    and authorization.issued_at <= now < authorization.expires_at
                )
            elif checkpoint.phase in {
                "implementation_running",
                "implementation_complete",
                "sealed",
            }:
                eligible = authorization.resume_ordinal == checkpoint.resume_ordinal
            else:
                eligible = False
            if eligible:
                matching.append(authorization)
        if not matching:
            return None
        highest = max(item.resume_ordinal for item in matching)
        selected = tuple(item for item in matching if item.resume_ordinal == highest)
        if len(selected) != 1:
            raise ValueError("runtime retry authority is conflicting")
        return selected[0]


def _require_resumable_binding(
    *,
    checkpoint: FactoryCodexBuildCheckpointV1,
    binding: FactoryCodexBuildInterruptionBindings,
    checkpoint_ref: ArtifactRef,
    terminal_receipt_ref: ArtifactRef,
    terminal_receipt_path: Path,
    resume_ordinal: int,
) -> None:
    checkpoint_sha = hashlib.sha256(
        canonical_factory_codex_model(checkpoint)
    ).hexdigest()
    if (
        checkpoint.phase not in {"implementation_interrupted", "implementation_failed"}
        or (
            checkpoint.phase == "implementation_failed"
            and checkpoint.implementation_failure_reason
            not in {"evidence_failure", "required_output_invalid"}
        )
        or checkpoint.job_id != binding.job_id
        or checkpoint.correlation_id != binding.correlation_id
        or checkpoint.attempt != binding.attempt
        or checkpoint.invocation_id != binding.invocation_id
        or checkpoint.workspace_ref != binding.workspace_ref
        or checkpoint.base_revision != binding.base_revision
        or checkpoint.scaffold_manifest_sha256
        != binding.scaffold_manifest_sha256
        or checkpoint.brief_sha256 != binding.brief_sha256
        or resume_ordinal != checkpoint.resume_ordinal + 1
        or checkpoint_ref
        != ArtifactRef(
            uri=f"artifact://factory/codex-checkpoint/{checkpoint_sha}",
            sha256=checkpoint_sha,
            media_type="application/json",
        )
    ):
        raise ValueError("runtime retry checkpoint binding changed")
    terminal_bytes = terminal_receipt_path.read_bytes()
    terminal_sha = hashlib.sha256(terminal_bytes).hexdigest()
    expected_terminal = ArtifactRef(
        uri=f"artifact://factory/codex-terminal-receipt/{terminal_sha}",
        sha256=terminal_sha,
        media_type="application/json",
    )
    try:
        terminal = json.loads(terminal_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("runtime retry terminal receipt is invalid") from exc
    interrupted_terminal = (
        terminal.get("schema") == "captain.codex-session-receipt.v1"
        and terminal.get("status") in {"timed_out", "cancelled"}
        and terminal.get("exit_code") in {124, 130}
    )
    evidence_failure_terminal = (
        terminal.get("schema") == "captain.codex-session-error-receipt.v1"
        and terminal.get("status") == "evidence_failed"
        and terminal.get("failure_kind") == "record_size_limit_exceeded"
    )
    required_output_terminal = (
        checkpoint.implementation_failure_reason == "required_output_invalid"
        and terminal.get("schema") == "captain.codex-session-receipt.v1"
        and terminal.get("status") == "succeeded"
        and terminal.get("exit_code") == 0
    )
    if (
        checkpoint.terminal_receipt_sha256 != terminal_sha
        or terminal_receipt_ref != expected_terminal
        or not isinstance(terminal, dict)
        or not (
            interrupted_terminal
            or evidence_failure_terminal
            or required_output_terminal
        )
        or terminal.get("resume_ordinal") != checkpoint.resume_ordinal
        or terminal.get("process_cleanup_status") == "unresolved"
        or terminal.get("workspace_ref") != checkpoint.workspace_ref
        or terminal.get("base_revision") != checkpoint.base_revision
    ):
        raise ValueError("runtime retry terminal receipt binding changed")


def _private_root(path: Path) -> Path:
    resolved = path.resolve()
    if ".captain-cook" not in {part.casefold() for part in resolved.parts}:
        raise ValueError("runtime retry storage must use the private .captain-cook namespace")
    return resolved


def _exact_file(path: Path, *, root: Path, expected_name: str, label: str) -> Path:
    resolved = path.resolve()
    _require_within(resolved, root, label)
    if resolved.name != expected_name or not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"runtime retry {label} path is invalid")
    return resolved


def _require_within(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"runtime retry {label} path escapes its root") from exc


def _read_checkpoint(path: Path) -> FactoryCodexBuildCheckpointV1:
    try:
        return FactoryCodexBuildCheckpointV1.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError("runtime retry checkpoint is unavailable or invalid") from exc


def _read_authorization(path: Path) -> FactoryRuntimeRetryAuthorizationV1:
    try:
        return FactoryRuntimeRetryAuthorizationV1.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError("runtime retry authority is unavailable or invalid") from exc


def _write_once(path: Path, content: bytes) -> None:
    try:
        descriptor = os.open(
            path,
            getattr(os, "O_BINARY", 0) | os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValueError("runtime retry authority immutable binding changed")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{label} time must be UTC")


__all__ = [
    "CaptainRuntimeRetryAuthorizationIssuer",
    "FilesystemFactoryRuntimeRetryAuthority",
]
