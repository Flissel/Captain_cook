from __future__ import annotations

import asyncio
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
        "1.00",
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


def _write_fake_live_gate_commands(directory: Path, leak_probe: str) -> None:
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
    preflight_json = json.dumps(preflight, separators=(",", ":"))
    (directory / "python.cmd").write_text(
        "@echo off\n"
        "setlocal EnableDelayedExpansion\n"
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
        f"echo {leak_probe}\n"
        f">&2 echo {leak_probe}\n"
        "exit /b 17\n",
        encoding="utf-8",
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
        "mysql+pymysql://captain:fixture@127.0.0.1:3306/captain_test"
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
        "mysql+pymysql://captain:fixture@127.0.0.1:3306/captain_test"
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


def test_wrapper_suppresses_failed_pytest_output_and_emits_only_a_generic_error(
    tmp_path: Path,
) -> None:
    leak_probe = "provider-secret-from-pytest-output"
    fake_commands = tmp_path / "fake-bin"
    _write_fake_live_gate_commands(fake_commands, leak_probe)
    environment = os.environ.copy()
    environment["TEST_MARIADB_DSN"] = (
        "mysql+pymysql://captain:fixture@127.0.0.1:3306/captain_test"
    )
    environment["PATH"] = str(fake_commands)

    result = _run_copied_wrapper(tmp_path, environment=environment)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Factory live validation failed without releasing test output." in output
    assert leak_probe not in output


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


def test_submission_inventory_includes_factory_live_operations_files() -> None:
    assert "scripts/run-hermes-factory-live-gate.ps1" in REQUIRED_FILES
    assert "tests/live/test_hermes_six_skill_factory_live.py" in REQUIRED_FILES
    assert "docs/AGENT_FACTORY_RUNBOOK.md" in REQUIRED_FILES
