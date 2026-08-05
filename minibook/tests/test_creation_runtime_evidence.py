from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from minibook.swarm.contracts import (
    ArtifactRef,
    CreationCompletionEvidenceV1,
    CreationJobV1,
    CreationPreparationEvidenceV1,
    CreationResumeGrantV1,
)
from minibook.swarm.creation_runtime import BackgroundCreationRuntime
from minibook.swarm.job_store import CreationConflictError, CreationJobStore, CreationNotFoundError
from minibook.swarm.pipeline_adapter import SwarmPipelineAdapter, SwarmStep
from minibook.tests.test_creation_evidence_api import (
    _completion_payload,
    _preparation_payload,
    _result,
)


FIXTURE = Path(__file__).parents[2] / "tests/fixtures/contracts/minibook_creation_job.v1.json"


def _job() -> CreationJobV1:
    return CreationJobV1.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


class LegacyPipeline:
    async def step_swarm_manager(self, session: object, input_uri: str) -> str:
        del session, input_uri
        return "manager-complete"

    def __getattr__(self, name: str):
        if not name.startswith("step_"):
            raise AttributeError(name)

        async def step(session: object) -> str:
            del session
            return f"{name}-complete"

        return step


@pytest.mark.asyncio
async def test_manager_receives_resolved_canonical_task_when_pipeline_exposes_one() -> None:
    received: list[str] = []

    class TaskPipeline(LegacyPipeline):
        task_name = "# Canonical sales-agent brief\n\nBuild the requested team."

        async def step_swarm_manager(self, session: object, task: str) -> str:
            del session
            received.append(task)
            return "manager-complete"

    job = _job()
    adapter = SwarmPipelineAdapter(
        lambda _snapshot: TaskPipeline(),
        session=object(),
    )
    await adapter.run_step(
        job,
        SwarmStep.MANAGER.value,
        {},
        "manager-effect",
        None,
    )

    assert received == [TaskPipeline.task_name]


@pytest.mark.asyncio
async def test_architect_coder_and_reviewer_keep_legacy_conversation_parallel() -> None:
    events: list[str] = []
    architect_started = asyncio.Event()

    class ConversationPipeline:
        async def step_architect(self, session: object) -> str:
            del session
            events.append("architect")
            architect_started.set()
            await asyncio.sleep(0)
            return "architect"

        async def step_coder(self, session: object) -> str:
            del session
            await architect_started.wait()
            events.append("coder")
            return "coder"

        async def step_reviewer(self, session: object) -> str:
            del session
            await architect_started.wait()
            events.append("reviewer")
            return "reviewer"

    job = _job()
    pipeline = ConversationPipeline()
    adapter = SwarmPipelineAdapter(lambda _snapshot: pipeline, session=object())
    architect = await adapter._dispatch(pipeline, SwarmStep.ARCHITECT, job)
    coder = await adapter._dispatch(pipeline, SwarmStep.CODER, job)
    reviewer = await adapter._dispatch(pipeline, SwarmStep.REVIEWER, job)

    assert (architect, coder, reviewer) == ("architect", "coder", "reviewer")
    assert events[0] == "architect"
    assert set(events[1:]) == {"coder", "reviewer"}


@pytest.mark.asyncio
async def test_adapter_rejects_a_legacy_timeout_as_a_completed_checkpoint() -> None:
    from minibook.swarm.pipeline_adapter import LegacySwarmStepIncomplete

    class TimedOutPipeline:
        completed_steps: set[str] = set()

        async def step_coder(self, session: object) -> None:
            del session

        async def step_reviewer(self, session: object) -> None:
            del session

    job = _job()
    adapter = SwarmPipelineAdapter(lambda _snapshot: TimedOutPipeline(), session=object())

    with pytest.raises(LegacySwarmStepIncomplete, match="CoderAgent"):
        await adapter.run_step(job, SwarmStep.CODER.value, {}, "coder-effect", None)


class EvidenceSnapshotter:
    def capture(
        self,
        job: CreationJobV1,
        step: SwarmStep,
        pipeline: Any,
        output: Any,
        prior: Any,
    ) -> dict[str, Any]:
        del job, pipeline, output, prior
        digit = f"{list(SwarmStep).index(step) + 1:x}"
        state_ref = ArtifactRef(
            uri=f"artifact://capability-factory/pipeline/{digit * 64}",
            sha256=digit * 64,
            media_type="application/json",
        )
        updates: dict[str, Any] = {"pipeline_state_ref": state_ref}
        if step is SwarmStep.EXPORT:
            updates.update(
                {
                    "package_manifest_ref": ArtifactRef(
                        uri=f"artifact://capability-factory/package/{'e' * 64}",
                        sha256="e" * 64,
                        media_type="application/json",
                    ),
                    "skill_usage_receipt_ref": ArtifactRef(
                        uri=f"artifact://capability-factory/skill/{'f' * 64}",
                        sha256="f" * 64,
                        media_type="application/json",
                    ),
                }
            )
        return updates


class PipelineFactory:
    @asynccontextmanager
    async def open(self, job: CreationJobV1):
        del job
        yield SwarmPipelineAdapter(
            lambda _snapshot: LegacyPipeline(),
            session=object(),
            snapshotter=EvidenceSnapshotter(),
        )


async def _wait_for_completion(store: CreationJobStore, job: CreationJobV1) -> None:
    import asyncio

    for _ in range(200):
        if store.result(job.creation_job_id) is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("creation result was not persisted")


@pytest.mark.asyncio
async def test_background_runtime_persists_real_step_evidence_with_success_result(
    tmp_path: Path,
) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    job = _job()
    store.submit(job)
    runtime = BackgroundCreationRuntime(store, PipelineFactory())

    await runtime.start()
    architect = None
    for _ in range(200):
        try:
            architect = store.architect(job.creation_job_id)
        except CreationNotFoundError:
            pass
        if architect is not None and store.progress(job.creation_job_id).status == "awaiting_tool_integrator":
            break
        await asyncio.sleep(0.01)
    assert architect is not None and architect.lease_id == job.architect_lease_id
    assert store.resume(
        CreationResumeGrantV1(
            creation_job_id=job.creation_job_id,
            tool_integrator_lease_id="captain-tool-integrator-lease-1",
        )
    ) is False
    runtime.schedule(job.creation_job_id)
    await _wait_for_completion(store, job)
    await runtime.stop()

    result = store.result(job.creation_job_id)
    assert result is not None and result.status == "succeeded", result
    preparation = store.preparation(job.creation_job_id)
    completion = store.completion(job.creation_job_id)

    assert tuple(block.phase for block in preparation.blocks) == (
        "blueprint_created",
        "tool_candidate_tested",
    )
    assert preparation.blocks[0].evidence_refs[0].sha256 == "3" * 64
    assert preparation.blocks[1].evidence_refs[0].sha256 == "c" * 64
    assert completion.result == result
    assert completion.block.evidence_refs == (
        result.package_manifest_ref,
        result.skill_usage_receipt_ref,
    )
    assert not preparation.blocks[0].assertion_ids
    assert not preparation.blocks[1].assertion_ids
    assert not completion.block.assertion_ids
    assert preparation.blocks[1].lease_id == "captain-tool-integrator-lease-1"
    assert completion.block.lease_id == "captain-tool-integrator-lease-1"


def test_finish_with_completion_rolls_back_result_when_evidence_binding_is_invalid(
    tmp_path: Path,
) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    job = _job()
    result = _result()
    store.submit(job)
    store.record_preparation(
        CreationPreparationEvidenceV1.model_validate(_preparation_payload())
    )
    payload = _completion_payload(result)
    payload["block"]["job_id"] = "99999999-9999-4999-8999-999999999999"  # type: ignore[index]
    completion = CreationCompletionEvidenceV1.model_validate(payload)

    with pytest.raises(CreationConflictError, match="bound"):
        store.finish_with_completion(result, completion)

    assert store.result(job.creation_job_id) is None
