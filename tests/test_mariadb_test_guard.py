from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
DATABASE_TEST_MODULES = (
    "tests/blockchain/test_mariadb_storage.py",
    "tests/gateway/test_gateway.py",
)


def _selected_summary_validator_source(source: str) -> str:
    function_marker = "function Assert-SelectedPytestSummary"
    if function_marker in source:
        function_start = source.index(function_marker)
        function_end = source.index("$repoRoot =", function_start)
        return source[function_start:function_end]

    legacy_start = source.index('$selectedText = $selectedOutput -join "`n"')
    legacy_end = source.index("$fullArguments =", legacy_start)
    legacy_body = source[legacy_start:legacy_end].replace(
        "$selectedOutput", "$SelectedOutput"
    )
    return f'''function Assert-SelectedPytestSummary {{
    param([string[]]$SelectedOutput)
{legacy_body}
}}
'''


def test_guard_accepts_only_the_isolated_database() -> None:
    from tests.support.mariadb import assert_isolated_test_database

    assert_isolated_test_database(
        "mariadb://captain_test:encoded%40password@127.0.0.1:33306/captain_test"
    )


@pytest.mark.parametrize(
    "dsn",
    (
        None,
        "",
        "not-a-dsn",
        "sqlite:///captain_test",
        "mysql:///captain_test",
        "mysql://captain_test:secret@127.0.0.1:33306",
        "mysql://captain_test:secret@127.0.0.1:33306/ledger",
        "mysql://captain_test:secret@127.0.0.1:33306/captain_ledger",
        "mysql://captain_test:secret@127.0.0.1:33306/captain_test/extra",
        "mysql://captain_test:secret@127.0.0.1:not-a-port/captain_test",
    ),
)
def test_guard_rejects_non_isolated_or_malformed_dsns(dsn: str | None) -> None:
    from tests.support.mariadb import assert_isolated_test_database

    with pytest.raises(ValueError, match="captain_test"):
        assert_isolated_test_database(dsn)


@pytest.mark.parametrize("test_module", DATABASE_TEST_MODULES)
def test_required_mode_without_a_dsn_is_a_collection_error(test_module: str) -> None:
    environment = os.environ.copy()
    environment.pop("TEST_MARIADB_DSN", None)
    environment["REQUIRE_MARIADB_TESTS"] = "1"

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-cov", "--collect-only", test_module],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "ERROR collecting" in output
    assert "TEST_MARIADB_DSN" in output


@pytest.mark.parametrize("test_module", DATABASE_TEST_MODULES)
def test_every_fixture_clear_is_immediately_guarded(test_module: str) -> None:
    module = ast.parse((ROOT / test_module).read_text(encoding="utf-8"))
    storage_fixture = next(
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "storage"
    )
    clear_indexes = [
        index
        for index, statement in enumerate(storage_fixture.body)
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Attribute)
        and statement.value.func.attr == "clear"
    ]

    assert clear_indexes, "storage fixture must contain destructive clear calls"
    for clear_index in clear_indexes:
        previous = storage_fixture.body[clear_index - 1]
        assert isinstance(previous, ast.Expr)
        assert isinstance(previous.value, ast.Call)
        assert isinstance(previous.value.func, ast.Name)
        assert previous.value.func.id == "assert_isolated_test_database"


def test_compose_service_is_disposable_and_isolated() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.test.yml").read_text(encoding="utf-8"))

    assert compose["name"] == "captain-cook-test"
    assert set(compose["services"]) == {"mariadb-test"}
    service = compose["services"]["mariadb-test"]
    assert service["image"] == "mariadb:11.8.8"
    assert service["environment"] == {
        "MARIADB_DATABASE": "captain_test",
        "MARIADB_USER": "captain_test",
        "MARIADB_PASSWORD": "${MARIADB_TEST_PASSWORD:?required}",
        "MARIADB_ROOT_PASSWORD": "${MARIADB_TEST_ROOT_PASSWORD:?required}",
    }
    assert service["ports"] == ["127.0.0.1:${MARIADB_TEST_PORT:-33306}:3306"]
    assert service["tmpfs"] == ["/var/lib/mysql"]
    assert "volumes" not in service
    assert "env_file" not in service


def test_compose_healthcheck_waits_for_initialized_mariadb() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.test.yml").read_text(encoding="utf-8"))
    healthcheck = compose["services"]["mariadb-test"]["healthcheck"]

    assert healthcheck == {
        "test": ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"],
        "interval": "2s",
        "timeout": "3s",
        "retries": 30,
    }


def test_gate_uses_cryptographic_process_local_encoded_credentials() -> None:
    source = (ROOT / "scripts/test_gateway.ps1").read_text(encoding="utf-8")

    assert "RandomNumberGenerator" in source
    assert source.count("New-RandomCredential") >= 3
    assert "EscapeDataString" in source
    assert "GetEnvironmentVariable" in source
    assert "SetEnvironmentVariable" in source
    assert re.search(r"SetEnvironmentVariable\([^\n]+['\"]Process['\"]", source)
    for name in (
        "MARIADB_TEST_PASSWORD",
        "MARIADB_TEST_ROOT_PASSWORD",
        "MARIADB_TEST_PORT",
        "TEST_MARIADB_DSN",
        "REQUIRE_MARIADB_TESTS",
        "COMPOSE_DISABLE_ENV_FILE",
    ):
        assert name in source
    assert re.search(r"COMPOSE_DISABLE_ENV_FILE[^\n]+['\"]1['\"]", source)
    assert "Set-Content" not in source
    assert "Out-File" not in source


def test_gate_falls_back_when_the_local_venv_cannot_import_pytest() -> None:
    source = (ROOT / "scripts/test_gateway.ps1").read_text(encoding="utf-8")

    assert "Test-PythonCanImportPytest" in source
    assert "-c" in source
    assert '"import pytest"' in source
    assert "Test-PythonCanImportPytest -Python $localPython" in source


def test_environment_helper_preserves_missing_and_empty_values() -> None:
    source = (ROOT / "scripts/test_gateway.ps1").read_text(encoding="utf-8")
    function_start = source.index("function Set-ProcessEnvironmentValue")
    function_end = source.index("function Invoke-Pytest")
    helper = source[function_start:function_end]
    probe = helper + r'''
$name = "CAPTAIN_COOK_P03_ENV_RESTORE_PROBE"
try {
    $nullString = [System.Management.Automation.Language.NullString]::Value
    [Environment]::SetEnvironmentVariable($name, $nullString, "Process")
    $missing = [Environment]::GetEnvironmentVariable($name, "Process")
    Set-ProcessEnvironmentValue -Name $name -Value "changed"
    Set-ProcessEnvironmentValue -Name $name -Value $missing
    if ($null -ne [Environment]::GetEnvironmentVariable($name, "Process")) { exit 11 }

    [Environment]::SetEnvironmentVariable($name, "", "Process")
    $empty = [Environment]::GetEnvironmentVariable($name, "Process")
    Set-ProcessEnvironmentValue -Name $name -Value "changed"
    Set-ProcessEnvironmentValue -Name $name -Value $empty
    $restored = [Environment]::GetEnvironmentVariable($name, "Process")
    if ($null -eq $restored -or $restored.Length -ne 0) { exit 12 }
} finally {
    [Environment]::SetEnvironmentVariable($name, $nullString, "Process")
}
'''

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_selected_summary_validator_accepts_growth_and_rejects_invalid_results() -> None:
    source = (ROOT / "scripts/test_gateway.ps1").read_text(encoding="utf-8")
    validator = _selected_summary_validator_source(source)
    probe = validator + r'''
$failures = [System.Collections.Generic.List[string]]::new()

function Test-ValidatorCase {
    param(
        [string]$Name,
        [string[]]$Output,
        [bool]$ShouldAccept
    )

    $accepted = $true
    try {
        $null = Assert-SelectedPytestSummary -SelectedOutput $Output
    } catch {
        $accepted = $false
    }
    if ($accepted -ne $ShouldAccept) {
        [void]$failures.Add("$Name expected accepted=$ShouldAccept but got accepted=$accepted")
    }
}

Test-ValidatorCase -Name "baseline-with-blank-lines" -Output @("", "22 passed in 0.10s", "") -ShouldAccept $true
Test-ValidatorCase -Name "growth" -Output @("37 passed, 1 warning in 0.20s") -ShouldAccept $true
Test-ValidatorCase -Name "below-baseline" -Output @("21 passed in 0.10s") -ShouldAccept $false
Test-ValidatorCase -Name "selected-skip" -Output @("22 passed, 1 skipped in 0.10s") -ShouldAccept $false
Test-ValidatorCase -Name "missing-summary" -Output @("no tests ran in 0.10s") -ShouldAccept $false
Test-ValidatorCase -Name "duplicate-summary" -Output @("22 passed in 0.10s", "23 passed in 0.11s") -ShouldAccept $false

if ($failures.Count -gt 0) {
    throw $failures -join "; "
}
'''

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_gate_uses_only_the_absolute_isolated_compose_project() -> None:
    source = (ROOT / "scripts/test_gateway.ps1").read_text(encoding="utf-8")

    assert "[System.IO.Path]::GetFullPath" in source
    assert re.search(
        r"compose\s+--project-name\s+captain-cook-test\s+--file\s+\$composeFile\s+up\s+-d\s+--wait",
        source,
        re.IGNORECASE,
    )
    down_match = re.search(
        r"compose\s+--project-name\s+captain-cook-test\s+--file\s+\$composeFile\s+down[^\r\n]*",
        source,
        re.IGNORECASE,
    )
    assert down_match is not None
    assert "--remove-orphans" in down_match.group(0)
    assert "--volumes" not in down_match.group(0)
    assert not re.search(r"(?:^|\s)-v(?:\s|$)", down_match.group(0), re.IGNORECASE)
    assert "docker-compose.yml" not in source
    assert "ledger_data" not in source
    assert "--env-file" not in source


def test_gate_enables_required_encoded_dsn_after_mariadb_is_healthy() -> None:
    source = (ROOT / "scripts/test_gateway.ps1").read_text(encoding="utf-8")

    up_index = source.index("up -d --wait")
    dsn_index = source.index('Set-ProcessEnvironmentValue -Name "TEST_MARIADB_DSN"')
    selected_index = source.index("$selectedArguments")
    assert up_index < dsn_index < selected_index
    assert re.search(
        r'Set-ProcessEnvironmentValue\s+-Name\s+"REQUIRE_MARIADB_TESTS"\s+-Value\s+"1"',
        source,
    )
    assert "mariadb://" in source
    assert "127.0.0.1" in source
    assert "captain_test" in source
    assert source.count("EscapeDataString") >= 3


def test_gate_enforces_selected_count_and_classified_full_suite_skips() -> None:
    source = (ROOT / "scripts/test_gateway.ps1").read_text(encoding="utf-8")
    validator = _selected_summary_validator_source(source)

    assert "tests/blockchain/test_mariadb_storage.py" in source
    assert "tests/gateway/test_gateway.py" in source
    assert "--no-cov" in source
    assert "Assert-SelectedPytestSummary" in source
    assert "[int]::TryParse" in validator
    assert re.search(r"\$passedCount\s+-lt\s+22", validator, re.IGNORECASE)
    assert not re.search(
        r"\$passedCount\s+-(?:eq|ne|gt|ge|le)\s+22", validator, re.IGNORECASE
    )
    assert re.search(r'["\']-m["\']\s*,\s*["\']not live["\']', source)
    assert "AllowedFullSuiteSkipPatterns" in source
    assert "tests/test_captain_supply_chain" in source
    assert "tests/ledger_bridge/test_query" in source
    assert "Unexpected full-suite skip" in source
    assert source.count("$LASTEXITCODE") >= 3
    assert "finally" in source
