from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveCapabilityEvidence,
    CapabilityLiveDemoEvidence,
    CapabilityLiveReservation,
    CapabilityLiveRestartEvidence,
)
from agenten.agent_runtime.contracts import ArtifactRef


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run-capability-live-demo.ps1"
INPUTS = (
    "examples/live-demo/sequential-n8n/TO_BE_BUILT.md",
    "examples/live-demo/selector-review/TO_BE_BUILT.md",
    "examples/live-demo/swarm-api-gap/TO_BE_BUILT.md",
)


def _authority_ref(namespace: str, value: str) -> ArtifactRef:
    digest = uuid5(NAMESPACE_URL, f"{namespace}:{value}").hex * 2
    return ArtifactRef(
        uri=f"artifact://{namespace}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _write_valid_real_entrypoint_evidence(path: Path) -> None:
    run_id = UUID("82000000-0000-0000-0000-000000000062")
    patterns = ("sequential", "selector_group_chat", "swarm")
    input_ids = ("sequential-n8n", "selector-review", "swarm-api-gap")
    allocations = (Decimal("0.33"), Decimal("0.33"), Decimal("0.34"))
    reservations = tuple(
        CapabilityLiveReservation(
            reservation_id=uuid5(run_id, f"reservation:{input_id}"),
            run_id=run_id,
            input_id=input_id,
            maximum_cost_usd=allocation,
        )
        for input_id, allocation in zip(input_ids, allocations, strict=True)
    )
    capabilities = tuple(
        CapabilityLiveCapabilityEvidence(
            input_id=input_id,
            conversation_pattern=pattern,
            job_id=uuid5(run_id, f"job:{input_id}"),
            correlation_id=uuid5(run_id, f"correlation:{input_id}"),
            capability_id=f"capability-{input_id}",
            capability_version=1,
            controlled_recovery_id=(
                uuid5(run_id, f"recovery:{input_id}") if index == 0 else None
            ),
            normal_trace_ids=tuple(
                uuid5(run_id, f"trace:{input_id}:{trace}")
                for trace in range(1, 4)
            ),
            release_authority_id=uuid5(run_id, f"release:{input_id}"),
            execution_authority_id=uuid5(run_id, f"execution:{input_id}"),
            execution_command_id=uuid5(run_id, f"command:{input_id}"),
            execution_result_id=uuid5(run_id, f"result:{input_id}"),
            projection_event_id=uuid5(run_id, f"projection:{input_id}"),
            projection_readback_id=uuid5(run_id, f"readback:{input_id}"),
            suite_ref=_authority_ref("benchmark-suite", input_id),
            hermes_result_ref=_authority_ref("hermes-result", input_id),
            booked_cost_usd=allocation,
            checkpoint_refs=(_authority_ref("checkpoint", input_id),),
        )
        for index, (input_id, pattern, allocation) in enumerate(
            zip(input_ids, patterns, allocations, strict=True)
        )
    )
    first = capabilities[0]
    evidence = CapabilityLiveDemoEvidence(
        run_id=run_id,
        maximum_cost_usd=Decimal("1.00"),
        booked_cost_usd=Decimal("1.00"),
        reservations=reservations,
        capabilities=capabilities,
        restart=CapabilityLiveRestartEvidence(
            run_id=run_id,
            job_id=first.job_id,
            reservation_id=reservations[0].reservation_id,
            correlation_id=first.correlation_id,
            capability_id=first.capability_id,
            capability_version=first.capability_version,
            original_execution_command_id=first.execution_command_id,
            original_execution_result_id=first.execution_result_id,
            resumed_execution_authority_id=uuid5(run_id, "restart-authority"),
            resumed_execution_command_id=uuid5(run_id, "restart-command"),
            resumed_execution_result_id=uuid5(run_id, "restart-result"),
            projection_event_id=first.projection_event_id,
            projection_readback_id=first.projection_readback_id,
        ),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        evidence.model_dump_json(by_alias=True, indent=2) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def demo_repository(tmp_path: Path) -> tuple[Path, Path, str]:
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
    return repository, copied, "agenten.agent_factory.fake_entrypoint"


def _invoke(
    demo_repository: tuple[Path, Path, str],
    *arguments: str,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    repository, script, entrypoint_module = demo_repository
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
            "-EntrypointModule",
            entrypoint_module,
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
    demo_repository: tuple[Path, Path, str],
    arguments: tuple[str, ...],
) -> None:
    result, call_log = _invoke(demo_repository, *arguments)

    assert result.returncode != 0
    assert not call_log.exists()


def test_wrapper_selects_exact_repository_inputs_runs_and_revalidates_evidence(
    demo_repository: tuple[Path, Path, str],
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
    demo_repository: tuple[Path, Path, str],
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
    demo_repository: tuple[Path, Path, str],
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
    demo_repository: tuple[Path, Path, str],
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


def test_real_wrapper_imports_the_real_entrypoint_as_a_repository_module(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    evidence = repository / ".captain-cook/live-demo/current/final-evidence.json"
    _write_valid_real_entrypoint_evidence(evidence)

    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "verify",
            "-RepositoryRoot",
            str(repository),
            "-PythonExecutable",
            sys.executable,
            "-EntrypointModule",
            "agenten.agent_factory.capability_live_entrypoint",
            "-EvidencePath",
            str(evidence),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ready_to_use" in result.stdout
    assert "ModuleNotFoundError" not in result.stdout + result.stderr


def test_real_wrapper_reaches_production_composition_and_fails_before_provider(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    for relative in INPUTS:
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {path.parent.name}\n", encoding="utf-8")
    evidence = repository / ".captain-cook/live-demo/current/final-evidence.json"

    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "start",
            "-ConfirmProviderCost",
            "-MaxCostUsd",
            "1.00",
            "-RepositoryRoot",
            str(repository),
            "-PythonExecutable",
            sys.executable,
            "-EntrypointModule",
            "agenten.agent_factory.capability_live_entrypoint",
            "-EvidencePath",
            str(evidence),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "entrypoint failed with exit code 2" in result.stdout + result.stderr
    assert "ModuleNotFoundError" not in result.stdout + result.stderr
    assert not evidence.exists()
