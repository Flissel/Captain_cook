from __future__ import annotations

import json
import hmac
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from minibook.swarm.api import create_creation_router
from minibook.swarm.artifact_store import FilesystemCreationArtifactStore
from minibook.swarm.contracts import CreationJobV3, FactoryBuildAssignmentV2, FactoryBuildEffectV2
from minibook.swarm.job_store import CreationJobStore


FIXTURE = Path(__file__).parents[2] / "tests/fixtures/contracts/minibook_creation_job.v1.json"
FIXTURE_ROOT = Path(__file__).parents[2] / "tests/fixtures/contracts"
CALLER_HEADERS = {"Authorization": "Bearer captain-secret"}
AUTH = CALLER_HEADERS


def payload() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def client(tmp_path: Path) -> tuple[TestClient, list[str]]:
    scheduled: list[str] = []
    app = FastAPI()
    app.include_router(
        create_creation_router(
            CreationJobStore(tmp_path / "creation.sqlite3"),
            schedule=lambda job_id: scheduled.append(str(job_id)),
            creation_api_key="captain-secret",
        )
    )
    return TestClient(app), scheduled


def v3_job(**updates: object) -> CreationJobV3:
    value = json.loads((FIXTURE_ROOT / "minibook_creation_job.v3.json").read_text(encoding="utf-8"))
    value.update({"deadline_at": "2099-08-09T12:00:00Z"} | updates)
    return CreationJobV3.model_validate(value)


def build_assignment(job: CreationJobV3) -> FactoryBuildAssignmentV2:
    return FactoryBuildAssignmentV2.model_validate({
        "assignment_id": str(uuid4()), "creation_job_id": str(job.creation_job_id),
        "factory_job_id": str(job.factory_job_id), "correlation_id": str(job.correlation_id),
        "subject_version": job.subject_version, "checkpoint_version": 2, "attempt": job.attempt,
        "effect_key": "a" * 64, "workspace_ref": "workspace://captain/codex",
        "workspace_policy_sha256": job.workspace_policy_ref.sha256,
        "required_agent_ids": list(job.required_agent_ids),
        "ordered_handoffs": [item.model_dump(mode="json") for item in job.ordered_handoffs],
        "tool_requirements": [item.model_dump(mode="json") for item in job.tool_requirements],
        "allowed_output_roots": ["out"], "allowed_commands": [["python", "-m", "pytest"]],
        "limits": {"max_wall_clock_seconds": 30, "max_provider_cost_usd": 1.0, "max_retries": 0, "max_files": 10},
        "lease_ref": job.lease_ref.model_dump(mode="json"), "deadline_at": job.deadline_at.isoformat(),
    })


def build_effect(assignment: FactoryBuildAssignmentV2) -> FactoryBuildEffectV2:
    refs = lambda digit, media="application/json": {
        "uri": f"artifact://worker/{digit * 64}", "sha256": digit * 64, "media_type": media,
    }
    return FactoryBuildEffectV2.model_validate({
        "assignment_id": str(assignment.assignment_id), "creation_job_id": str(assignment.creation_job_id),
        "factory_job_id": str(assignment.factory_job_id), "correlation_id": str(assignment.correlation_id),
        "subject_version": assignment.subject_version, "checkpoint_version": assignment.checkpoint_version,
        "attempt": assignment.attempt, "effect_key": assignment.effect_key,
        "hermes_usage_receipt_ref": refs("1"), "codex_build_receipt_ref": refs("2"),
        "codex_session_ref": refs("3"), "workspace_snapshot_ref": refs("4", "application/zip"),
        "source_archive_ref": refs("5", "application/zip"), "preliminary_team_manifest_ref": refs("6"),
        "build_evidence_refs": [refs("7")], "test_evidence_refs": [refs("8")],
    })


def v3_client(tmp_path: Path) -> tuple[TestClient, CreationJobStore, list[str], FilesystemCreationArtifactStore]:
    scheduled: list[str] = []
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    artifacts = FilesystemCreationArtifactStore(tmp_path / "cas")
    app = FastAPI()
    app.include_router(create_creation_router(
        store, schedule=lambda job_id: scheduled.append(str(job_id)), worker_token="worker-secret",
        worker_id="hermes-factory", artifact_store=artifacts,
        creation_api_key="captain-secret",
    ))
    return TestClient(app), store, scheduled, artifacts


def test_post_is_persisted_before_schedule_and_identical_replay_reschedules(tmp_path: Path) -> None:
    api, scheduled = client(tmp_path)
    first = api.post("/api/v1/creation-jobs", json=payload(), headers=CALLER_HEADERS)
    replay = api.post("/api/v1/creation-jobs", json=payload(), headers=CALLER_HEADERS)
    assert first.status_code == 202
    assert replay.status_code == 200
    assert scheduled == [payload()["creation_job_id"], payload()["creation_job_id"]]


def test_changed_replay_is_409(tmp_path: Path) -> None:
    api, _ = client(tmp_path)
    assert api.post("/api/v1/creation-jobs", json=payload(), headers=CALLER_HEADERS).status_code == 202
    changed = payload() | {"attempt": 2}
    assert api.post("/api/v1/creation-jobs", json=changed, headers=CALLER_HEADERS).status_code == 409


def test_status_cancel_and_result_conflicts_are_typed(tmp_path: Path) -> None:
    api, _ = client(tmp_path)
    job_id = str(payload()["creation_job_id"])
    assert api.get(f"/api/v1/creation-jobs/{job_id}", headers=CALLER_HEADERS).status_code == 404
    api.post("/api/v1/creation-jobs", json=payload(), headers=CALLER_HEADERS)
    assert api.get(f"/api/v1/creation-jobs/{job_id}", headers=CALLER_HEADERS).json()["status"] == "queued"
    assert api.get(f"/api/v1/creation-jobs/{job_id}/result", headers=CALLER_HEADERS).status_code == 409
    assert api.post(f"/api/v1/creation-jobs/{job_id}/cancel", json={"expected_version": 2}, headers=CALLER_HEADERS).status_code == 409
    cancelled = api.post(
        f"/api/v1/creation-jobs/{job_id}/cancel", json={"expected_version": 1}, headers=CALLER_HEADERS
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_disabled_forge_exposes_capability_document_without_store() -> None:
    app = FastAPI()
    app.include_router(create_creation_router(None, creation_api_key="captain-secret"))
    api = TestClient(app)
    capability = api.get("/api/v1/creation-capabilities", headers=AUTH)
    assert capability.status_code == 200
    assert capability.json() == {"creation_jobs": False, "schema": "minibook.creation-capabilities.v1"}
    assert api.post("/api/v1/creation-jobs", json=payload(), headers=CALLER_HEADERS).status_code == 503


def test_every_creation_route_requires_the_exact_bearer(tmp_path: Path) -> None:
    api, _ = client(tmp_path)
    job_id = str(payload()["creation_job_id"])
    routes = (
        ("get", "/api/v1/creation-capabilities", None),
        ("post", "/api/v1/creation-jobs", payload()),
        ("get", f"/api/v1/creation-jobs/{job_id}", None),
        ("post", f"/api/v1/creation-jobs/{job_id}/cancel", {"expected_version": 1}),
        ("get", f"/api/v1/creation-jobs/{job_id}/result", None),
    )
    for method, path, body in routes:
        call = getattr(api, method)
        kwargs = {"json": body} if body is not None else {}
        assert call(path, **kwargs).status_code == 401
        assert call(
            path, **kwargs, headers={"Authorization": "Bearer wrong"},
        ).status_code == 403

def test_v3_submit_status_result_and_cancel_are_version_dispatched(tmp_path: Path) -> None:
    api, _, scheduled, _ = v3_client(tmp_path)
    job = v3_job()
    submitted = api.post("/v3/creation/jobs", json=job.model_dump(mode="json", by_alias=True), headers=CALLER_HEADERS)
    assert submitted.status_code == 202
    assert api.get(f"/v3/creation/jobs/{job.creation_job_id}", headers=CALLER_HEADERS).json()["status"] == "queued"
    assert api.get(f"/v3/creation/jobs/{job.creation_job_id}/result", headers=CALLER_HEADERS).status_code == 409
    cancelled = api.post(f"/v3/creation/jobs/{job.creation_job_id}/cancel", json={"expected_version": 1}, headers=CALLER_HEADERS)
    assert cancelled.status_code == 200
    assert scheduled == [str(job.creation_job_id)]
    assert api.post("/v3/creation/jobs", json=payload(), headers=CALLER_HEADERS).status_code == 422


def test_lifecycle_routes_require_separate_constant_time_caller_auth_before_lookup(
    tmp_path: Path, monkeypatch,
) -> None:
    api, _, _, _ = v3_client(tmp_path)
    missing_id = str(uuid4())
    routes = (
        ("post", "/api/v1/creation-jobs", payload()),
        ("get", f"/api/v1/creation-jobs/{missing_id}", None),
        ("get", f"/api/v1/creation-jobs/{missing_id}/result", None),
        ("post", f"/api/v1/creation-jobs/{missing_id}/cancel", {"expected_version": 1}),
        ("post", "/v3/creation/jobs", {}),
        ("get", f"/v3/creation/jobs/{missing_id}", None),
        ("get", f"/v3/creation/jobs/{missing_id}/result", None),
        ("post", f"/v3/creation/jobs/{missing_id}/cancel", {"expected_version": 1}),
    )
    comparisons: list[tuple[str, str]] = []
    original = hmac.compare_digest

    def recording_compare(left: str, right: str) -> bool:
        comparisons.append((left, right))
        return original(left, right)

    monkeypatch.setattr("minibook.swarm.api.hmac.compare_digest", recording_compare)
    for method, path, body in routes:
        request = getattr(api, method)
        kwargs = {} if body is None else {"json": body}
        assert request(path, **kwargs).status_code == 401
        wrong = request(path, headers={"Authorization": "Bearer wrong"}, **kwargs)
        assert wrong.status_code == 403
        worker = request(path, headers={"Authorization": "Bearer worker-secret"}, **kwargs)
        assert worker.status_code == 403
        assert wrong.json() == worker.json()
    assert comparisons
    assert all(expected == "captain-secret" for _, expected in comparisons)


@pytest.mark.parametrize("path", ("/api/v1/creation-jobs", "/v3/creation/jobs"))
def test_lifecycle_auth_precedes_malformed_submit_body_validation(
    tmp_path: Path, path: str,
) -> None:
    api, _, _, _ = v3_client(tmp_path)
    malformed = {"content": "{", "headers": {"Content-Type": "application/json"}}

    assert api.post(path, **malformed).status_code == 401
    authorized = malformed | {"headers": CALLER_HEADERS | malformed["headers"]}
    assert api.post(path, **authorized).status_code == 422


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/creation-jobs/not-a-uuid",
        "/api/v1/creation-jobs/not-a-uuid/result",
        "/v3/creation/jobs/not-a-uuid",
        "/v3/creation/jobs/not-a-uuid/result",
    ),
)
def test_lifecycle_auth_precedes_invalid_path_validation(tmp_path: Path, path: str) -> None:
    api, _, _, _ = v3_client(tmp_path)

    assert api.get(path).status_code == 401
    assert api.get(path, headers=CALLER_HEADERS).status_code == 422


def test_lifecycle_auth_precedes_cancel_body_and_path_validation(tmp_path: Path) -> None:
    api, _, _, _ = v3_client(tmp_path)
    malformed = {"content": "{", "headers": {"Content-Type": "application/json"}}

    assert api.post("/v3/creation/jobs/not-a-uuid/cancel", **malformed).status_code == 401
    authorized = malformed | {"headers": CALLER_HEADERS | malformed["headers"]}
    assert api.post("/v3/creation/jobs/not-a-uuid/cancel", **authorized).status_code == 422


def test_router_rejects_shared_lifecycle_and_worker_credentials(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must differ"):
        create_creation_router(
            CreationJobStore(tmp_path / "creation.sqlite3"),
            creation_api_key="shared-secret",
            worker_token="shared-secret",
            worker_id="hermes-factory",
        )


def test_identical_replay_reschedules_when_first_callback_failed_after_persistence(
    tmp_path: Path,
) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    calls: list[str] = []

    def schedule(job_id) -> None:
        calls.append(str(job_id))
        if len(calls) == 1:
            raise RuntimeError("scheduler temporarily unavailable")

    app = FastAPI()
    app.include_router(create_creation_router(
        store, schedule=schedule, creation_api_key="captain-secret",
    ))
    api = TestClient(app, raise_server_exceptions=False)

    first = api.post("/api/v1/creation-jobs", json=payload(), headers=CALLER_HEADERS)
    assert first.status_code == 500
    assert store.progress(UUID(str(payload()["creation_job_id"]))).status == "queued"
    replay = api.post("/api/v1/creation-jobs", json=payload(), headers=CALLER_HEADERS)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert calls == [payload()["creation_job_id"], payload()["creation_job_id"]]


def test_worker_auth_uses_constant_time_comparison_and_binds_identity(
    tmp_path: Path, monkeypatch,
) -> None:
    api, store, _, _ = v3_client(tmp_path)
    job = v3_job()
    store.submit(job)
    store.transition(job.creation_job_id, "planning", expected_version=1)
    assignment = store.publish_build_assignment(build_assignment(job))
    calls: list[tuple[str, str]] = []
    original = hmac.compare_digest

    def recording_compare(left: str, right: str) -> bool:
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr("minibook.swarm.api.hmac.compare_digest", recording_compare)
    assert api.get("/v3/creation/assignments/next").status_code == 401
    assert api.get("/v3/creation/assignments/next", headers={"Authorization": "Bearer wrong"}).status_code == 403
    headers = {"Authorization": "Bearer worker-secret"}
    assert api.get("/v3/creation/assignments/next", headers=headers).status_code == 200
    foreign = api.post(
        f"/v3/creation/assignments/{assignment.assignment_id}/claim", headers=headers,
        json={"worker_id": "other-worker"},
    )
    assert foreign.status_code == 403
    assert calls == [("wrong", "worker-secret"), ("worker-secret", "worker-secret"), ("worker-secret", "worker-secret")]


def test_worker_claim_replay_conflict_effect_binding_and_schedule_resume(tmp_path: Path) -> None:
    api, store, scheduled, _ = v3_client(tmp_path)
    job = v3_job()
    store.submit(job)
    store.transition(job.creation_job_id, "planning", expected_version=1)
    assignment = store.publish_build_assignment(build_assignment(job))
    headers = {"Authorization": "Bearer worker-secret"}
    path = f"/v3/creation/assignments/{assignment.assignment_id}/claim"
    assert api.post(path, headers=headers, json={"worker_id": "hermes-factory"}).status_code == 200
    assert api.post(path, headers=headers, json={"worker_id": "hermes-factory"}).status_code == 200
    foreign_app = FastAPI()
    foreign_app.include_router(create_creation_router(
        store, worker_token="other-secret", worker_id="other-worker",
    ))
    foreign_api = TestClient(foreign_app)
    foreign_headers = {"Authorization": "Bearer other-secret"}
    assert foreign_api.post(
        path, headers=foreign_headers, json={"worker_id": "other-worker"},
    ).status_code == 409
    assert foreign_api.post(
        f"/v3/creation/assignments/{assignment.assignment_id}/effect", headers=foreign_headers,
        json=build_effect(assignment).model_dump(mode="json", by_alias=True),
    ).status_code == 403
    bad = build_effect(assignment).model_copy(update={"effect_key": "b" * 64})
    assert api.post(
        f"/v3/creation/assignments/{assignment.assignment_id}/effect", headers=headers,
        json=bad.model_dump(mode="json", by_alias=True),
    ).status_code == 409
    accepted = api.post(
        f"/v3/creation/assignments/{assignment.assignment_id}/effect", headers=headers,
        json=build_effect(assignment).model_dump(mode="json", by_alias=True),
    )
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "validating"
    assert scheduled == [str(job.creation_job_id)]
    malformed = api.post(
        f"/v3/creation/assignments/{assignment.assignment_id}/effect", headers=headers, json={},
    )
    assert malformed.status_code == 422


def test_effect_response_captures_validating_before_fast_scheduler_advances(
    tmp_path: Path,
) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    job = v3_job()
    store.submit(job)
    store.transition(job.creation_job_id, "planning", expected_version=1)
    assignment = store.publish_build_assignment(build_assignment(job))
    store.claim_build_assignment(assignment.assignment_id, "hermes-factory")

    def advance_immediately(job_id) -> None:
        acknowledgement = store.progress(job_id)
        assert acknowledgement.status == "validating"
        store.transition(job_id, "packaging", expected_version=acknowledgement.version)

    app = FastAPI()
    app.include_router(create_creation_router(
        store, schedule=advance_immediately, creation_api_key="captain-secret",
        worker_token="worker-secret", worker_id="hermes-factory",
    ))
    api = TestClient(app)
    response = api.post(
        f"/v3/creation/assignments/{assignment.assignment_id}/effect",
        headers={"Authorization": "Bearer worker-secret"},
        json=build_effect(assignment).model_dump(mode="json", by_alias=True),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "validating"
    assert store.progress(job.creation_job_id).status == "packaging"


def test_artifact_metadata_never_returns_local_path(tmp_path: Path) -> None:
    api, _, _, artifacts = v3_client(tmp_path)
    reference = artifacts.put(b"safe", "application/json", namespace="evidence")
    response = api.get(
        f"/v3/creation/artifacts/{reference.sha256}/metadata",
        headers={"Authorization": "Bearer worker-secret"},
    )
    assert response.status_code == 200
    assert response.json() == {"sha256": reference.sha256, "size_bytes": 4}
    assert str(tmp_path) not in response.text


def test_next_assignment_exposes_only_current_claimable_or_same_worker_replay(
    tmp_path: Path,
) -> None:
    api, store, _, _ = v3_client(tmp_path)
    headers = {"Authorization": "Bearer worker-secret"}

    cancelled_job = v3_job(creation_job_id=str(uuid4()))
    store.submit(cancelled_job)
    store.transition(cancelled_job.creation_job_id, "planning", expected_version=1)
    store.publish_build_assignment(build_assignment(cancelled_job))
    store.cancel(cancelled_job.creation_job_id, expected_version=3)

    expired_job = v3_job(
        creation_job_id=str(uuid4()), deadline_at="2020-08-09T12:00:00Z"
    )
    store.submit(expired_job)
    store.transition(expired_job.creation_job_id, "planning", expected_version=1)
    store.publish_build_assignment(build_assignment(expired_job))

    stale_job = v3_job(creation_job_id=str(uuid4()))
    store.submit(stale_job)
    store.transition(stale_job.creation_job_id, "planning", expected_version=1)
    store.publish_build_assignment(build_assignment(stale_job))
    store.transition(stale_job.creation_job_id, "awaiting_user_input", expected_version=3)

    assert store.next_claimable_build_assignment("hermes-factory") is None
    assert api.get("/v3/creation/assignments/next", headers=headers).status_code == 204

    live_job = v3_job(creation_job_id=str(uuid4()))
    store.submit(live_job)
    store.transition(live_job.creation_job_id, "planning", expected_version=1)
    live = store.publish_build_assignment(build_assignment(live_job))
    assert store.next_claimable_build_assignment("hermes-factory") == live
    assert api.get("/v3/creation/assignments/next", headers=headers).json()[
        "assignment_id"
    ] == str(live.assignment_id)

    store.claim_build_assignment(live.assignment_id, "hermes-factory")
    assert store.next_claimable_build_assignment("hermes-factory") == live
    assert store.next_claimable_build_assignment("other-worker") is None
    assert api.get("/v3/creation/assignments/next", headers=headers).json()[
        "assignment_id"
    ] == str(live.assignment_id)
