from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agenten.agent_factory.contracts import FactoryEvidenceBlock
from minibook.swarm.contracts import (
    CreationJobV1,
    CreationPreparationEvidenceV1,
    CreationResultV1,
)
from minibook.swarm.api import create_creation_router
from minibook.swarm.job_store import CreationJobStore


FIXTURE = Path(__file__).parents[2] / "tests/fixtures/contracts/minibook_creation_job.v1.json"
API_KEY = "test-creation-evidence-key"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}


def _job_payload() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _ref(namespace: str, digest: str) -> dict[str, str]:
    return {
        "uri": f"artifact://capability-factory/{namespace}/{digest}",
        "sha256": digest,
        "media_type": "application/json",
    }


def _block(
    *,
    phase: str,
    role: str,
    event_id: str,
    occurred_at: str,
    evidence_refs: list[dict[str, str]],
    lease_id: str,
) -> dict[str, object]:
    job = _job_payload()
    return {
        "schema": "captain.agent-factory-block.v1",
        "event_id": event_id,
        "job_id": job["factory_job_id"],
        "correlation_id": job["correlation_id"],
        "causation_id": job["causation_id"],
        "occurred_at": occurred_at,
        "producer": "hermes",
        "subject_version": job["subject_version"],
        "attempt": job["attempt"],
        "phase": phase,
        "role": role,
        "status": "succeeded",
        "artifact_refs": [],
        "evidence_refs": evidence_refs,
        "assertion_ids": [],
        "lease_id": lease_id,
    }


def _preparation_payload() -> dict[str, object]:
    return {
        "schema": "minibook.creation-preparation-evidence.v1",
        "creation_job": _job_payload(),
        "blocks": [
            _block(
                phase="blueprint_created",
                role="agent_architect",
                event_id="55555555-5555-4555-8555-555555555555",
                occurred_at="2029-01-01T00:00:01Z",
                evidence_refs=[_ref("blueprint", "1" * 64)],
                lease_id="lease-architect-1",
            ),
            _block(
                phase="tool_candidate_tested",
                role="tool_integrator",
                event_id="66666666-6666-4666-8666-666666666666",
                occurred_at="2029-01-01T00:00:02Z",
                evidence_refs=[_ref("tools", "2" * 64)],
                lease_id="lease-tool-1",
            ),
        ],
    }


def _result() -> CreationResultV1:
    job = _job_payload()
    package = _ref("package-manifest", "3" * 64)
    skill = _ref("skill-usage", "4" * 64)
    return CreationResultV1.model_validate(
        {
            "schema": "minibook.creation-result.v1",
            "creation_job_id": job["creation_job_id"],
            "correlation_id": job["correlation_id"],
            "subject_version": job["subject_version"],
            "attempt": job["attempt"],
            "status": "succeeded",
            "package_manifest_ref": package,
            "artifact_refs": [_ref("agent-team", "5" * 64)],
            "evidence_refs": [_ref("tests", "6" * 64)],
            "tool_gaps": [],
            "skill_usage_receipt_ref": skill,
            "private_skill_candidate_ref": None,
            "failure": None,
        }
    )


def _completion_payload(result: CreationResultV1) -> dict[str, object]:
    assert result.package_manifest_ref is not None
    assert result.skill_usage_receipt_ref is not None
    return {
        "schema": "minibook.creation-completion-evidence.v1",
        "result": result.model_dump(mode="json", by_alias=True),
        "block": _block(
            phase="agent_code_created",
            role="tool_integrator",
            event_id="77777777-7777-4777-8777-777777777777",
            occurred_at="2029-01-01T00:00:03Z",
            evidence_refs=[
                result.package_manifest_ref.model_dump(mode="json"),
                result.skill_usage_receipt_ref.model_dump(mode="json"),
            ],
            lease_id="lease-code-1",
        ),
    }


def _client(path: Path) -> tuple[TestClient, CreationJobStore]:
    store = CreationJobStore(path)
    app = FastAPI()
    app.include_router(create_creation_router(store, api_key=API_KEY))
    return TestClient(app), store


def test_preparation_blocks_are_authenticated_typed_and_restart_safe(tmp_path: Path) -> None:
    database = tmp_path / "creation.sqlite3"
    api, _ = _client(database)
    job_id = _job_payload()["creation_job_id"]
    route = f"/api/v1/creation-jobs/{job_id}/preparation-blocks"

    assert api.get(route).status_code == 401
    first = api.put(
        f"/api/v1/creation-jobs/{job_id}/preparation-evidence",
        json=_preparation_payload(),
        headers=HEADERS,
    )
    replay = api.put(
        f"/api/v1/creation-jobs/{job_id}/preparation-evidence",
        json=_preparation_payload(),
        headers=HEADERS,
    )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    observed = api.get(route, headers=HEADERS)
    assert observed.status_code == 200
    blocks = tuple(FactoryEvidenceBlock.model_validate(item) for item in observed.json())
    assert tuple(block.phase.value for block in blocks) == (
        "blueprint_created",
        "tool_candidate_tested",
    )

    restarted, _ = _client(database)
    assert restarted.get(route, headers=HEADERS).json() == observed.json()


def test_preparation_rejects_identity_drift_and_changed_replay(tmp_path: Path) -> None:
    api, _ = _client(tmp_path / "creation.sqlite3")
    job_id = _job_payload()["creation_job_id"]
    route = f"/api/v1/creation-jobs/{job_id}/preparation-evidence"
    assert api.put(route, json=_preparation_payload(), headers=HEADERS).status_code == 201

    changed = _preparation_payload()
    changed["blocks"][0]["occurred_at"] = "2029-01-01T00:00:00Z"  # type: ignore[index]
    assert api.put(route, json=changed, headers=HEADERS).status_code == 409

    drifted = _preparation_payload()
    drifted["blocks"][0]["correlation_id"] = "88888888-8888-4888-8888-888888888888"  # type: ignore[index]
    assert api.put(route, json=drifted, headers=HEADERS).status_code == 422


def test_completion_is_bound_to_persisted_result_and_returns_both_release_refs(
    tmp_path: Path,
) -> None:
    api, store = _client(tmp_path / "creation.sqlite3")
    job = CreationJobV1.model_validate(_job_payload())
    result = _result()
    store.record_preparation(
        CreationPreparationEvidenceV1.model_validate(_preparation_payload())
    )
    store.submit(job)
    store.finish(result)
    job_id = str(job.creation_job_id)
    evidence_route = f"/api/v1/creation-jobs/{job_id}/completion-evidence"
    block_route = f"/api/v1/creation-jobs/{job_id}/completion-block"

    first = api.put(evidence_route, json=_completion_payload(result), headers=HEADERS)
    replay = api.put(evidence_route, json=_completion_payload(result), headers=HEADERS)
    observed_result = api.get(
        f"/api/v1/creation-jobs/{job_id}/result", headers=HEADERS
    )
    observed_block = api.get(block_route, headers=HEADERS)

    assert first.status_code == 201
    assert replay.status_code == 200
    assert observed_result.status_code == 200
    assert observed_result.json()["package_manifest_ref"] == _ref("package-manifest", "3" * 64)
    assert observed_result.json()["skill_usage_receipt_ref"] == _ref("skill-usage", "4" * 64)
    assert observed_block.status_code == 200
    block = FactoryEvidenceBlock.model_validate(observed_block.json())
    assert tuple(ref.sha256 for ref in block.evidence_refs) == ("3" * 64, "4" * 64)


def test_completion_rejects_unpersisted_result_changed_replay_and_secret_fields(
    tmp_path: Path,
) -> None:
    api, store = _client(tmp_path / "creation.sqlite3")
    job = CreationJobV1.model_validate(_job_payload())
    result = _result()
    store.submit(job)
    route = f"/api/v1/creation-jobs/{job.creation_job_id}/completion-evidence"

    assert api.put(route, json=_completion_payload(result), headers=HEADERS).status_code == 409
    store.finish(result)
    assert api.put(route, json=_completion_payload(result), headers=HEADERS).status_code == 409
    store.record_preparation(
        CreationPreparationEvidenceV1.model_validate(_preparation_payload())
    )
    assert api.put(route, json=_completion_payload(result), headers=HEADERS).status_code == 201

    changed = _completion_payload(result)
    changed["block"]["occurred_at"] = "2029-01-01T00:00:04Z"  # type: ignore[index]
    assert api.put(route, json=changed, headers=HEADERS).status_code == 409

    secret_bearing = _completion_payload(result)
    secret_bearing["authorization"] = "Bearer should-not-be-stored"
    assert api.put(route, json=secret_bearing, headers=HEADERS).status_code == 422
