from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from minibook.swarm.contracts import (
    CreationFailure,
    CreationJobV3,
    CreationResultV2,
    FactoryActionRequestV1,
    FactoryActionResponseV1,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
)
from minibook.swarm.job_store import CreationConflictError, CreationJobStore


FIXTURES = Path(__file__).parents[2] / "tests/fixtures/contracts"


def job(**updates: object) -> CreationJobV3:
    payload = json.loads((FIXTURES / "minibook_creation_job.v3.json").read_text(encoding="utf-8"))
    payload.update({"deadline_at": "2099-08-09T12:00:00Z"} | updates)
    return CreationJobV3.model_validate(payload)


def assignment(creation_job: CreationJobV3, version: int = 2, **updates: object) -> FactoryBuildAssignmentV2:
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
    payload.update(updates)
    return FactoryBuildAssignmentV2.model_validate(payload)


def effect(published: FactoryBuildAssignmentV2, **updates: object) -> FactoryBuildEffectV2:
    payload = {
        "assignment_id": str(published.assignment_id), "creation_job_id": str(published.creation_job_id), "factory_job_id": str(published.factory_job_id), "correlation_id": str(published.correlation_id), "subject_version": published.subject_version, "checkpoint_version": published.checkpoint_version, "attempt": published.attempt, "effect_key": published.effect_key,
        "hermes_usage_receipt_ref": {"uri":"artifact://x/"+"1"*64,"sha256":"1"*64,"media_type":"application/json"}, "codex_build_receipt_ref": {"uri":"artifact://x/"+"2"*64,"sha256":"2"*64,"media_type":"application/json"}, "codex_session_ref": {"uri":"artifact://x/"+"3"*64,"sha256":"3"*64,"media_type":"application/json"}, "workspace_snapshot_ref": {"uri":"artifact://x/"+"4"*64,"sha256":"4"*64,"media_type":"application/zip"}, "source_archive_ref": {"uri":"artifact://x/"+"5"*64,"sha256":"5"*64,"media_type":"application/zip"}, "preliminary_team_manifest_ref": {"uri":"artifact://x/"+"6"*64,"sha256":"6"*64,"media_type":"application/json"}, "build_evidence_refs":[{"uri":"artifact://x/"+"7"*64,"sha256":"7"*64,"media_type":"application/json"}], "test_evidence_refs":[{"uri":"artifact://x/"+"8"*64,"sha256":"8"*64,"media_type":"application/json"}],
    }
    payload.update(updates)
    return FactoryBuildEffectV2.model_validate(payload)


def action_request(creation_job: CreationJobV3, **updates: object) -> FactoryActionRequestV1:
    payload = json.loads((FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8"))
    payload.update({
        "request_id": str(uuid4()), "creation_job_id": str(creation_job.creation_job_id),
        "factory_job_id": str(creation_job.factory_job_id), "correlation_id": str(creation_job.correlation_id),
        "expected_subject_version": creation_job.subject_version, "deadline_at": "2099-08-09T12:00:00Z",
    } | updates)
    return FactoryActionRequestV1.model_validate(payload)


def action_response(request: FactoryActionRequestV1, **updates: object) -> FactoryActionResponseV1:
    payload = json.loads((FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8"))
    payload.update({
        "request_id": str(request.request_id), "expected_subject_version": request.expected_subject_version,
        "approved_scope": request.required_scope, "answered_at": "2099-08-08T12:00:00Z",
    } | updates)
    return FactoryActionResponseV1.model_validate(payload)


def prepare_assignment(store: CreationJobStore, creation_job: CreationJobV3) -> FactoryBuildAssignmentV2:
    store.submit(creation_job)
    store.transition(creation_job.creation_job_id, "planning", expected_version=1)
    return store.publish_build_assignment(assignment(creation_job))


def test_assignment_claim_is_exactly_once_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "creation.sqlite3"
    creation_job = job()
    published = prepare_assignment(CreationJobStore(path), creation_job)
    first = CreationJobStore(path).claim_build_assignment(published.assignment_id, "worker-a")
    assert CreationJobStore(path).claim_build_assignment(published.assignment_id, "worker-a") == first
    with pytest.raises(CreationConflictError, match="another worker"):
        CreationJobStore(path).claim_build_assignment(published.assignment_id, "worker-b")


@pytest.mark.parametrize(
    ("field", "foreign"),
    [
        ("creation_job_id", uuid4()), ("factory_job_id", uuid4()), ("correlation_id", uuid4()),
        ("subject_version", 2), ("attempt", 2), ("workspace_policy_sha256", "9" * 64),
        ("lease_ref", {"uri": "artifact://foreign/" + "9" * 64, "sha256": "9" * 64, "media_type": "application/json"}),
        ("required_agent_ids", ["planner", "foreign"]), ("ordered_handoffs", []),
        ("tool_requirements", [{"tool_id": "foreign", "required": True}]),
    ],
)
def test_publish_assignment_rejects_every_foreign_job_binding(tmp_path: Path, field: str, foreign: object) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    creation_job = job()
    store.submit(creation_job)
    store.transition(creation_job.creation_job_id, "planning", expected_version=1)
    updates = {field: foreign}
    if field == "required_agent_ids":
        updates["ordered_handoffs"] = [{"from_agent_id": "planner", "to_agent_id": "foreign"}]
    with pytest.raises(CreationConflictError, match="assignment.*job"):
        store.publish_build_assignment(assignment(creation_job, **updates))


def test_publish_assignment_rejects_stale_or_illegal_job_head(tmp_path: Path) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    creation_job = job()
    store.submit(creation_job)
    with pytest.raises(CreationConflictError, match="checkpoint"):
        store.publish_build_assignment(assignment(creation_job, version=2))


def test_build_effect_requires_live_claim_and_nonterminal_building_state(tmp_path: Path) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    creation_job = job()
    published = prepare_assignment(store, creation_job)
    submitted_effect = effect(published)
    with pytest.raises(CreationConflictError, match="claimed"):
        store.accept_build_effect(submitted_effect)
    store.claim_build_assignment(published.assignment_id, "worker-a")
    store.cancel(creation_job.creation_job_id, expected_version=4)
    with pytest.raises(CreationConflictError, match="terminal"):
        store.accept_build_effect(submitted_effect)


def test_build_effect_rejects_expired_assignment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    creation_job = job()
    store.submit(creation_job)
    store.transition(creation_job.creation_job_id, "planning", expected_version=1)
    published = store.publish_build_assignment(assignment(creation_job))
    store.claim_build_assignment(published.assignment_id, "worker-a")
    class ExpiredClock:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            del tz
            return datetime(2100, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr("minibook.swarm.job_store.datetime", ExpiredClock)
    with pytest.raises(CreationConflictError, match="expired"):
        store.accept_build_effect(effect(published))


def test_effect_binding_is_replayed_and_identity_drift_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "creation.sqlite3"
    store = CreationJobStore(path)
    published = prepare_assignment(store, job())
    store.claim_build_assignment(published.assignment_id, "worker-a")
    submitted = effect(published)
    assert store.accept_build_effect(submitted) == submitted
    assert CreationJobStore(path).build_effect(published.assignment_id) == submitted
    with pytest.raises(CreationConflictError, match="differs|drift"):
        store.accept_build_effect(submitted.model_copy(update={"effect_key": "b" * 64}))


def test_v3_full_legal_lifecycle_reaches_success(tmp_path: Path) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    creation_job = job()
    published = prepare_assignment(store, creation_job)
    assert store.progress(creation_job.creation_job_id).status == "awaiting_build"
    store.claim_build_assignment(published.assignment_id, "worker-a")
    assert store.progress(creation_job.creation_job_id).status == "building"
    store.accept_build_effect(effect(published))
    assert store.progress(creation_job.creation_job_id).status == "validating"
    store.transition(creation_job.creation_job_id, "packaging", expected_version=5)
    store.transition(creation_job.creation_job_id, "readback", expected_version=6)
    refs = [
        {"uri": f"artifact://result/{index}/" + f"{index:x}" * 64, "sha256": f"{index:x}" * 64, "media_type": "application/zip" if index == 3 else "application/json"}
        for index in range(1, 10)
    ]
    result = CreationResultV2(
        creation_job_id=creation_job.creation_job_id, factory_job_id=creation_job.factory_job_id,
        correlation_id=creation_job.correlation_id, subject_version=creation_job.subject_version,
        attempt=creation_job.attempt, status="succeeded", team_manifest_ref=refs[0],
        candidate_manifest_ref=refs[1], source_archive_ref=refs[2], package_manifest_ref=refs[3],
        hermes_usage_receipt_ref=refs[4], codex_build_receipt_ref=refs[5],
        build_evidence_refs=[refs[6]], real_case_evidence_refs=[refs[7]], cas_readback_receipt_ref=refs[8],
    )
    store.finish(result, expected_version=7)
    assert store.progress(creation_job.creation_job_id).status == "succeeded"


def test_illegal_v3_transition_and_stale_complete_step_are_rejected(tmp_path: Path) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    creation_job = job()
    store.submit(creation_job)
    with pytest.raises(CreationConflictError, match="illegal"):
        store.transition(creation_job.creation_job_id, "packaging", expected_version=1)
    with pytest.raises(CreationConflictError, match="version"):
        store.complete_step(creation_job.creation_job_id, "inspect", "effect", {}, expected_version=2)


def test_action_pause_and_response_are_immutable_version_fenced_and_resume_prior_state(tmp_path: Path) -> None:
    path = tmp_path / "creation.sqlite3"
    store = CreationJobStore(path)
    creation_job = job()
    store.submit(creation_job)
    store.transition(creation_job.creation_job_id, "planning", expected_version=1, checkpoint="plan", snapshot={"safe": True})
    request = action_request(creation_job)
    paused = store.record_action_request(request, expected_version=2)
    assert paused.status == "awaiting_user_input"
    assert paused.checkpoint == "plan"
    assert CreationJobStore(path).action_request(request.request_id) == request
    with pytest.raises(CreationConflictError, match="different content"):
        store.record_action_request(request.model_copy(update={"message": "Different safe question"}), expected_version=3)
    response = action_response(request)
    resumed = store.accept_action_response(response, expected_version=3)
    assert resumed.status == "planning"
    assert resumed.checkpoint == "plan"
    assert store.snapshot(creation_job.creation_job_id) == {"safe": True}
    assert CreationJobStore(path).action_response(request.request_id) == response
    with pytest.raises(CreationConflictError, match="consumed|version"):
        store.accept_action_response(action_response(request), expected_version=3)


def test_action_expiry_blocks_the_paused_job(tmp_path: Path) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    creation_job = job()
    store.submit(creation_job)
    store.transition(creation_job.creation_job_id, "planning", expected_version=1)
    request = action_request(creation_job, deadline_at="2020-01-01T00:00:00Z")
    store.record_action_request(request, expected_version=2)
    blocked = store.expire_action_request(request.request_id, expected_version=3, now=datetime.now(timezone.utc))
    assert blocked.status == "blocked"
