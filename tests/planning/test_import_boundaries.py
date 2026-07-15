import subprocess
import sys


def test_llm_planning_adapter_imports_without_package_cycle() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from agenten.llm.plan_batches import make_llm_align"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
