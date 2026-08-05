from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from minibook.swarm.api import create_creation_router
from minibook.swarm.job_store import CreationJobStore


FIXTURE = Path(__file__).parents[2] / "tests/fixtures/contracts/minibook_creation_job.v1.json"


def payload() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def client(tmp_path: Path) -> tuple[TestClient, list[str]]:
    scheduled: list[str] = []
    app = FastAPI()
    app.include_router(
        create_creation_router(
            CreationJobStore(tmp_path / "creation.sqlite3"),
            schedule=lambda job_id: scheduled.append(str(job_id)),
        )
    )
    return TestClient(app), scheduled


def test_post_is_persisted_before_schedule_and_identical_replay_is_200(tmp_path: Path) -> None:
    api, scheduled = client(tmp_path)
    first = api.post("/api/v1/creation-jobs", json=payload())
    replay = api.post("/api/v1/creation-jobs", json=payload())
    assert first.status_code == 202
    assert replay.status_code == 200
    assert scheduled == [payload()["creation_job_id"]]


def test_changed_replay_is_409(tmp_path: Path) -> None:
    api, _ = client(tmp_path)
    assert api.post("/api/v1/creation-jobs", json=payload()).status_code == 202
    changed = payload() | {"attempt": 2}
    assert api.post("/api/v1/creation-jobs", json=changed).status_code == 409


def test_status_cancel_and_result_conflicts_are_typed(tmp_path: Path) -> None:
    api, _ = client(tmp_path)
    job_id = str(payload()["creation_job_id"])
    assert api.get(f"/api/v1/creation-jobs/{job_id}").status_code == 404
    api.post("/api/v1/creation-jobs", json=payload())
    assert api.get(f"/api/v1/creation-jobs/{job_id}").json()["status"] == "queued"
    assert api.get(f"/api/v1/creation-jobs/{job_id}/result").status_code == 409
    assert api.post(f"/api/v1/creation-jobs/{job_id}/cancel", json={"expected_version": 2}).status_code == 409
    cancelled = api.post(
        f"/api/v1/creation-jobs/{job_id}/cancel", json={"expected_version": 1}
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_disabled_forge_exposes_capability_document_without_store() -> None:
    app = FastAPI()
    app.include_router(create_creation_router(None))
    api = TestClient(app)
    capability = api.get("/api/v1/creation-capabilities")
    assert capability.status_code == 200
    assert capability.json() == {"creation_jobs": False, "schema": "minibook.creation-capabilities.v1"}
    assert api.post("/api/v1/creation-jobs", json=payload()).status_code == 503
