from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest


pytestmark = pytest.mark.live


def test_creation_job_live_requires_public_service_and_provider_evidence() -> None:
    required = ("MINIBOOK_CREATION_URL", "MINIBOOK_CREATION_API_KEY", "CODEX_PROVIDER_LIVE")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        pytest.skip("missing live prerequisites: " + ", ".join(missing))
    fixture = Path(os.environ.get(
        "MINIBOOK_CREATION_JOB_FIXTURE",
        "tests/fixtures/contracts/minibook_creation_job.v1.json",
    ))
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    base = os.environ["MINIBOOK_CREATION_URL"].rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ['MINIBOOK_CREATION_API_KEY']}"}
    submitted = httpx.post(f"{base}/api/v1/creation-jobs", json=payload, headers=headers, timeout=30)
    assert submitted.status_code in {200, 202}
    job_id = payload["creation_job_id"]
    progress = None
    for _ in range(120):
        response = httpx.get(f"{base}/api/v1/creation-jobs/{job_id}", headers=headers, timeout=30)
        response.raise_for_status()
        progress = response.json()
        if progress["status"] in {"succeeded", "failed", "blocked", "cancelled"}:
            break
        time.sleep(1)
    assert progress is not None and progress["status"] == "succeeded"
    result = httpx.get(
        f"{base}/api/v1/creation-jobs/{job_id}/result", headers=headers, timeout=30
    )
    result.raise_for_status()
    body = result.json()
    assert len(body["package_manifest_ref"]["sha256"]) == 64
    assert body["skill_usage_receipt_ref"] is not None
