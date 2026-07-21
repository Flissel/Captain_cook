from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from minibook.swarm.api import create_creation_router
from minibook.swarm.job_store import CreationJobStore
from minibook.tests.test_creation_evidence_api import API_KEY, HEADERS, _job_payload


def test_submitted_creation_reads_are_retryable_pending_responses(tmp_path: Path) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    app = FastAPI()
    app.include_router(create_creation_router(store, api_key=API_KEY))
    client = TestClient(app)
    job_id = _job_payload()["creation_job_id"]

    submitted = client.post(
        "/api/v1/creation-jobs",
        json=_job_payload(),
        headers=HEADERS,
    )
    responses = (
        client.get(
            f"/api/v1/creation-jobs/{job_id}/preparation-blocks",
            headers=HEADERS,
        ),
        client.get(f"/api/v1/creation-jobs/{job_id}/result", headers=HEADERS),
        client.get(
            f"/api/v1/creation-jobs/{job_id}/completion-block",
            headers=HEADERS,
        ),
    )

    assert submitted.status_code == 202
    assert tuple(response.status_code for response in responses) == (409, 409, 409)
    assert tuple(response.headers["Retry-After"] for response in responses) == (
        "1",
        "1",
        "1",
    )


def test_terminal_creation_missing_evidence_fails_without_retry(tmp_path: Path) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    app = FastAPI()
    app.include_router(create_creation_router(store, api_key=API_KEY))
    client = TestClient(app)
    job_id = _job_payload()["creation_job_id"]
    client.post("/api/v1/creation-jobs", json=_job_payload(), headers=HEADERS)
    cancelled = client.post(
        f"/api/v1/creation-jobs/{job_id}/cancel",
        json={"expected_version": 1},
        headers=HEADERS,
    )

    responses = (
        client.get(f"/api/v1/creation-jobs/{job_id}/preparation-blocks", headers=HEADERS),
        client.get(f"/api/v1/creation-jobs/{job_id}/result", headers=HEADERS),
        client.get(f"/api/v1/creation-jobs/{job_id}/completion-block", headers=HEADERS),
    )

    assert cancelled.status_code == 200
    assert tuple(response.status_code for response in responses) == (422, 422, 422)
    assert all("Retry-After" not in response.headers for response in responses)
