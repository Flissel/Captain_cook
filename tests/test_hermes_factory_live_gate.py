from __future__ import annotations

import os
import subprocess
from pathlib import Path

from scripts.verify_submission import REQUIRED_FILES


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "run-hermes-factory-live-gate.ps1"
LIVE_TEST = ROOT / "tests" / "live" / "test_hermes_six_skill_factory_live.py"
RUNBOOK = ROOT / "docs" / "AGENT_FACTORY_RUNBOOK.md"

SKILLS = (
    "captain-factory-discover",
    "captain-factory-brief-codex",
    "captain-factory-execute-team",
    "captain-factory-evaluate-team",
    "captain-factory-improve-team",
    "captain-factory-report-captain",
)


def test_live_gate_wrapper_has_the_exact_paid_gate_parameters() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    assert "[ValidateSet('demo', 'release')]" in source
    assert "[string]$Mode = 'demo'" in source
    assert "[Parameter(Mandatory)]" in source
    assert "[decimal]$MaxCostUsd" in source
    assert "[string]$Model" in source
    assert "[switch]$WithN8n" in source


def test_live_gate_wrapper_is_fail_closed_and_runs_only_the_factory_live_file() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    assert "captain_test" in source
    assert "mariadb-test" in source
    assert "docker" in source and "version" in source and "compose" in source
    assert "codex" in source and "login" in source and "status" in source
    assert "hermes" in source and "skills" in source and "enabled-only" in source
    assert "captain-agent-factory-loop" in source
    for skill in SKILLS:
        assert skill in source
    assert "agenten.agent_factory.factory_live_entrypoint" in source
    assert "preflight" in source
    assert "$env:CAPTAIN_FACTORY_PREREQUISITES_CONFIRMED = '1'" in source
    assert source.index("preflight") < source.index(
        "$env:CAPTAIN_FACTORY_PREREQUISITES_CONFIRMED = '1'"
    )
    assert "tests/live/test_hermes_six_skill_factory_live.py" in source
    assert "'-m', 'live'" in source
    assert "tests/live/test_gate_a_codex_n8n.py" not in source
    assert "tests/live/test_gate_e_release_decision.py" not in source


def test_live_gate_wrapper_uses_an_external_content_addressed_redacted_report() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    assert "[System.IO.Path]::GetTempPath()" in source
    assert "CAPTAIN_FACTORY_REPORT_DIRECTORY" in source
    assert "sha256-" in source
    assert "Get-FileHash" in source
    assert "secret" in source.lower()
    assert "authorization" in source.lower()
    assert "artifacts/" not in source.replace("\\", "/").lower()


def test_live_gate_rejects_a_non_isolated_database_without_printing_the_dsn() -> None:
    secret = "fixture-password-must-not-be-printed"
    environment = os.environ.copy()
    environment["TEST_MARIADB_DSN"] = (
        f"mysql+pymysql://captain:{secret}@127.0.0.1:3306/production"
    )

    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(WRAPPER),
            "-Mode",
            "demo",
            "-MaxCostUsd",
            "1.00",
            "-Model",
            "fixture-model",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "captain_test" in output
    assert secret not in output


def test_live_test_and_runbook_freeze_the_runtime_merge_contract() -> None:
    live_source = LIVE_TEST.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "pytestmark = pytest.mark.live" in live_source
    assert "CAPTAIN_FACTORY_PREREQUISITES_CONFIRMED" in live_source
    assert "pytest.skip" in live_source
    assert "pytest.fail" in live_source
    assert "agenten.agent_factory.factory_live_entrypoint" in live_source
    assert "run_factory_live_gate_from_environment" in live_source
    assert "demo_ready" in live_source
    assert "ready_to_use" in live_source
    assert "recovery" in live_source
    assert "provider_traces" in live_source
    assert "n8n_execution_id" in live_source
    assert "agenten.agent_factory.factory_live_entrypoint" in runbook
    assert "run_factory_live_gate_from_environment" in runbook
    assert "preflight" in runbook
    assert "Runtime merge contract" in runbook


def test_submission_inventory_includes_factory_live_operations_files() -> None:
    assert "scripts/run-hermes-factory-live-gate.ps1" in REQUIRED_FILES
    assert "tests/live/test_hermes_six_skill_factory_live.py" in REQUIRED_FILES
    assert "docs/AGENT_FACTORY_RUNBOOK.md" in REQUIRED_FILES
