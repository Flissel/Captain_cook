import json
import subprocess
import sys


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
