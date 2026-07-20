from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "gateway-and-gate-e.yml"


def _workflow() -> dict[str, object]:
    assert WORKFLOW_PATH.is_file(), "the Gateway and Gate E CI workflow must exist"
    loaded = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_gateway_ci_uses_a_real_mariadb_service_without_skips() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["mariadb_gateway"]
    assert isinstance(job, dict)
    assert job["runs-on"] == "ubuntu-latest"

    services = job["services"]
    assert isinstance(services, dict)
    mariadb = services["mariadb"]
    assert isinstance(mariadb, dict)
    assert str(mariadb["image"]).startswith("mariadb:")

    environment = job["env"]
    assert isinstance(environment, dict)
    assert environment["REQUIRE_MARIADB_TESTS"] == "1"
    assert environment["OPENAI_API_KEY"] == "ci-placeholder"
    assert "captain_test" in str(environment["TEST_MARIADB_DSN"])

    steps = job["steps"]
    assert isinstance(steps, list)
    commands = "\n".join(str(step.get("run", "")) for step in steps if isinstance(step, dict))
    assert "tests/blockchain/test_mariadb_storage.py" in commands
    assert "tests/gateway/test_gateway.py" in commands
    assert "minibook/tests" in commands
    assert "minibook/requirements.txt" in commands
    assert "--no-cov" in commands
    assert "tests/test_architecture_fitness.py" in commands
    assert "tests/live" not in commands


def test_windows_ci_covers_the_full_deterministic_runtime() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["deterministic_windows"]
    assert isinstance(job, dict)
    assert job["runs-on"] == ["self-hosted", "windows"]

    steps = job["steps"]
    assert isinstance(steps, list)
    checkout = steps[0]
    assert isinstance(checkout, dict)
    assert checkout["uses"] == "actions/checkout@v4"
    checkout_options = checkout["with"]
    assert isinstance(checkout_options, dict)
    assert checkout_options["submodules"] == "recursive"

    commands = "\n".join(str(step.get("run", "")) for step in steps if isinstance(step, dict))
    assert "minibook/requirements.txt" in commands
    assert '-m "not live" --ignore=tests/live' in commands


def test_gate_e_is_manual_and_uses_the_isolated_local_live_runner() -> None:
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    assert "workflow_dispatch" in triggers

    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["gate_e_live"]
    assert isinstance(job, dict)
    assert job["if"] == "github.event_name == 'workflow_dispatch'"
    assert job["runs-on"] == ["self-hosted", "windows"]
    environment = job["env"]
    assert isinstance(environment, dict)
    assert environment["MINIBOOK_BACKEND_URL"] == (
        "${{ vars.MINIBOOK_BACKEND_URL || 'http://127.0.0.1:3456' }}"
    )
    assert environment["MINIBOOK_API_KEY"] == "${{ secrets.MINIBOOK_API_KEY }}"
    assert environment["MINIBOOK_PROJECTION_API_KEY"] == (
        "${{ secrets.MINIBOOK_PROJECTION_API_KEY }}"
    )

    steps = job["steps"]
    assert isinstance(steps, list)
    checkout = steps[0]
    assert isinstance(checkout, dict)
    assert checkout["uses"] == "actions/checkout@v4"
    checkout_options = checkout["with"]
    assert isinstance(checkout_options, dict)
    assert checkout_options["submodules"] == "recursive"
    uses = "\n".join(str(step.get("uses", "")) for step in steps if isinstance(step, dict))
    assert "actions/setup-python" not in uses
    commands = "\n".join(str(step.get("run", "")) for step in steps if isinstance(step, dict))
    assert "scripts/run-gate-e-ci.ps1" in commands

    runner = ROOT / "scripts" / "run-gate-e-ci.ps1"
    assert runner.is_file()
    source = runner.read_text(encoding="utf-8")
    assert "TEST_MARIADB_DSN" in source
    assert "run-gate-e.ps1" in source
    assert "MINIBOOK_PROJECTION_API_KEY" in source
    assert "finally" in source
