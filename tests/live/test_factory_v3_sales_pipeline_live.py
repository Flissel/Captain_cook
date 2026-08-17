from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import pytest

from gateway.factory_v3_live_evidence import (
    FactoryV3HttpEvidenceAuthority,
    assert_factory_v3_live_secret_free,
    verify_factory_v3_live_evidence,
)


pytestmark = pytest.mark.live

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "run-factory-v3-live.ps1"
INPUT = ROOT / "tests" / "fixtures" / "agent_factory" / "sales_pipeline_brief.v3.json"


def test_real_factory_v3_sales_pipeline_is_independently_captain_accepted(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "factory-v3-live"
    completed = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-Input",
            str(INPUT),
            "-EvidenceRoot",
            str(evidence_root),
            "-Python",
            sys.executable,
        ],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=1_200,
        check=False,
    )
    if completed.returncode != 0:
        summaries = [
            json.loads(line)
            for line in completed.stdout.splitlines()
            if line.strip().startswith("{")
        ]
        status = summaries[-1]["status"] if summaries else "preflight_error"
        unmet = summaries[-1].get("unmet_checks", []) if summaries else []
        pytest.fail(f"Factory V3 live gate did not run: {status}; unmet={unmet}")

    preflights = tuple(evidence_root.glob("*/preflight.json"))
    indexes = tuple(evidence_root.glob("*/evidence-index.json"))
    assert len(preflights) == 1
    assert len(indexes) == 1
    assert preflights[0].parent == indexes[0].parent
    preflight_bytes = preflights[0].read_bytes()
    preflight = json.loads(preflight_bytes)
    index = json.loads(indexes[0].read_bytes())
    assert_factory_v3_live_secret_free(preflight)
    assert_factory_v3_live_secret_free(index)
    assert preflight["schema"] == "captain.factory-v3-live-preflight.v1"
    assert preflight["status"] == "ready"
    run_id = UUID(preflight["run_id"])
    assert UUID(index["preflight_run_id"]) == run_id
    assert index["preflight_report_sha256"] == hashlib.sha256(
        preflight_bytes
    ).hexdigest()

    authority = FactoryV3HttpEvidenceAuthority(
        gateway_origin=os.environ["CAPTAIN_FACTORY_V3_GATEWAY_ORIGIN"],
        minibook_origin=os.environ["CAPTAIN_FACTORY_V3_MINIBOOK_ORIGIN"],
        captain_token=os.environ["CAPTAIN_FACTORY_V3_CAPTAIN_TOKEN"],
        worker_token=os.environ["CAPTAIN_FACTORY_V3_WORKER_TOKEN"],
    )
    verified = verify_factory_v3_live_evidence(index, authority=authority)

    assert verified.preflight_run_id == run_id
    assert verified.correlation_id == UUID(index["correlation_id"])
    assert verified.factory_job_id == UUID(index["factory_job_id"])
    assert verified.creation_job_id == UUID(index["creation_job_id"])
    assert_factory_v3_live_secret_free(verified.as_dict())
