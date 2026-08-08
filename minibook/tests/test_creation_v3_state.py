from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from minibook.swarm.contracts import FactoryBuildAssignmentV2, FactoryBuildEffectV2, CreationJobV3
from minibook.swarm.job_store import CreationConflictError, CreationJobStore


FIXTURE = Path(__file__).parents[2] / "tests/fixtures/contracts/minibook_creation_job.v3.json"


def job() -> CreationJobV3:
    return CreationJobV3.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def assignment(creation_job: CreationJobV3, version: int = 2) -> FactoryBuildAssignmentV2:
    payload = {
        "assignment_id": str(uuid4()), "creation_job_id": str(creation_job.creation_job_id),
        "factory_job_id": str(creation_job.factory_job_id), "correlation_id": str(creation_job.correlation_id),
        "subject_version": creation_job.subject_version, "checkpoint_version": version, "attempt": creation_job.attempt,
        "effect_key": "a" * 64, "workspace_ref": "workspace://captain/codex", "workspace_policy_sha256": creation_job.workspace_policy_ref.sha256,
        "required_agent_ids": list(creation_job.required_agent_ids), "ordered_handoffs": [x.model_dump(mode="json") for x in creation_job.ordered_handoffs],
        "tool_requirements": [x.model_dump(mode="json") for x in creation_job.tool_requirements], "allowed_output_roots": ["out"],
        "allowed_commands": [["python", "-m", "pytest"]], "limits": {"max_wall_clock_seconds": 30, "max_provider_cost_usd": 1.0, "max_retries": 0, "max_files": 10},
        "lease_ref": creation_job.lease_ref.model_dump(mode="json"), "deadline_at": creation_job.deadline_at.isoformat(),
    }
    return FactoryBuildAssignmentV2.model_validate(payload)


def test_v3_assignment_is_append_only_replayable_and_claimed_once(tmp_path: Path) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    creation_job = job()
    store.submit(creation_job)
    store.transition(creation_job.creation_job_id, "planning", expected_version=1)
    submitted_assignment = assignment(creation_job)
    published = store.publish_build_assignment(submitted_assignment)
    assert published == submitted_assignment
    assert store.publish_build_assignment(published) == published
    assert store.claim_build_assignment(published.assignment_id, "worker-a") == published
    assert store.claim_build_assignment(published.assignment_id, "worker-a") == published
    with pytest.raises(CreationConflictError, match="claimed"):
        store.claim_build_assignment(published.assignment_id, "worker-b")


def test_v3_state_survives_restart_and_rejects_effect_identity_drift(tmp_path: Path) -> None:
    path = tmp_path / "creation.sqlite3"
    store = CreationJobStore(path)
    creation_job = job()
    store.submit(creation_job)
    store.transition(creation_job.creation_job_id, "planning", expected_version=1)
    published = store.publish_build_assignment(assignment(creation_job))
    store.claim_build_assignment(published.assignment_id, "worker-a")
    effect = FactoryBuildEffectV2.model_validate({
        "assignment_id": str(published.assignment_id), "creation_job_id": str(creation_job.creation_job_id), "factory_job_id": str(creation_job.factory_job_id), "correlation_id": str(creation_job.correlation_id), "subject_version": creation_job.subject_version, "checkpoint_version": 2, "attempt": creation_job.attempt, "effect_key": "a" * 64,
        "hermes_usage_receipt_ref": {"uri":"artifact://x/"+"1"*64,"sha256":"1"*64,"media_type":"application/json"}, "codex_build_receipt_ref": {"uri":"artifact://x/"+"2"*64,"sha256":"2"*64,"media_type":"application/json"}, "codex_session_ref": {"uri":"artifact://x/"+"3"*64,"sha256":"3"*64,"media_type":"application/json"}, "workspace_snapshot_ref": {"uri":"artifact://x/"+"4"*64,"sha256":"4"*64,"media_type":"application/zip"}, "source_archive_ref": {"uri":"artifact://x/"+"5"*64,"sha256":"5"*64,"media_type":"application/zip"}, "preliminary_team_manifest_ref": {"uri":"artifact://x/"+"6"*64,"sha256":"6"*64,"media_type":"application/json"}, "build_evidence_refs":[{"uri":"artifact://x/"+"7"*64,"sha256":"7"*64,"media_type":"application/json"}], "test_evidence_refs":[{"uri":"artifact://x/"+"8"*64,"sha256":"8"*64,"media_type":"application/json"}],
    })
    assert store.accept_build_effect(effect) == effect
    assert CreationJobStore(path).build_effect(published.assignment_id) == effect
    with pytest.raises(CreationConflictError):
        store.accept_build_effect(effect.model_copy(update={"effect_key": "b" * 64}))
