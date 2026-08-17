from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "run-factory-v3-live.ps1"
INPUT = ROOT / "tests" / "fixtures" / "agent_factory" / "sales_pipeline_brief.v3.json"


def test_live_script_persists_unmet_preflight_without_starting_a_build(
    tmp_path: Path,
) -> None:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("CAPTAIN_FACTORY_V3_")
        and name
        not in {
            "CAPTAIN_CODEX_EXECUTABLE",
            "CAPTAIN_PWSH_EXECUTABLE",
            "CAPTAIN_CODEX_HOME",
        }
    }
    evidence_root = tmp_path / "evidence"

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
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 3
    summaries = [
        json.loads(line)
        for line in completed.stdout.splitlines()
        if line.strip().startswith("{")
    ]
    assert summaries[-1]["status"] == "unmet_preflight"
    assert str(tmp_path) not in completed.stdout
    reports = tuple(evidence_root.glob("*/preflight.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["status"] == "unmet_preflight"
    assert not tuple(evidence_root.rglob("build-started*"))
    assert not tuple(evidence_root.rglob("evidence.json"))

    resumed = subprocess.run(
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
            "-ResumeMetadata",
            str(reports[0]),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert resumed.returncode == 3
    resumed_summary = json.loads(resumed.stdout.strip().splitlines()[-1])
    assert resumed_summary["run_id"] == report["run_id"]
    assert len(tuple(evidence_root.glob("*/preflight.json"))) == 1
