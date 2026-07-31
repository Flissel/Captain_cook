from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenten.agent_factory.codex_build_recovery import (
    FactoryCodexBuildCheckpointV1,
    FilesystemFactoryCodexBuildCheckpointStore,
)
from agenten.agent_factory.orchestration import FactoryDispatchError
from tests.agent_factory.test_codex_build_execution import (
    _executor_job_and_brief,
    _seal_invocation,
)


NOW = datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc)


def _bound_checkpoint(
    tmp_path: Path,
    *,
    phase: str = "scaffold_ready",
    resume_ordinal: int = 0,
    terminal_receipt_sha256: str | None = None,
    parent_terminal_receipt_sha256: str | None = None,
    parent_journal_sha256: str | None = None,
    parent_codex_thread_id: str | None = None,
    updated_at: datetime = NOW,
) -> tuple[FactoryCodexBuildCheckpointV1, object]:
    job, brief, _artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    if phase in {
        "implementation_interrupted",
        "implementation_complete",
        "implementation_failed",
        "sealed",
    } and terminal_receipt_sha256 is None:
        terminal_receipt_sha256 = "b" * 64
    sealed_bindings = (
        {
            "sealed_evidence_sha256": "1" * 64,
            "sealed_build_receipt_uri": "artifact://sealed/original",
            "sealed_build_receipt_sha256": "2" * 64,
        }
        if phase == "sealed"
        else {}
    )
    output_manifest_sha256 = "5" * 64
    output_bindings = (
        {
            "output_manifest_uri": (
                "artifact://factory/codex-output-manifest/"
                f"{output_manifest_sha256}"
            ),
            "output_manifest_sha256": output_manifest_sha256,
        }
        if phase in {"implementation_complete", "sealed"}
        else {}
    )
    failure_bindings = (
        {"implementation_failure_reason": "required_output_invalid"}
        if phase == "implementation_failed"
        else {}
    )
    checkpoint = FactoryCodexBuildCheckpointV1(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        attempt=invocation.attempt,
        invocation_id=invocation.invocation_id,
        workspace_ref=brief.build_assignment.workspace_ref,
        workspace_root=(tmp_path / "workspace").resolve(),
        base_revision="a" * 40,
        brief_sha256=brief.artifact_ref.sha256,
        scaffold_manifest_sha256="f" * 64,
        phase=phase,
        resume_ordinal=resume_ordinal,
        terminal_receipt_sha256=terminal_receipt_sha256,
        parent_terminal_receipt_sha256=parent_terminal_receipt_sha256,
        parent_journal_sha256=parent_journal_sha256,
        parent_codex_thread_id=parent_codex_thread_id,
        updated_at=updated_at,
        **output_bindings,
        **failure_bindings,
        **sealed_bindings,
    )
    return checkpoint, invocation


def _next(
    checkpoint: FactoryCodexBuildCheckpointV1,
    *,
    phase: str,
    seconds: int,
    resume_ordinal: int | None = None,
    terminal_receipt_sha256: str | None = None,
    parent_terminal_receipt_sha256: str | None = None,
    parent_journal_sha256: str | None = None,
    parent_codex_thread_id: str | None = None,
) -> FactoryCodexBuildCheckpointV1:
    updates = {
            "phase": phase,
            "resume_ordinal": (
                checkpoint.resume_ordinal
                if resume_ordinal is None
                else resume_ordinal
            ),
            "terminal_receipt_sha256": terminal_receipt_sha256,
            "parent_terminal_receipt_sha256": (
                checkpoint.parent_terminal_receipt_sha256
                if parent_terminal_receipt_sha256 is None
                else parent_terminal_receipt_sha256
            ),
            "parent_journal_sha256": (
                checkpoint.parent_journal_sha256
                if parent_journal_sha256 is None
                else parent_journal_sha256
            ),
            "parent_codex_thread_id": (
                checkpoint.parent_codex_thread_id
                if parent_codex_thread_id is None
                else parent_codex_thread_id
            ),
            "updated_at": checkpoint.updated_at + timedelta(seconds=seconds),
        }
    effective_ordinal = updates["resume_ordinal"]
    if isinstance(effective_ordinal, int) and effective_ordinal > 0:
        updates.update(
            {
                "runtime_retry_authorization_uri": "artifact://factory/runtime-retry/test",
                "runtime_retry_authorization_sha256": "3" * 64,
                "runtime_retry_authorization_binding_sha256": "4" * 64,
                "runtime_retry_authorization_issued_at": NOW,
                "runtime_retry_authorization_expires_at": NOW + timedelta(minutes=1),
            }
        )
    if phase == "sealed":
        updates.update(
            {
                "sealed_evidence_sha256": "1" * 64,
                "sealed_build_receipt_uri": "artifact://sealed/original",
                "sealed_build_receipt_sha256": "2" * 64,
            }
        )
    if phase in {"implementation_complete", "sealed"}:
        digest = checkpoint.output_manifest_sha256 or "5" * 64
        updates.update(
            {
                "output_manifest_uri": (
                    "artifact://factory/codex-output-manifest/"
                    f"{digest}"
                ),
                "output_manifest_sha256": digest,
            }
        )
    else:
        updates.update(
            {
                "output_manifest_uri": None,
                "output_manifest_sha256": None,
            }
        )
    if phase == "implementation_failed":
        updates["implementation_failure_reason"] = "required_output_invalid"
    else:
        updates["implementation_failure_reason"] = None
    return checkpoint.model_copy(update=updates)


def test_checkpoint_store_advances_full_monotonic_lifecycle_and_replays_target(
    tmp_path: Path,
) -> None:
    store = FilesystemFactoryCodexBuildCheckpointStore(tmp_path / "checkpoints")
    scaffold, invocation = _bound_checkpoint(tmp_path)
    running = _next(scaffold, phase="implementation_running", seconds=1)
    interrupted = _next(
        running,
        phase="implementation_interrupted",
        seconds=1,
        terminal_receipt_sha256="b" * 64,
    )
    resumed = _next(
        interrupted,
        phase="implementation_running",
        seconds=1,
        resume_ordinal=1,
        parent_terminal_receipt_sha256="b" * 64,
        parent_journal_sha256="d" * 64,
        parent_codex_thread_id="019f0000-0000-7000-8000-000000000001",
    )
    complete = _next(
        resumed,
        phase="implementation_complete",
        seconds=1,
        terminal_receipt_sha256="c" * 64,
    )
    sealed = _next(
        complete,
        phase="sealed",
        seconds=1,
        terminal_receipt_sha256="c" * 64,
    )

    assert store.load(invocation) is None
    assert store.advance(None, scaffold) == scaffold
    assert store.advance(scaffold, running) == running
    assert store.advance(running, interrupted) == interrupted
    assert store.advance(interrupted, resumed) == resumed
    assert store.advance(resumed, complete) == complete
    assert store.advance(complete, sealed) == sealed
    assert store.advance(sealed, sealed) == sealed
    assert store.load(invocation) == sealed


def test_resume_checkpoint_binds_exact_prior_receipt_journal_and_thread_lineage(
    tmp_path: Path,
) -> None:
    store = FilesystemFactoryCodexBuildCheckpointStore(tmp_path / "checkpoints")
    scaffold, _ = _bound_checkpoint(tmp_path)
    running = _next(scaffold, phase="implementation_running", seconds=1)
    interrupted = _next(
        running,
        phase="implementation_interrupted",
        seconds=1,
        terminal_receipt_sha256="b" * 64,
    )
    resumed = _next(
        interrupted,
        phase="implementation_running",
        seconds=1,
        resume_ordinal=1,
        parent_terminal_receipt_sha256="b" * 64,
        parent_journal_sha256="c" * 64,
        parent_codex_thread_id="019f0000-0000-7000-8000-000000000001",
    )
    completed = _next(
        resumed,
        phase="implementation_complete",
        seconds=1,
        terminal_receipt_sha256="d" * 64,
    )

    store.advance(None, scaffold)
    store.advance(scaffold, running)
    store.advance(running, interrupted)
    store.advance(interrupted, resumed)
    store.advance(resumed, completed)

    assert completed.parent_terminal_receipt_sha256 == "b" * 64
    assert completed.parent_journal_sha256 == "c" * 64
    assert (
        completed.parent_codex_thread_id
        == "019f0000-0000-7000-8000-000000000001"
    )


def test_resume_checkpoint_rejects_missing_or_changed_parent_lineage(
    tmp_path: Path,
) -> None:
    store = FilesystemFactoryCodexBuildCheckpointStore(tmp_path / "checkpoints")
    scaffold, _ = _bound_checkpoint(tmp_path)
    running = _next(scaffold, phase="implementation_running", seconds=1)
    interrupted = _next(
        running,
        phase="implementation_interrupted",
        seconds=1,
        terminal_receipt_sha256="b" * 64,
    )
    store.advance(None, scaffold)
    store.advance(scaffold, running)
    store.advance(running, interrupted)

    missing = _next(
        interrupted,
        phase="implementation_running",
        seconds=1,
        resume_ordinal=1,
    )
    with pytest.raises(FactoryDispatchError, match="checkpoint.*invalid|parent.*lineage"):
        store.advance(
            interrupted,
            missing,
        )

    resumed = _next(
        interrupted,
        phase="implementation_running",
        seconds=1,
        resume_ordinal=1,
        parent_terminal_receipt_sha256="b" * 64,
        parent_journal_sha256="c" * 64,
    )
    store.advance(interrupted, resumed)
    changed = _next(
        resumed,
        phase="implementation_complete",
        seconds=1,
        terminal_receipt_sha256="d" * 64,
    ).model_copy(update={"parent_journal_sha256": "e" * 64})

    with pytest.raises(FactoryDispatchError, match="lineage"):
        store.advance(resumed, changed)


@pytest.mark.parametrize(
    ("initial_phase", "next_phase"),
    (
        ("implementation_running", None),
        ("scaffold_ready", "implementation_complete"),
        ("scaffold_ready", "sealed"),
        ("implementation_running", "sealed"),
        ("implementation_interrupted", "implementation_complete"),
        ("implementation_complete", "implementation_running"),
        ("sealed", "implementation_complete"),
    ),
)
def test_checkpoint_store_rejects_skipped_backward_and_non_scaffold_initial_states(
    tmp_path: Path,
    initial_phase: str,
    next_phase: str | None,
) -> None:
    store = FilesystemFactoryCodexBuildCheckpointStore(tmp_path / "checkpoints")
    checkpoint, _invocation = _bound_checkpoint(tmp_path, phase=initial_phase)

    if next_phase is None:
        previous = None
        target = checkpoint
    else:
        scaffold, _ = _bound_checkpoint(tmp_path)
        store.advance(None, scaffold)
        previous = scaffold
        if initial_phase != "scaffold_ready":
            previous = checkpoint
            path = next((tmp_path / "checkpoints").glob("*.json"))
            path.write_bytes(
                checkpoint.model_dump_json().encode("utf-8")
            )
        target = _next(previous, phase=next_phase, seconds=1)

    with pytest.raises(FactoryDispatchError, match="checkpoint"):
        store.advance(previous, target)


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("job_id", "other"),
        ("correlation_id", "other"),
        ("attempt", 2),
        ("invocation_id", "other"),
        ("workspace_ref", "workspace://factory/changed"),
        ("workspace_root", "changed"),
        ("base_revision", "d" * 40),
        ("brief_sha256", "e" * 64),
    ),
)
def test_checkpoint_store_rejects_changed_immutable_binding(
    tmp_path: Path,
    field: str,
    changed: object,
) -> None:
    from uuid import uuid4

    store = FilesystemFactoryCodexBuildCheckpointStore(tmp_path / "checkpoints")
    scaffold, _invocation = _bound_checkpoint(tmp_path)
    store.advance(None, scaffold)
    if changed == "other":
        changed = uuid4()
    elif changed == "changed":
        changed = (tmp_path / "other-workspace").resolve()
    target = _next(scaffold, phase="implementation_running", seconds=1).model_copy(
        update={field: changed}
    )

    with pytest.raises(FactoryDispatchError, match="binding"):
        store.advance(scaffold, target)


def test_checkpoint_store_compares_exact_previous_bytes_before_replacement(
    tmp_path: Path,
) -> None:
    store = FilesystemFactoryCodexBuildCheckpointStore(tmp_path / "checkpoints")
    scaffold, invocation = _bound_checkpoint(tmp_path)
    running = _next(scaffold, phase="implementation_running", seconds=1)
    interrupted = _next(
        running,
        phase="implementation_interrupted",
        seconds=1,
        terminal_receipt_sha256="b" * 64,
    )
    complete = _next(
        running,
        phase="implementation_complete",
        seconds=1,
        terminal_receipt_sha256="c" * 64,
    )
    store.advance(None, scaffold)
    store.advance(scaffold, running)
    store.advance(running, interrupted)

    with pytest.raises(FactoryDispatchError, match="previous"):
        store.advance(running, complete)

    assert store.load(invocation) == interrupted
