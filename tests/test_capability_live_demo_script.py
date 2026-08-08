from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run-capability-live-demo.ps1"
INPUTS = (
    "examples/live-demo/sequential-n8n/TO_BE_BUILT.md",
    "examples/live-demo/selector-review/TO_BE_BUILT.md",
    "examples/live-demo/swarm-api-gap/TO_BE_BUILT.md",
)


@pytest.fixture
def demo_repository(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    copied = scripts / SCRIPT.name
    shutil.copy2(SCRIPT, copied)
    for relative in INPUTS:
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {path.parent.name}\n", encoding="utf-8")
    entrypoint = repository / "agenten" / "agent_factory" / "fake_entrypoint.py"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text(
        """
import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--repository-root')
parser.add_argument('--input', action='append', default=[])
parser.add_argument('--pattern', action='append', default=[])
parser.add_argument('--max-cost-usd')
parser.add_argument('--evidence-path', required=True)
parser.add_argument('--verify-only', action='store_true')
args = parser.parse_args()
with Path(os.environ['CAPTAIN_TEST_CALL_LOG']).open('a', encoding='utf-8') as stream:
    stream.write(json.dumps(vars(args), sort_keys=True) + '\\n')
evidence = Path(args.evidence_path)
if args.verify_only:
    value = json.loads(evidence.read_text(encoding='utf-8'))
    if value.get('schema') != 'captain.capability-live-demo-evidence.v1':
        raise SystemExit(4)
    if value.get('status') != 'ready_to_use':
        raise SystemExit(5)
else:
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps({
        'schema': 'captain.capability-live-demo-evidence.v1',
        'status': 'ready_to_use',
        'input_count': 3,
        'maximum_cost_usd': args.max_cost_usd,
    }), encoding='utf-8')
""".strip(),
        encoding="utf-8",
    )
    return repository, copied, entrypoint


def _invoke(
    demo_repository: tuple[Path, Path, Path],
    *arguments: str,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    repository, script, entrypoint = demo_repository
    call_log = repository / ".captain-cook" / "test-calls.jsonl"
    environment = os.environ.copy()
    environment["CAPTAIN_TEST_CALL_LOG"] = str(call_log)
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(script),
            *arguments,
            "-RepositoryRoot",
            str(repository),
            "-PythonExecutable",
            sys.executable,
            "-EntrypointPath",
            str(entrypoint),
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result, call_log


@pytest.mark.parametrize(
    "arguments",
    [
        ("start", "-MaxCostUsd", "6.00"),
        ("start", "-ConfirmProviderCost"),
        ("start", "-ConfirmProviderCost", "-MaxCostUsd", "0"),
        ("start", "-ConfirmProviderCost", "-MaxCostUsd", "NaN"),
        ("start", "-ConfirmProviderCost", "-MaxCostUsd", "6,00"),
        ("start", "-ConfirmProviderCost", "-MaxCostUsd", "0.02"),
    ],
)
def test_paid_authorization_and_three_reservations_fail_before_entrypoint(
    demo_repository: tuple[Path, Path, Path],
    arguments: tuple[str, ...],
) -> None:
    result, call_log = _invoke(demo_repository, *arguments)

    assert result.returncode != 0
    assert not call_log.exists()


def test_wrapper_selects_exact_repository_inputs_runs_and_revalidates_evidence(
    demo_repository: tuple[Path, Path, Path],
) -> None:
    repository, _, _ = demo_repository

    result, call_log = _invoke(
        demo_repository,
        "start",
        "-ConfirmProviderCost",
        "-MaxCostUsd",
        "1.00",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2
    run, verify = calls
    assert run["verify_only"] is False
    assert verify["verify_only"] is True
    assert run["max_cost_usd"] == "1.00"
    assert run["pattern"] == ["sequential", "selector_group_chat", "swarm"]
    assert tuple(
        Path(path).relative_to(repository).as_posix() for path in run["input"]
    ) == INPUTS
    assert run["evidence_path"] == verify["evidence_path"]
    assert "ready_to_use" in result.stdout


def test_missing_canonical_input_stops_before_entrypoint(
    demo_repository: tuple[Path, Path, Path],
) -> None:
    repository, _, _ = demo_repository
    (repository / INPUTS[1]).unlink()

    result, call_log = _invoke(
        demo_repository,
        "start",
        "-ConfirmProviderCost",
        "-MaxCostUsd",
        "1.00",
    )

    assert result.returncode != 0
    assert not call_log.exists()


def test_recording_requires_ffmpeg_before_entrypoint(
    demo_repository: tuple[Path, Path, Path],
) -> None:
    result, call_log = _invoke(
        demo_repository,
        "start",
        "-ConfirmProviderCost",
        "-MaxCostUsd",
        "1.00",
        "-RecordingWindowTitle",
        "Captain Demo Exact Window",
        "-FfmpegPath",
        "captain-ffmpeg-does-not-exist.exe",
    )

    assert result.returncode != 0
    assert "FFmpeg" in result.stdout + result.stderr
    assert not call_log.exists()


def test_recording_rejects_zero_exact_named_windows_before_entrypoint(
    demo_repository: tuple[Path, Path, Path],
) -> None:
    ffmpeg_probe = shutil.which("where.exe") or shutil.which("where")
    assert ffmpeg_probe is not None

    result, call_log = _invoke(
        demo_repository,
        "start",
        "-ConfirmProviderCost",
        "-MaxCostUsd",
        "1.00",
        "-RecordingWindowTitle",
        "Captain Window That Cannot Exist 82000000",
        "-FfmpegPath",
        ffmpeg_probe,
    )

    assert result.returncode != 0
    assert "exactly one named window" in (result.stdout + result.stderr)
    assert not call_log.exists()
