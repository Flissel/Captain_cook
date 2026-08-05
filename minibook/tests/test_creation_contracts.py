from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from minibook.swarm.contracts import CreationJobV1, CreationResultV1


FIXTURES = Path(__file__).parents[2] / "tests" / "fixtures" / "contracts"


def job_payload() -> dict[str, object]:
    return json.loads(
        (FIXTURES / "minibook_creation_job.v1.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "field", ["holdout_bodies", "credentials", "absolute_workspace"]
)
def test_creation_job_rejects_private_or_unrestricted_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        CreationJobV1.model_validate(job_payload() | {field: "forbidden"})


@pytest.mark.parametrize("uri", ["C:/work/result.json", "/tmp/result.json", "../result"])
def test_creation_job_rejects_non_opaque_artifact_uris(uri: str) -> None:
    payload = job_payload()
    payload["input_ref"] = payload["input_ref"] | {"uri": uri}  # type: ignore[operator]
    with pytest.raises(ValidationError):
        CreationJobV1.model_validate(payload)


def test_creation_job_reads_legacy_persisted_job_without_new_authority() -> None:
    payload = job_payload()
    del payload["architect_lease_id"]
    assert CreationJobV1.model_validate(payload).architect_lease_id is None


def test_success_requires_manifest_and_skill_receipt() -> None:
    payload = json.loads(
        (FIXTURES / "minibook_creation_result.v1.json").read_text(encoding="utf-8")
    )
    payload["package_manifest_ref"] = None
    with pytest.raises(ValidationError, match="package manifest"):
        CreationResultV1.model_validate(payload)


def test_failure_requires_sanitized_failure() -> None:
    payload = json.loads(
        (FIXTURES / "minibook_creation_result.v1.json").read_text(encoding="utf-8")
    )
    payload.update(status="failed", package_manifest_ref=None, skill_usage_receipt_ref=None)
    with pytest.raises(ValidationError, match="failure"):
        CreationResultV1.model_validate(payload)
