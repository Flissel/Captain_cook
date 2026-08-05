from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path("scripts/run-business-benchmark-demo-secure.ps1")


def test_secure_runner_prompts_masked_and_keeps_provider_key_process_only() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "Read-Host 'OPENAI_API_KEY' -AsSecureString" in source
    assert "SecureStringToBSTR" in source
    assert "PtrToStringBSTR" in source
    assert "ZeroFreeBSTR" in source
    assert "SetEnvironmentVariable('OPENAI_API_KEY', $plainKey, 'Process')" in source
    assert "SetEnvironmentVariable('OPENAI_API_KEY', $nullString, 'Process')" in source
    assert "run-business-benchmark-demo.ps1" in source
    assert "[ValidateSet('Build', 'Run')]" in source
    assert "[string]$Action = 'Run'" in source
    assert "-Action $Action" in source
    assert "Get-Content" not in source
    assert ".env" not in source
    assert "Write-Output $plainKey" not in source
    assert "Write-Host $plainKey" not in source


def test_secure_runner_preserves_python_path_as_first_positional_argument(
    tmp_path: Path,
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable")
    wrapper = tmp_path / SCRIPT.name
    wrapper.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "run-business-benchmark-demo.ps1").write_text(
        """
param([string]$Action, [string]$PythonPath)
$resultPath = Join-Path $PSScriptRoot 'forwarded-arguments.json'
[ordered]@{ action = $Action; python_path = $PythonPath } |
    ConvertTo-Json -Compress |
    Set-Content -LiteralPath $resultPath -NoNewline
""".strip(),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
                pwsh,
                "-NoProfile",
                "-Command",
                "& { "
                "$global:secureKey = ConvertTo-SecureString 'test-key' -AsPlainText -Force; "
                "function global:Read-Host { "
                "param([string]$Prompt, [switch]$AsSecureString); return $global:secureKey }; "
            f"& '{wrapper}' 'C:\\compat-python.exe' -Action Build "
            "}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads((tmp_path / "forwarded-arguments.json").read_text("utf-8")) == {
        "action": "Build",
        "python_path": r"C:\compat-python.exe",
    }
