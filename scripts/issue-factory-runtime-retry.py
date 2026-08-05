from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import UUID

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agenten.agent_factory.codex_build_execution import (
    FactoryCodexBuildInterruptionBindings,
)
from agenten.agent_factory.codex_build_recovery import (
    FactoryCodexBuildCheckpointV1,
    canonical_factory_codex_model,
)
from agenten.agent_factory.hermes_cli import load_factory_skill_replay_record
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.factory_runtime_retry_authority import (
    CaptainRuntimeRetryAuthorizationIssuer,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Issue one Captain-owned Codex retry from a canonical interruption "
            "checkpoint read from stdin or an exact durable runtime failure."
        )
    )
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--maximum-runtime-seconds", type=int, default=600)
    parser.add_argument("--authorization-window-seconds", type=int, default=1200)
    parser.add_argument(
        "--failure-job-id",
        "--evidence-failure-job-id",
        dest="failure_job_id",
        type=UUID,
    )
    parser.add_argument("--attempt", type=int)
    return parser


def _load_interruption(raw: str) -> tuple[
    FactoryCodexBuildInterruptionBindings,
    ArtifactRef,
    ArtifactRef,
    int,
]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("interruption checkpoint is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("interruption checkpoint must be an object")
    expected = {
        "schema",
        "database",
        "status",
        "exit_code",
        "reason",
        "checkpoint_ref",
        "terminal_receipt_ref",
        "next_resume_ordinal",
        "captain_authorization_binding",
    }
    if set(payload) != expected:
        raise ValueError("interruption checkpoint fields are not canonical")
    if (
        payload["schema"] != "captain.business-demo-factory-operator.v1"
        or payload["database"] != "captain_test"
        or payload["status"] != "codex_build_interrupted"
        or payload["reason"] not in {"codex_timed_out", "runtime_cancelled"}
        or payload["exit_code"] not in {124, 130}
        or payload["next_resume_ordinal"] not in {1, 2}
    ):
        raise ValueError("interruption checkpoint status is not retryable")
    if (
        (payload["reason"] == "codex_timed_out" and payload["exit_code"] != 124)
        or (payload["reason"] == "runtime_cancelled" and payload["exit_code"] != 130)
    ):
        raise ValueError("interruption reason and exit code do not match")
    binding_payload = payload["captain_authorization_binding"]
    binding_fields = {
        "job_id",
        "correlation_id",
        "subject_version",
        "attempt",
        "invocation_id",
        "idempotency_key",
        "lease_id",
        "workspace_ref",
        "base_revision",
        "scaffold_manifest_sha256",
        "brief_sha256",
    }
    if not isinstance(binding_payload, dict) or set(binding_payload) != binding_fields:
        raise ValueError("Captain authorization binding is invalid")
    try:
        binding = FactoryCodexBuildInterruptionBindings(
            job_id=UUID(binding_payload["job_id"]),
            correlation_id=UUID(binding_payload["correlation_id"]),
            subject_version=_positive_int(
                binding_payload["subject_version"], "subject version"
            ),
            attempt=_positive_int(binding_payload["attempt"], "attempt"),
            invocation_id=UUID(binding_payload["invocation_id"]),
            idempotency_key=_digest(
                binding_payload["idempotency_key"], "idempotency key"
            ),
            lease_id=_nonempty(binding_payload["lease_id"], "lease ID"),
            workspace_ref=_nonempty(
                binding_payload["workspace_ref"], "workspace reference"
            ),
            base_revision=_revision(binding_payload["base_revision"]),
            scaffold_manifest_sha256=_digest(
                binding_payload["scaffold_manifest_sha256"], "scaffold digest"
            ),
            brief_sha256=_digest(
                binding_payload["brief_sha256"], "brief digest"
            ),
        )
        checkpoint_ref = ArtifactRef.model_validate(payload["checkpoint_ref"])
        terminal_ref = ArtifactRef.model_validate(payload["terminal_receipt_ref"])
    except (TypeError, ValueError) as exc:
        raise ValueError("interruption evidence binding is invalid") from exc
    return (
        binding,
        checkpoint_ref,
        terminal_ref,
        payload["next_resume_ordinal"],
    )


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} is invalid")
    return value


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    text = _nonempty(value, label)
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise ValueError(f"{label} is invalid")
    return text


def _revision(value: object) -> str:
    text = _nonempty(value, "base revision")
    if re.fullmatch(r"[0-9a-f]{40,64}", text) is None:
        raise ValueError("base revision is invalid")
    return text


def _runtime_failure_reason(failure_kind: str) -> str:
    reasons = {
        "FactoryCodexEvidenceFailure": "evidence_failure",
        "FactoryCodexOutputCaptureError": "required_output_invalid",
        "FactoryDispatchError": "runtime_failed",
    }
    try:
        return reasons[failure_kind]
    except KeyError:
        raise ValueError("Codex terminal failure is not retryable") from None


def _matches_failed_checkpoint(
    *,
    failure_kind: str,
    checkpoint_reason: str | None,
    replay_resume_ordinal: int,
    checkpoint_resume_ordinal: int,
) -> bool:
    if failure_kind == "CodexPolicyViolation":
        return (
            checkpoint_reason == "required_output_invalid"
            and replay_resume_ordinal == checkpoint_resume_ordinal + 1
        )
    try:
        expected_reason = _runtime_failure_reason(failure_kind)
    except ValueError:
        return False
    return (
        checkpoint_reason == expected_reason
        and replay_resume_ordinal == checkpoint_resume_ordinal
    )


def _load_failed_attempt(
    workspace: Path,
    *,
    job_id: UUID,
    attempt: int,
) -> tuple[
    FactoryCodexBuildInterruptionBindings,
    ArtifactRef,
    ArtifactRef,
    int,
]:
    if isinstance(attempt, bool) or attempt < 1 or attempt > 5:
        raise ValueError("evidence failure attempt is invalid")
    state_root = (
        workspace
        / ".captain-cook"
        / "private"
        / "business-benchmarks"
        / "runtime-state"
    ).resolve()
    replay_root = state_root / "hermes-evidence" / "skill-replays"
    matches = []
    for path in replay_root.glob("*.json"):
        replay = load_factory_skill_replay_record(path)
        if (
            replay.invocation.job_id == job_id
            and replay.invocation.attempt == attempt
            and replay.invocation.step is FactorySkillStep.SEAL_CODEX_BUILD
            and replay.state == "failed"
            and replay.failure_kind
            in {
                "FactoryCodexEvidenceFailure",
                "FactoryCodexOutputCaptureError",
                "FactoryDispatchError",
                "CodexPolicyViolation",
            }
        ):
            matches.append(replay)
    if len(matches) != 1:
        raise ValueError("exactly one durable Codex runtime failure is required")
    replay = matches[0]
    invocation = replay.invocation
    checkpoint_path = (
        state_root
        / "codex"
        / "checkpoints"
        / f"{invocation.invocation_id.hex}.json"
    )
    try:
        checkpoint_bytes = checkpoint_path.read_bytes()
        checkpoint = FactoryCodexBuildCheckpointV1.model_validate_json(
            checkpoint_bytes
        )
    except (OSError, ValueError) as exc:
        raise ValueError("Codex runtime failure checkpoint is unavailable") from exc
    checkpoint_sha = hashlib.sha256(
        canonical_factory_codex_model(checkpoint)
    ).hexdigest()
    terminal_sha = checkpoint.terminal_receipt_sha256
    if (
        checkpoint.phase != "implementation_failed"
        or not _matches_failed_checkpoint(
            failure_kind=replay.failure_kind or "",
            checkpoint_reason=checkpoint.implementation_failure_reason,
            replay_resume_ordinal=replay.resume_ordinal,
            checkpoint_resume_ordinal=checkpoint.resume_ordinal,
        )
        or terminal_sha is None
    ):
        raise ValueError("Codex runtime failure checkpoint is not resumable")
    binding = FactoryCodexBuildInterruptionBindings(
        job_id=invocation.job_id,
        correlation_id=invocation.correlation_id,
        subject_version=invocation.subject_version,
        attempt=invocation.attempt,
        invocation_id=invocation.invocation_id,
        idempotency_key=invocation.idempotency_key,
        lease_id=invocation.lease.lease_id,
        workspace_ref=checkpoint.workspace_ref,
        base_revision=checkpoint.base_revision,
        scaffold_manifest_sha256=checkpoint.scaffold_manifest_sha256,
        brief_sha256=checkpoint.brief_sha256,
    )
    return (
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
        checkpoint.resume_ordinal + 1,
    )


def main() -> int:
    args = _parser().parse_args()
    if (
        args.maximum_runtime_seconds < 1
        or args.maximum_runtime_seconds > 900
        or args.authorization_window_seconds < args.maximum_runtime_seconds
        or args.authorization_window_seconds > 3600
    ):
        raise SystemExit("retry runtime or authorization window is invalid")
    workspace = args.workspace_root.resolve(strict=True)
    failure_mode = args.failure_job_id is not None or args.attempt is not None
    if failure_mode:
        if args.failure_job_id is None or args.attempt is None:
            raise SystemExit(
                "--failure-job-id and --attempt must be provided together"
            )
        binding, checkpoint_ref, terminal_ref, resume_ordinal = (
            _load_failed_attempt(
                workspace,
                job_id=args.failure_job_id,
                attempt=args.attempt,
            )
        )
    else:
        binding, checkpoint_ref, terminal_ref, resume_ordinal = _load_interruption(
            sys.stdin.read()
        )
    authority_root = (
        workspace
        / ".captain-cook"
        / "private"
        / "business-benchmarks"
        / "runtime-state"
    ).resolve()
    codex_state_root = authority_root / "codex"
    issuer = CaptainRuntimeRetryAuthorizationIssuer(
        authority_root=authority_root / "runtime-retry-authorizations",
        codex_state_root=codex_state_root,
    )
    issued_at = datetime.now(timezone.utc)
    authorization = issuer.issue(
        checkpoint_path=(
            codex_state_root / "checkpoints" / f"{binding.invocation_id.hex}.json"
        ),
        terminal_receipt_path=(
            codex_state_root
            / "sessions"
            / (
                f"{binding.idempotency_key}.json"
                if resume_ordinal == 1
                else f"{binding.idempotency_key}.resume-{resume_ordinal - 1}.json"
            )
        ),
        binding=binding,
        checkpoint_ref=checkpoint_ref,
        terminal_receipt_ref=terminal_ref,
        resume_ordinal=resume_ordinal,
        maximum_runtime_seconds=args.maximum_runtime_seconds,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=args.authorization_window_seconds),
    )
    print(
        json.dumps(
            {
                "schema": "captain.factory-runtime-retry-issued.v1",
                "status": "succeeded",
                "job_id": str(authorization.job_id),
                "attempt": authorization.attempt,
                "resume_ordinal": authorization.resume_ordinal,
                "maximum_runtime_seconds": authorization.maximum_runtime_seconds,
                "authorization_ref": authorization.authorization_ref.model_dump(
                    mode="json"
                ),
                "expires_at": authorization.expires_at.isoformat().replace(
                    "+00:00", "Z"
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
