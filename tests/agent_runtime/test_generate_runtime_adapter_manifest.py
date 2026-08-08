"""Behavioral coverage for the local digest-bound adapter manifest generator."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "generate-runtime-adapter-manifest.py"
SERVICES = ROOT / "scripts" / "live-demo-services.ps1"
MANAGED_PROCESS = ROOT / "scripts" / "managed-process-identity.ps1"


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _create_committed_adapter_repository(tmp_path: Path) -> tuple[Path, bytes]:
    repository = tmp_path / "repository"
    module = repository / "agenten" / "agent_runtime" / "captain_production_adapters.py"
    module.parent.mkdir(parents=True)
    module_bytes = b"# fixture-secret-should-not-be-emitted\nADAPTER = 'fixture'\n"
    module.write_bytes(module_bytes)
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "Captain tests"],
        ["git", "add", "."],
        ["git", "commit", "-m", "fixture adapter"],
    ):
        result = _run(command, cwd=repository)
        assert result.returncode == 0, result.stdout + result.stderr
    return repository, module_bytes


def _run_generator(repository: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            str(ROOT / ".venv" / "Scripts" / "python.exe"),
            str(GENERATOR),
            "--repository-root",
            str(repository),
        ],
        cwd=repository,
    )


def _generated_metadata(repository: Path) -> dict[str, str]:
    result = _run_generator(repository)
    assert result.returncode == 0, result.stdout + result.stderr
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def _runtime_status_result(
    repository: Path,
    *,
    listener_owner_expression: str,
) -> subprocess.CompletedProcess[str]:
    scripts = repository / "scripts"
    scripts.mkdir(exist_ok=True)
    for source in (SERVICES, MANAGED_PROCESS, GENERATOR):
        shutil.copy2(source, scripts / source.name)
    (scripts / "minibook-demo.ps1").write_text(
        "param([string]$Action)\nif ($Action -ne 'status') { throw 'unexpected action' }\nWrite-Output 'minibook-status'\n",
        encoding="utf-8",
    )
    (scripts / "demo-preflight.ps1").write_text(
        "param([string]$EnvFile)\nWrite-Output 'preflight-status'\n",
        encoding="utf-8",
    )
    metadata = _generated_metadata(repository)
    (repository / ".env").write_text(
        "\n".join(
            (
                "CAPTAIN_RUNTIME_URL=http://127.0.0.1:18091",
                "CAPTAIN_RUNTIME_TOKEN=fixture-runtime-token",
                f"CAPTAIN_RUNTIME_ADAPTER_MANIFEST={metadata['manifest_path']}",
                f"CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256={metadata['manifest_sha256']}",
                "",
            )
        ),
        encoding="utf-8",
    )
    harness = repository / "runtime-status.ps1"
    harness.write_text(
        "\n".join(
            (
                "$ErrorActionPreference = 'Stop'",
                f". '{(scripts / 'managed-process-identity.ps1').as_posix()}'",
                "$identityPid = $PID",
                "$manifest = '" + metadata["manifest_path"].replace("'", "''") + "'",
                "$manifestSha = '" + metadata["manifest_sha256"] + "'",
                "$payload = @(",
                "  'captain.runtime.configuration.v1',",
                "  'CAPTAIN_RUNTIME_URL=http://127.0.0.1:18091',",
                "  ('CAPTAIN_RUNTIME_ADAPTER_MANIFEST=' + $manifest),",
                "  ('CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256=' + $manifestSha)",
                ") -join \"`n\"",
                "$sha = [Security.Cryptography.SHA256]::Create()",
                "try { $configurationSha = [Convert]::ToHexString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($payload))).ToLowerInvariant() } finally { $sha.Dispose() }",
                "Write-ManagedProcessIdentity -Process (Get-Process -Id $identityPid) -Path (Join-Path (Get-Location) '.captain-cook/runtime-demo.pid') -ConfigurationSha256 $configurationSha",
                "$health = { param($url, $token) $true }",
                "$listener = { param($port) [pscustomobject]@{ OwningProcess = " + listener_owner_expression + " } }.GetNewClosure()",
                f"& '{(scripts / 'live-demo-services.ps1').as_posix()}' status -RuntimeHealthProbe $health -RuntimeListenerProbe $listener",
            )
        ),
        encoding="utf-8",
    )
    return _run(["pwsh", "-NoProfile", "-File", str(harness)], cwd=repository)


def test_generator_writes_a_canonical_committed_adapter_manifest(tmp_path: Path) -> None:
    """Changing adapter bytes or JSON formatting must invalidate the generated bundle."""

    repository, module_bytes = _create_committed_adapter_repository(tmp_path)

    first = _run_generator(repository)
    assert first.returncode == 0, first.stdout + first.stderr
    first_lines = first.stdout.splitlines()
    assert len(first_lines) == 3
    assert [line.split("=", 1)[0] for line in first_lines] == [
        "manifest_path",
        "manifest_sha256",
        "module_sha256",
    ]
    output = dict(line.split("=", 1) for line in first_lines)
    manifest = Path(output["manifest_path"])
    manifest_bytes = manifest.read_bytes()
    expected_module_digest = hashlib.sha256(module_bytes).hexdigest()
    expected_document = {
        "factory_name": "create_runtime_adapters",
        "module_path": "agenten/agent_runtime/captain_production_adapters.py",
        "module_sha256": expected_module_digest,
        "schema": "captain.runtime-adapters.v1",
    }

    assert manifest == repository / ".captain-cook" / "runtime-adapters" / "captain-runtime-adapters.json"
    assert manifest_bytes == (
        json.dumps(expected_document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    assert output["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert output["module_sha256"] == expected_module_digest
    assert "fixture-secret-should-not-be-emitted" not in first.stdout

    second = _run_generator(repository)
    assert second.returncode == 0, second.stdout + second.stderr
    assert second.stdout == first.stdout
    assert manifest.read_bytes() == manifest_bytes


def test_generator_rejects_an_adapter_that_differs_from_the_committed_bytes(
    tmp_path: Path,
) -> None:
    """An uncommitted adapter edit must not be blessed by a new manifest digest."""

    repository, _ = _create_committed_adapter_repository(tmp_path)
    module = repository / "agenten" / "agent_runtime" / "captain_production_adapters.py"
    module.write_text("ADAPTER = 'uncommitted'\n", encoding="utf-8")

    result = _run_generator(repository)

    assert result.returncode != 0
    assert "does not match committed bytes" in result.stderr
    assert not (repository / ".captain-cook" / "runtime-adapters").exists()


def test_runtime_status_rebinds_the_listener_to_the_managed_runtime_process(
    tmp_path: Path,
) -> None:
    """A healthy response is insufficient if another process owns the runtime port."""

    repository, _ = _create_committed_adapter_repository(tmp_path)

    valid = _runtime_status_result(
        repository,
        listener_owner_expression="$identityPid",
    )
    mismatched = _runtime_status_result(
        repository,
        listener_owner_expression="($identityPid + 1)",
    )

    assert valid.returncode == 0, valid.stdout + valid.stderr
    assert "minibook-status" in valid.stdout
    assert mismatched.returncode != 0
    assert "minibook-status" not in mismatched.stdout
    assert "listener" in (mismatched.stdout + mismatched.stderr).lower()


@pytest.mark.parametrize(
    ("manifest_setting", "digest_setting"),
    [
        (None, "0" * 64),
        (".captain-cook/runtime-adapters/captain-runtime-adapters.json", None),
        (".captain-cook/runtime-adapters/captain-runtime-adapters.json", "0" * 64),
    ],
)
def test_service_start_refuses_invalid_adapter_settings_before_docker(
    tmp_path: Path,
    manifest_setting: str | None,
    digest_setting: str | None,
) -> None:
    """A missing or mismatched externally supplied bundle never reaches service launch."""

    repository, _ = _create_committed_adapter_repository(tmp_path)
    scripts = repository / "scripts"
    scripts.mkdir()
    shutil.copy2(SERVICES, scripts / "live-demo-services.ps1")
    shutil.copy2(MANAGED_PROCESS, scripts / "managed-process-identity.ps1")
    shutil.copy2(GENERATOR, scripts / "generate-runtime-adapter-manifest.py")
    lines: list[str] = []
    if manifest_setting is not None:
        lines.append(f"CAPTAIN_RUNTIME_ADAPTER_MANIFEST={manifest_setting}")
    if digest_setting is not None:
        lines.append(f"CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256={digest_setting}")
    (repository / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    original_env = (repository / ".env").read_bytes()
    runtime_pid = repository / ".captain-cook" / "runtime-demo.pid"
    runtime_pid.parent.mkdir()
    original_pid = b'{"fixture":"runtime-identity"}\n'
    runtime_pid.write_bytes(original_pid)
    docker = repository / "docker.cmd"
    docker.write_text("@echo docker-called\r\nexit /b 99\r\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["PATH"] = f"{repository}{os.pathsep}{environment['PATH']}"

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(scripts / "live-demo-services.ps1"), "start"],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "docker-called" not in output
    assert "Runtime adapter" in output or "runtime adapter" in output
    assert (repository / ".env").read_bytes() == original_env
    assert runtime_pid.read_bytes() == original_pid
