from __future__ import annotations

from pathlib import Path


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
    assert "-Action Run" in source
    assert "Get-Content" not in source
    assert ".env" not in source
    assert "Write-Output $plainKey" not in source
    assert "Write-Host $plainKey" not in source
