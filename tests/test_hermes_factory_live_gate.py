from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

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


def _load_live_contract_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("factory_live_contract", LIVE_TEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_copied_wrapper(
    tmp_path: Path,
    *,
    environment: dict[str, str],
    with_n8n: bool = False,
    max_cost_usd: str = "1.00",
) -> subprocess.CompletedProcess[str]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    copied_wrapper = scripts / WRAPPER.name
    shutil.copyfile(WRAPPER, copied_wrapper)
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    arguments = [
        pwsh,
        "-NoProfile",
        "-File",
        str(copied_wrapper),
        "-Mode",
        "demo",
        "-MaxCostUsd",
        max_cost_usd,
        "-Model",
        "fixture-model",
    ]
    if with_n8n:
        arguments.append("-WithN8n")
    return subprocess.run(
        arguments,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _write_fake_live_gate_commands(
    directory: Path,
    leak_probe: str,
    *,
    live_report: dict[str, object] | None = None,
    preflight_extra: dict[str, object] | None = None,
) -> None:
    directory.mkdir()
    (directory / "docker.cmd").write_text(
        '@echo off\nif "%1"=="ps" echo fixture-container\nexit /b 0\n',
        encoding="utf-8",
    )
    skill_lines = "\n".join(f"echo {skill}" for skill in SKILLS)
    (directory / "hermes.cmd").write_text(
        f"@echo off\n{skill_lines}\nexit /b 0\n",
        encoding="utf-8",
    )
    (directory / "codex.cmd").write_text(
        "@echo off\nexit /b 0\n",
        encoding="utf-8",
    )
    preflight = {
        "schema": "captain.hermes-six-skill-factory-preflight.v1",
        "prerequisites_confirmed": True,
        "database_name": "captain_test",
        "services_verified": True,
        "codex_authenticated": True,
        "skills_verified": True,
        "skill_digests": {skill: "a" * 64 for skill in SKILLS},
    }
    if preflight_extra is not None:
        preflight.update(preflight_extra)
    preflight_json = json.dumps(preflight, separators=(",", ":"))
    if live_report is None:
        pytest_action = (
            f"echo {leak_probe}\n"
            f">&2 echo {leak_probe}\n"
            "exit /b 17\n"
        )
    else:
        report_json = json.dumps(live_report, separators=(",", ":"))
        report_bytes = (report_json + "\r\n").encode("ascii")
        report_digest = hashlib.sha256(report_bytes).hexdigest()
        batch_report_json = report_json.replace("%", "%%")
        pytest_action = (
            f'>"%CAPTAIN_FACTORY_REPORT_DIRECTORY%\\sha256-{report_digest}.json" '
            f"echo {batch_report_json}\n"
            "exit /b 0\n"
        )
    (directory / "python.cmd").write_text(
        "@echo off\n"
        "setlocal EnableDelayedExpansion\n"
        'if defined CAPTAIN_N8N_URL >"%CD%\\n8n-child-env-present.txt" echo present\n'
        'if defined CAPTAIN_N8N_API_KEY >"%CD%\\n8n-child-env-present.txt" echo present\n'
        'if defined CAPTAIN_N8N_MCP_TOKEN >"%CD%\\n8n-child-env-present.txt" echo present\n'
        'set "output="\n'
        'set "previous="\n'
        "for %%A in (%*) do (\n"
        '  if "!previous!"=="--output" set "output=%%~A"\n'
        '  set "previous=%%~A"\n'
        ")\n"
        "if defined output (\n"
        f'  >"!output!" echo {preflight_json}\n'
        "  exit /b 0\n"
        ")\n"
        f"{pytest_action}",
        encoding="utf-8",
    )


def _valid_demo_live_report() -> dict[str, object]:
    return {
        "schema": "captain.hermes-six-skill-factory-live-report.v1",
        "mode": "demo",
        "prerequisites_confirmed": True,
        "live_execution": True,
        "model": "fixture-model",
        "database_name": "captain_test",
        "context7_provenance_digest": "1" * 64,
        "job_id": "00000000-0000-0000-0000-000000000201",
        "correlation_id": "00000000-0000-0000-0000-000000000202",
        "subject_version": 1,
        "attempt": 1,
        "provider_traces": [
            {
                "trace_id": "artifact://provider-traces/demo-1",
                "codex_session_id": "https://codex.invalid/sessions/demo-1",
                "hermes_session_id": "holdout://hermes-sessions/demo-1",
                "provider": "https://api.openai.com",
                "model": "fixture-model",
                "status": "succeeded",
                "cost_usd": "0.10",
                "usage_receipt_ref": {
                    "uri": "artifact://usage-receipts/demo-1",
                    "sha256": "2" * 64,
                    "media_type": "application/json",
                },
                "budget_receipt_ref": {
                    "uri": "artifact://budget-receipts/demo-1",
                    "sha256": "3" * 64,
                    "media_type": "application/json",
                },
            }
        ],
        "total_cost_usd": "0.10",
        "terminal_status": "demo_ready",
        "with_n8n": False,
    }


def test_live_gate_wrapper_has_the_exact_paid_gate_parameters() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    assert "[ValidateSet('demo', 'release')]" in source
    assert "[string]$Mode = 'demo'" in source
    assert "[Parameter(Mandatory)]" in source
    assert "[decimal]$MaxCostUsd" in source
    assert "[string]$Model" in source
    assert "[switch]$WithN8n" in source


@pytest.mark.parametrize("max_cost_usd", ("1.001", "0.999"))
def test_live_gate_rejects_more_than_two_cost_decimals_before_formatting(
    tmp_path: Path,
    max_cost_usd: str,
) -> None:
    environment = os.environ.copy()
    environment.pop("TEST_MARIADB_DSN", None)

    result = _run_copied_wrapper(
        tmp_path,
        environment=environment,
        max_cost_usd=max_cost_usd,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "at most two fractional decimal places" in output
    assert "TEST_MARIADB_DSN is required" not in output
    source = WRAPPER.read_text(encoding="utf-8")
    assert source.index("[decimal]::GetBits($MaxCostUsd)") < source.index(
        "$MaxCostUsd.ToString('0.00'"
    )


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


def test_live_gate_loads_only_allowlisted_local_env_files_before_validation() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    assert "Import-AllowlistedEnvironmentFile" in source
    assert "Join-Path $root '.env'" in source
    assert "Join-Path $root '.env.captain-n8n'" in source
    assert "TEST_MARIADB_DSN" in source
    assert "CAPTAIN_FACTORY_MODEL" in source
    assert "CAPTAIN_N8N_API_KEY" in source
    assert "[EnvironmentVariableTarget]::Process" in source
    dedicated_load = source.index(
        "Import-AllowlistedEnvironmentFile -Path (Join-Path $root '.env.captain-n8n')"
    )
    root_load = source.index(
        "Import-AllowlistedEnvironmentFile -Path (Join-Path $root '.env')"
    )
    validation = source.index("$databaseDsn = Get-RequiredEnvironmentValue")
    assert dedicated_load < root_load < validation


def test_live_gate_loads_an_allowlisted_dsn_without_printing_it(
    tmp_path: Path,
) -> None:
    secret = "env-fixture-password-must-not-be-printed"
    (tmp_path / ".env").write_text(
        "UNRELATED_SHOULD_NOT_LOAD=ignored\n"
        f"TEST_MARIADB_DSN=mysql+pymysql://captain:{secret}@127.0.0.1:3306/production\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("TEST_MARIADB_DSN", None)

    result = _run_copied_wrapper(tmp_path, environment=environment)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "captain_test" in output
    assert "TEST_MARIADB_DSN is required" not in output
    assert secret not in output


def test_process_environment_overrides_local_env_files_without_live_calls(
    tmp_path: Path,
) -> None:
    file_secret = "file-password-must-not-be-printed"
    process_secret = "process-password-must-not-be-printed"
    (tmp_path / ".env").write_text(
        f"TEST_MARIADB_DSN=mysql+pymysql://captain:{file_secret}@127.0.0.1:3306/production\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["TEST_MARIADB_DSN"] = (
        f"mysql+pymysql://captain:{process_secret}@127.0.0.1:3306/captain_test"
    )
    environment["PATH"] = ""

    result = _run_copied_wrapper(tmp_path, environment=environment)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Required command is unavailable: docker" in output
    assert "captain_test database" not in output
    assert file_secret not in output
    assert process_secret not in output


def test_dedicated_n8n_env_overrides_root_fallback_without_live_calls(
    tmp_path: Path,
) -> None:
    root_secret = "root-n8n-key-must-not-be-printed"
    (tmp_path / ".env").write_text(
        f"CAPTAIN_N8N_API_KEY={root_secret}\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.captain-n8n").write_text(
        'CAPTAIN_N8N_API_KEY="   "\n',
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["TEST_MARIADB_DSN"] = (
        "mysql+pymysql://captain:db-password-not-in-output@127.0.0.1:3306/captain_test"
    )
    environment["CAPTAIN_N8N_URL"] = "https://n8n.invalid"
    environment["CAPTAIN_N8N_MCP_TOKEN"] = "opaque-fixture-token"
    environment.pop("CAPTAIN_N8N_API_KEY", None)

    result = _run_copied_wrapper(
        tmp_path,
        environment=environment,
        with_n8n=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "CAPTAIN_N8N_API_KEY is required" in output
    assert "Required command is unavailable" not in output
    assert root_secret not in output


def test_process_n8n_environment_overrides_dedicated_and_root_files(
    tmp_path: Path,
) -> None:
    file_secret = "file-n8n-key-must-not-be-printed"
    process_secret = "process-n8n-key-must-not-be-printed"
    (tmp_path / ".env").write_text(
        f"CAPTAIN_N8N_API_KEY={file_secret}\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.captain-n8n").write_text(
        'CAPTAIN_N8N_API_KEY="   "\n',
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["TEST_MARIADB_DSN"] = (
        "mysql+pymysql://captain:db-password-not-in-output@127.0.0.1:3306/captain_test"
    )
    environment["CAPTAIN_N8N_URL"] = "https://n8n.invalid"
    environment["CAPTAIN_N8N_API_KEY"] = process_secret
    environment["CAPTAIN_N8N_MCP_TOKEN"] = "opaque-fixture-token"
    environment["PATH"] = ""

    result = _run_copied_wrapper(
        tmp_path,
        environment=environment,
        with_n8n=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Required command is unavailable: docker" in output
    assert "CAPTAIN_N8N_API_KEY is required" not in output
    assert file_secret not in output
    assert process_secret not in output


def test_non_n8n_gate_removes_inherited_and_file_n8n_values_from_children(
    tmp_path: Path,
) -> None:
    fake_commands = tmp_path / "fake-bin"
    _write_fake_live_gate_commands(fake_commands, "sanitized-provider-failure")
    (tmp_path / ".env").write_text(
        "CAPTAIN_N8N_URL=https://root.invalid\n"
        "CAPTAIN_N8N_API_KEY=root-key\n"
        "CAPTAIN_N8N_MCP_TOKEN=root-token\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.captain-n8n").write_text(
        "CAPTAIN_N8N_URL=https://dedicated.invalid\n"
        "CAPTAIN_N8N_API_KEY=dedicated-key\n"
        "CAPTAIN_N8N_MCP_TOKEN=dedicated-token\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["TEST_MARIADB_DSN"] = (
        "mysql+pymysql://captain:db-password-not-in-output@127.0.0.1:3306/captain_test"
    )
    environment["CAPTAIN_N8N_URL"] = "https://process.invalid"
    environment["CAPTAIN_N8N_API_KEY"] = "process-key"
    environment["CAPTAIN_N8N_MCP_TOKEN"] = "process-token"
    environment["PATH"] = str(fake_commands)

    result = _run_copied_wrapper(tmp_path, environment=environment)

    assert result.returncode != 0
    assert not (tmp_path / "n8n-child-env-present.txt").exists()


def test_n8n_gate_loads_dedicated_values_for_children(tmp_path: Path) -> None:
    fake_commands = tmp_path / "fake-bin"
    _write_fake_live_gate_commands(fake_commands, "sanitized-provider-failure")
    (tmp_path / ".env.captain-n8n").write_text(
        "CAPTAIN_N8N_URL=https://dedicated.invalid\n"
        "CAPTAIN_N8N_API_KEY=dedicated-key\n"
        "CAPTAIN_N8N_MCP_TOKEN=dedicated-token\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["TEST_MARIADB_DSN"] = (
        "mysql+pymysql://captain:db-password-not-in-output@127.0.0.1:3306/captain_test"
    )
    for name in ("CAPTAIN_N8N_URL", "CAPTAIN_N8N_API_KEY", "CAPTAIN_N8N_MCP_TOKEN"):
        environment.pop(name, None)
    environment["PATH"] = str(fake_commands)

    result = _run_copied_wrapper(
        tmp_path,
        environment=environment,
        with_n8n=True,
    )

    assert result.returncode != 0
    assert (tmp_path / "n8n-child-env-present.txt").read_text(
        encoding="utf-8"
    ).strip() == "present"


def test_wrapper_suppresses_failed_pytest_output_and_emits_only_a_generic_error(
    tmp_path: Path,
) -> None:
    leak_probe = "provider-secret-from-pytest-output"
    fake_commands = tmp_path / "fake-bin"
    _write_fake_live_gate_commands(fake_commands, leak_probe)
    environment = os.environ.copy()
    environment["TEST_MARIADB_DSN"] = (
        "mysql+pymysql://captain:db-password-not-in-output@127.0.0.1:3306/captain_test"
    )
    environment["PATH"] = str(fake_commands)

    result = _run_copied_wrapper(tmp_path, environment=environment)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Factory live validation failed without releasing test output." in output
    assert leak_probe not in output


def test_wrapper_accepts_a_valid_content_addressed_report_with_uri_schemes(
    tmp_path: Path,
) -> None:
    fake_commands = tmp_path / "fake-bin"
    _write_fake_live_gate_commands(
        fake_commands,
        "unused-leak-probe",
        live_report=_valid_demo_live_report(),
    )
    environment = os.environ.copy()
    environment["TEST_MARIADB_DSN"] = (
        "mysql+pymysql://captain:db-password-not-in-output@127.0.0.1:3306/captain_test"
    )
    environment["PATH"] = str(fake_commands)

    result = _run_copied_wrapper(tmp_path, environment=environment)

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "gate passed; report sha256=" in output


def test_wrapper_rejects_an_unknown_report_field_after_successful_pytest(
    tmp_path: Path,
) -> None:
    report = {**_valid_demo_live_report(), "unexpected": "benign-but-unknown"}
    fake_commands = tmp_path / "fake-bin"
    _write_fake_live_gate_commands(
        fake_commands,
        "unused-leak-probe",
        live_report=report,
    )
    environment = os.environ.copy()
    environment["TEST_MARIADB_DSN"] = (
        "mysql+pymysql://captain:db-password-not-in-output@127.0.0.1:3306/captain_test"
    )
    environment["PATH"] = str(fake_commands)

    result = _run_copied_wrapper(tmp_path, environment=environment)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "exact live-report contract" in output
    assert "benign-but-unknown" not in output


def test_wrapper_rejects_an_unknown_nested_preflight_field(
    tmp_path: Path,
) -> None:
    leak_probe = "opaque-preflight-credential-material"
    fake_commands = tmp_path / "fake-bin"
    _write_fake_live_gate_commands(
        fake_commands,
        "unused-leak-probe",
        live_report=_valid_demo_live_report(),
        preflight_extra={"unexpected": {"credential_material": leak_probe}},
    )
    environment = os.environ.copy()
    environment["TEST_MARIADB_DSN"] = (
        "mysql+pymysql://captain:db-password-not-in-output@127.0.0.1:3306/captain_test"
    )
    environment["PATH"] = str(fake_commands)

    result = _run_copied_wrapper(tmp_path, environment=environment)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "exact preflight contract" in output
    assert leak_probe not in output


@pytest.mark.parametrize(
    "boolean_field",
    (
        "prerequisites_confirmed",
        "services_verified",
        "codex_authenticated",
        "skills_verified",
    ),
)
def test_wrapper_rejects_stringified_preflight_booleans(
    tmp_path: Path,
    boolean_field: str,
) -> None:
    fake_commands = tmp_path / "fake-bin"
    _write_fake_live_gate_commands(
        fake_commands,
        "unused-leak-probe",
        live_report=_valid_demo_live_report(),
        preflight_extra={boolean_field: "true"},
    )
    environment = os.environ.copy()
    environment["TEST_MARIADB_DSN"] = (
        "mysql+pymysql://captain:db-password-not-in-output@127.0.0.1:3306/captain_test"
    )
    environment["PATH"] = str(fake_commands)

    result = _run_copied_wrapper(tmp_path, environment=environment)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "did not confirm every required prerequisite" in output


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
    assert ".env.captain-n8n" in runbook
    assert "FactoryReleaseDecision" in runbook
    assert "FactoryEvidenceBlock" in runbook


def test_preflight_and_final_report_are_redacted_before_json_is_consumed_or_printed() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    assert "Assert-RedactedJsonFile" in source
    preflight_redaction = source.index("Assert-RedactedJsonFile -Path $preflightPath")
    preflight_parse = source.index("ConvertFrom-Json", preflight_redaction)
    assert preflight_redaction < preflight_parse
    report_redaction = source.index("Assert-RedactedJsonFile -Path $report.FullName")
    final_output = source.index("Write-Output")
    assert report_redaction < final_output
    for forbidden in ("access[_-]?token", "raw[_-]?prompt", "bearer", "private", "path"):
        assert forbidden in source.lower()


@pytest.mark.parametrize(
    "payload",
    (
        {"access_token": "redacted"},
        {"raw_prompt": "redacted"},
        {"private": "redacted"},
        {"nested": {"private_holdout": "redacted"}},
        {"header": "Bearer credential"},
        {"diagnostic": "sk-proj-fixture-secret"},
        {"message": "https://captain:credential@service.invalid/status"},
        {"path": "relative-is-still-host-metadata"},
        {"message": r"C:\\Users\\someone\\secret.txt"},
    ),
)
def test_live_report_redaction_contract_rejects_sensitive_keys_and_values(
    payload: object,
) -> None:
    live_contract = _load_live_contract_module()

    with pytest.raises(AssertionError):
        live_contract._assert_redacted(payload)


def test_live_report_rejects_every_known_allowlisted_secret_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_contract = _load_live_contract_module()
    dsn = "mysql+pymysql://captain:db-fixture-password@127.0.0.1:3306/captain_test"
    n8n_url = "https://n8n-user:n8n-url-password@n8n.invalid"
    monkeypatch.setenv("TEST_MARIADB_DSN", dsn)
    monkeypatch.setenv("CAPTAIN_N8N_URL", n8n_url)
    monkeypatch.setenv("CAPTAIN_N8N_API_KEY", "n8n-api-fixture-value")
    monkeypatch.setenv("CAPTAIN_N8N_MCP_TOKEN", "n8n-mcp-fixture-value")

    forbidden_values = live_contract._known_secret_values_from_environment()
    for secret in (
        dsn,
        "db-fixture-password",
        n8n_url,
        "n8n-url-password",
        "n8n-api-fixture-value",
        "n8n-mcp-fixture-value",
    ):
        with pytest.raises(AssertionError):
            live_contract._assert_redacted(
                {"message": f"prefix-{secret}-suffix"},
                forbidden_values=forbidden_values,
            )


def test_live_report_rejects_raw_and_decoded_percent_encoded_uri_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_contract = _load_live_contract_module()
    monkeypatch.setenv(
        "TEST_MARIADB_DSN",
        "mysql+pymysql://captain:p%40ss%2Fword@127.0.0.1:3306/captain_test",
    )
    monkeypatch.setenv(
        "CAPTAIN_N8N_URL",
        "https://n8n-user:n8n%40pass%2Fword@n8n.invalid",
    )

    forbidden_values = live_contract._known_secret_values_from_environment()
    expected_values = {
        "captain:p%40ss%2Fword",
        "captain:p@ss/word",
        "p%40ss%2Fword",
        "p@ss/word",
        "n8n-user:n8n%40pass%2Fword",
        "n8n-user:n8n@pass/word",
        "n8n%40pass%2Fword",
        "n8n@pass/word",
    }
    assert expected_values <= set(forbidden_values)
    report = _valid_demo_live_report()
    trace = report["provider_traces"][0]
    assert isinstance(trace, dict)
    trace["provider"] = "p%40ss%2Fword"
    serialized = live_contract._serialize_live_report(report)

    with pytest.raises(AssertionError):
        live_contract._assert_redacted(
            serialized,
            forbidden_values=forbidden_values,
        )


@pytest.mark.parametrize(
    ("secret_source", "report_target", "with_n8n"),
    (
        ("dsn_password", "provider", False),
        ("n8n_api_key", "n8n_mcp_call_id", True),
        ("n8n_mcp_token", "n8n_execution_id", True),
    ),
)
def test_wrapper_rejects_known_secret_values_in_schema_valid_report_fields(
    tmp_path: Path,
    secret_source: str,
    report_target: str,
    with_n8n: bool,
) -> None:
    secrets = {
        "dsn_password": "db-known-fixture-value",
        "n8n_api_key": "api-known-fixture-value",
        "n8n_mcp_token": "mcp-known-fixture-value",
    }
    secret = secrets[secret_source]
    report = _valid_demo_live_report()
    trace = report["provider_traces"][0]
    assert isinstance(trace, dict)
    if report_target == "provider":
        trace["provider"] = secret
    else:
        report["with_n8n"] = True
        report["n8n_evidence"] = {
            "workflow_digest": "4" * 64,
            "n8n_mcp_call_id": (
                secret if report_target == "n8n_mcp_call_id" else "call-redacted"
            ),
            "n8n_execution_id": (
                secret if report_target == "n8n_execution_id" else "execution-redacted"
            ),
        }
    fake_commands = tmp_path / "fake-bin"
    _write_fake_live_gate_commands(
        fake_commands,
        "unused-leak-probe",
        live_report=report,
    )
    environment = os.environ.copy()
    environment["TEST_MARIADB_DSN"] = (
        "mysql+pymysql://captain:db-known-fixture-value@127.0.0.1:3306/captain_test"
    )
    environment["CAPTAIN_N8N_URL"] = "https://n8n.invalid"
    environment["CAPTAIN_N8N_API_KEY"] = secrets["n8n_api_key"]
    environment["CAPTAIN_N8N_MCP_TOKEN"] = secrets["n8n_mcp_token"]
    environment["PATH"] = str(fake_commands)

    result = _run_copied_wrapper(
        tmp_path,
        environment=environment,
        with_n8n=with_n8n,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "forbidden sensitive material" in output
    assert secret not in output


@pytest.mark.parametrize("secret_source", ("dsn", "n8n_url"))
def test_wrapper_rejects_raw_percent_encoded_uri_passwords(
    tmp_path: Path,
    secret_source: str,
) -> None:
    encoded_password = (
        "p%40ss%2Fword" if secret_source == "dsn" else "n8n%40secret%2Fword"
    )
    report = _valid_demo_live_report()
    trace = report["provider_traces"][0]
    assert isinstance(trace, dict)
    trace["provider"] = encoded_password
    with_n8n = secret_source == "n8n_url"
    if with_n8n:
        report["with_n8n"] = True
        report["n8n_evidence"] = {
            "workflow_digest": "4" * 64,
            "n8n_mcp_call_id": "call-redacted",
            "n8n_execution_id": "execution-redacted",
        }
    fake_commands = tmp_path / "fake-bin"
    _write_fake_live_gate_commands(
        fake_commands,
        "unused-leak-probe",
        live_report=report,
    )
    environment = os.environ.copy()
    environment["TEST_MARIADB_DSN"] = (
        "mysql+pymysql://captain:"
        f"{'p%40ss%2Fword' if secret_source == 'dsn' else 'safe-db-password'}"
        "@127.0.0.1:3306/captain_test"
    )
    environment["CAPTAIN_N8N_URL"] = (
        "https://n8n-user:n8n%40secret%2Fword@n8n.invalid"
    )
    environment["CAPTAIN_N8N_API_KEY"] = "api-known-fixture-value"
    environment["CAPTAIN_N8N_MCP_TOKEN"] = "mcp-known-fixture-value"
    environment["PATH"] = str(fake_commands)

    result = _run_copied_wrapper(
        tmp_path,
        environment=environment,
        with_n8n=with_n8n,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "forbidden sensitive material" in output
    assert encoded_password not in output


def test_live_runner_exception_is_sanitized_outside_the_original_exception_context() -> None:
    live_contract = _load_live_contract_module()
    leak_probe = "provider-secret-from-runtime-exception"

    async def leaking_runner() -> None:
        raise RuntimeError(leak_probe)

    with pytest.raises(pytest.fail.Exception) as captured:
        asyncio.run(live_contract._run_sanitized_live_gate(leaking_runner))

    assert leak_probe not in str(captured.value)
    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None

    async def leaking_pytest_runner() -> None:
        pytest.fail(leak_probe)

    with pytest.raises(pytest.fail.Exception) as pytest_captured:
        asyncio.run(live_contract._run_sanitized_live_gate(leaking_pytest_runner))

    assert leak_probe not in str(pytest_captured.value)
    assert pytest_captured.value.__context__ is None
    assert pytest_captured.value.__cause__ is None


def test_live_report_is_serialized_through_an_exact_extra_forbid_schema() -> None:
    live_contract = _load_live_contract_module()
    valid_report = _valid_demo_live_report()

    assert live_contract._serialize_live_report(valid_report) == valid_report
    with pytest.raises(pytest.fail.Exception) as captured:
        live_contract._serialize_live_report(
            {**valid_report, "unexpected": "must-not-be-accepted"}
        )

    assert "must-not-be-accepted" not in str(captured.value)
    assert captured.value.__context__ is None


def test_live_report_requires_exact_decimal_strings_and_exact_gateway_refs() -> None:
    live_contract = _load_live_contract_module()
    job_id = "00000000-0000-0000-0000-000000000101"
    correlation_id = "00000000-0000-0000-0000-000000000102"
    artifact_ref = {
        "uri": "artifact://sha256/release-proof",
        "sha256": "a" * 64,
        "media_type": "application/json",
    }
    evaluation_ref = {**artifact_ref, "sha256": "d" * 64}
    gateway_promotion = {
        "projection_status": "ready_to_use",
        "release_decision": {
            "job_id": job_id,
            "correlation_id": correlation_id,
            "status": "ready",
            "reasons": ["release evidence verified"],
            "evaluation_id": "00000000-0000-0000-0000-000000000104",
            "evaluation_ref": evaluation_ref,
            "tool_gaps": [],
        },
        "promotion_block": {
            "schema": "captain.agent-factory-block.v1",
            "event_id": "00000000-0000-0000-0000-000000000103",
            "job_id": job_id,
            "correlation_id": correlation_id,
            "causation_id": None,
            "occurred_at": "2026-07-21T15:00:00Z",
            "producer": "captain",
            "subject_version": 2,
            "attempt": 3,
            "phase": "capability_promoted",
            "role": None,
            "status": "succeeded",
            "artifact_refs": [evaluation_ref],
            "evidence_refs": [artifact_ref],
            "assertion_ids": ["release_evidence_complete"],
            "lease_id": None,
        },
    }
    report_binding = {
        "job_id": job_id,
        "correlation_id": correlation_id,
        "subject_version": 2,
        "attempt": 3,
    }

    assert live_contract._exact_usd("0.10", "cost") == Decimal("0.10")
    assert live_contract._gateway_promotion(gateway_promotion, report_binding) is None
    with pytest.raises(AssertionError):
        live_contract._exact_usd(0.1, "cost")
    with pytest.raises(AssertionError):
        live_contract._exact_usd("1e-1", "cost")
    with pytest.raises(AssertionError):
        live_contract._gateway_promotion("ready_to_use", report_binding)
    with pytest.raises(AssertionError):
        live_contract._gateway_promotion(
            {
                **gateway_promotion,
                "promotion_block": {
                    **gateway_promotion["promotion_block"],
                    "correlation_id": "00000000-0000-0000-0000-000000000999",
                },
            },
            report_binding,
        )
    with pytest.raises(AssertionError):
        live_contract._gateway_promotion(
            {
                **gateway_promotion,
                "release_decision": {
                    key: value
                    for key, value in gateway_promotion["release_decision"].items()
                    if key != "tool_gaps"
                },
            },
            report_binding,
        )
    with pytest.raises(ValidationError):
        live_contract._gateway_promotion(
            {
                **gateway_promotion,
                "promotion_block": {
                    **gateway_promotion["promotion_block"],
                    "evidence_refs": [],
                },
            },
            report_binding,
        )
    with pytest.raises(AssertionError):
        live_contract._gateway_promotion(
            {
                **gateway_promotion,
                "promotion_block": {
                    **gateway_promotion["promotion_block"],
                    "artifact_refs": [{**artifact_ref, "sha256": "e" * 64}],
                },
            },
            report_binding,
        )
    with pytest.raises(AssertionError):
        live_contract._gateway_promotion(
            {
                **gateway_promotion,
                "release_decision": {
                    **gateway_promotion["release_decision"],
                    "evaluation_id": None,
                    "evaluation_ref": None,
                },
            },
            report_binding,
        )


def test_powershell_gateway_check_binds_evaluation_to_promotion_artifacts() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    assert "$decision.evaluation_id" in source
    assert "$decision.evaluation_ref" in source
    assert "$block.artifact_refs" in source


def test_live_contract_requires_unique_codex_sessions_and_lowercase_n8n_digest() -> None:
    source = LIVE_TEST.read_text(encoding="utf-8")

    assert "codex_session_ids" in source
    assert "len(codex_session_ids) == len(set(codex_session_ids))" in source
    assert "_exact_usd" in source
    assert "_gateway_promotion" in source
    assert "^[0-9a-f]{64}$" in source


def test_live_contract_rejects_n8n_evidence_without_opt_in() -> None:
    live_contract = _load_live_contract_module()
    assert live_contract._validate_n8n_evidence({}, with_n8n=False) is None

    with pytest.raises(AssertionError):
        live_contract._validate_n8n_evidence(
            {
                "n8n_evidence": {
                    "workflow_digest": "a" * 64,
                    "n8n_mcp_call_id": "call-1",
                    "n8n_execution_id": "execution-1",
                }
            },
            with_n8n=False,
        )


def test_submission_inventory_includes_factory_live_operations_files() -> None:
    assert "scripts/run-hermes-factory-live-gate.ps1" in REQUIRED_FILES
    assert "tests/live/test_hermes_six_skill_factory_live.py" in REQUIRED_FILES
    assert "docs/AGENT_FACTORY_RUNBOOK.md" in REQUIRED_FILES
