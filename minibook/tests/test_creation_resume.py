from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from minibook.swarm.contracts import CreationFailure, CreationJobV1, CreationResultV1
from minibook.swarm.job_store import CreationConflictError, CreationJobStore
from minibook.swarm.runner import CreationRunner, StepOutcome


FIXTURE = Path(__file__).parents[2] / "tests/fixtures/contracts/minibook_creation_job.v1.json"


def creation_job(**updates: object) -> CreationJobV1:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload.update(updates)
    return CreationJobV1.model_validate(payload)


class ScriptedPipeline:
    steps = ("tool_resolution", "codex_build")
    effectful_steps = frozenset(steps)

    def __init__(self, store: CreationJobStore, *, crash_after_effect: str | None = None) -> None:
        self.store = store
        self.crash_after_effect = crash_after_effect
        self.calls = {step: 0 for step in self.steps}

    async def run_step(
        self,
        job: CreationJobV1,
        step: str,
        prior_snapshot: dict[str, Any],
        effect_key: str,
        accepted_effect: dict[str, Any] | None,
    ) -> StepOutcome:
        effect = accepted_effect
        if effect is None:
            self.calls[step] += 1
            effect = {"receipt_id": f"receipt-{step}"}
            self.store.record_external_effect(job.creation_job_id, effect_key, effect)
            if self.crash_after_effect == step:
                raise RuntimeError("simulated crash")
        snapshot = prior_snapshot | {step: effect["receipt_id"]}
        return StepOutcome(snapshot=snapshot, effect_receipt=effect)

    def assemble_result(self, job: CreationJobV1, snapshot: dict[str, Any]) -> CreationResultV1:
        ref = {
            "uri": "artifact://sha256/result",
            "sha256": "f" * 64,
            "media_type": "application/json",
        }
        return CreationResultV1(
            creation_job_id=job.creation_job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=job.attempt,
            status="succeeded",
            package_manifest_ref=ref,
            skill_usage_receipt_ref=ref,
        )


@pytest.mark.asyncio
async def test_restart_reuses_accepted_effect_without_duplicate_dispatch(tmp_path: Path) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    job = creation_job()
    store.submit(job)
    first_pipeline = ScriptedPipeline(store, crash_after_effect="tool_resolution")

    with pytest.raises(RuntimeError, match="simulated crash"):
        await CreationRunner(store, first_pipeline).run_slice(job.creation_job_id)

    resumed_pipeline = ScriptedPipeline(store)
    result = await CreationRunner(store, resumed_pipeline).run_slice(job.creation_job_id)
    assert result.status == "succeeded"
    assert resumed_pipeline.calls["tool_resolution"] == 0
    assert resumed_pipeline.calls["codex_build"] == 1


def test_identical_submit_is_idempotent_but_changed_content_conflicts(tmp_path: Path) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    job = creation_job()
    first = store.submit(job)
    replay = store.submit(job)
    assert first.replayed is False
    assert replay.replayed is True
    with pytest.raises(CreationConflictError):
        store.submit(creation_job(attempt=2))


@pytest.mark.asyncio
async def test_expired_job_finishes_blocked_before_any_effect(tmp_path: Path) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    job = creation_job(deadline_at="2020-01-01T00:00:00Z")
    store.submit(job)
    pipeline = ScriptedPipeline(store)
    result = await CreationRunner(store, pipeline).run_slice(job.creation_job_id)
    assert result.status == "blocked"
    assert result.failure == CreationFailure(code="deadline_expired", summary="creation deadline expired")
    assert pipeline.calls == {"tool_resolution": 0, "codex_build": 0}


@pytest.mark.asyncio
async def test_version_fenced_cancel_prevents_effects(tmp_path: Path) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    job = creation_job()
    receipt = store.submit(job)
    with pytest.raises(CreationConflictError):
        store.cancel(job.creation_job_id, expected_version=receipt.subject_version + 1)
    store.cancel(job.creation_job_id, expected_version=receipt.subject_version)
    pipeline = ScriptedPipeline(store)
    result = await CreationRunner(store, pipeline).run_slice(job.creation_job_id)
    assert result.status == "cancelled"
    assert pipeline.calls == {"tool_resolution": 0, "codex_build": 0}


def test_result_is_unavailable_until_terminal(tmp_path: Path) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    job = creation_job()
    store.submit(job)
    assert store.result(job.creation_job_id) is None
    progress = store.progress(job.creation_job_id)
    assert progress.status == "queued"
    assert progress.version == 1
