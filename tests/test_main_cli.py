import json
import subprocess
import sys

import main as captain_main


def test_demo_command_writes_successful_evidence(tmp_path):
    output = tmp_path / "demo.json"

    result = subprocess.run(
        [sys.executable, "main.py", "demo", "--output", str(output)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Demo complete: 4 subproblems reached done" in result.stdout
    assert json.loads(output.read_text(encoding="utf-8"))["success"] is True


def test_recover_gateway_command_routes_to_captain_recovery(monkeypatch):
    from agenten.delivery import recovery_cli

    observed: list[list[str]] = []

    def recover(argv: list[str]) -> int:
        observed.append(argv)
        return 7

    monkeypatch.setattr(recovery_cli, "main", recover)

    assert captain_main.main(["recover-gateway", "--gateway-url", "https://gateway.test"]) == 7
    assert observed == [["--gateway-url", "https://gateway.test"]]
