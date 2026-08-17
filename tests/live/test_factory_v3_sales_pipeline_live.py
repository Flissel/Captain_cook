from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import UUID

import pytest


pytestmark = pytest.mark.live

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "run-factory-v3-live.ps1"
INPUT = ROOT / "tests" / "fixtures" / "agent_factory" / "sales_pipeline_brief.v3.json"
AGENTS = (
    "account_researcher",
    "opportunity_qualifier",
    "deal_strategist",
    "executive_brief_writer",
)
HANDOFFS = (
    ("account_researcher", "opportunity_qualifier"),
    ("opportunity_qualifier", "deal_strategist"),
    ("deal_strategist", "executive_brief_writer"),
)


def test_real_factory_v3_sales_pipeline_is_captain_accepted(tmp_path: Path) -> None:
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

    evidence_files = tuple(evidence_root.glob("*/evidence.json"))
    assert len(evidence_files) == 1
    evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
    assert evidence["schema"] == "captain.factory-v3-live-evidence.v1"
    assert evidence["status"] == "succeeded"
    UUID(evidence["correlation_id"])
    assert re.fullmatch(r"[0-9a-f]{64}", evidence["released_skill_sha256"])
    assert evidence["hermes_invocation_count"] == 1
    assert evidence["codex_session_count"] == 1
    assert tuple(evidence["agent_ids"]) == AGENTS
    assert tuple(tuple(item) for item in evidence["ordered_handoffs"]) == HANDOFFS
    assert set(evidence["required_tool_ids"]) == {
        "account_snapshot_reader",
        "opportunity_scoring_rules",
    }
    assert evidence["required_tools_tested"] is True
    assert evidence["minibook_package_status"] == "succeeded"
    assert evidence["minibook_readback_status"] == "verified"
    assert evidence["captain_acceptance_status"] == "accepted"
    assert evidence["openfang_events"] == 0
    assert evidence["promotion_events"] == 0
    assert evidence["deployment_events"] == 0
    assert evidence["ready_to_use_events"] == 0
    serialized = json.dumps(evidence, sort_keys=True)
    assert not re.search(r"[A-Za-z]:[/\\]", serialized)
    assert not any(
        marker in serialized.casefold()
        for marker in ("authorization", "api_key", "password", "prompt", "holdout")
    )
