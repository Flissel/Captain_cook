from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
from uuid import UUID

import pytest

from gateway.agent_factory_live_operator import (
    FactoryLiveOperatorSettings,
    _LazyProductionBenchmarkInputs,
)


LOCAL_DSN = "mariadb://captain_test:redacted@127.0.0.1:3306/captain_test"
JOB_IDS = (
    UUID("71000000-0000-0000-0000-000000000001"),
    UUID("71000000-0000-0000-0000-000000000002"),
)


def test_operator_settings_enforce_isolated_database_two_jobs_and_cost_allocation(
    tmp_path: Path,
) -> None:
    settings = FactoryLiveOperatorSettings(
        workspace_root=tmp_path,
        python_executable=Path(sys.executable),
        test_mariadb_dsn=LOCAL_DSN,
        job_ids=JOB_IDS,
        hermes_provider="openai-api",
        hermes_model="gpt-4.1-mini",
        hermes_maximum_total_cost_usd=Decimal("0.10"),
    )

    assert settings.job_ids == JOB_IDS
    with pytest.raises(ValueError, match="cost allocation"):
        FactoryLiveOperatorSettings(
            workspace_root=tmp_path,
            python_executable=Path(sys.executable),
            test_mariadb_dsn=LOCAL_DSN,
            job_ids=JOB_IDS,
            hermes_provider="openai-api",
            hermes_model="gpt-4.1-mini",
            hermes_maximum_total_cost_usd=Decimal("0.11"),
        )
    with pytest.raises(ValueError, match="distinct jobs"):
        FactoryLiveOperatorSettings(
            workspace_root=tmp_path,
            python_executable=Path(sys.executable),
            test_mariadb_dsn=LOCAL_DSN,
            job_ids=(JOB_IDS[0], JOB_IDS[0]),
            hermes_provider="openai-api",
            hermes_model="gpt-4.1-mini",
            hermes_maximum_total_cost_usd=Decimal("0.10"),
        )


def test_lazy_benchmark_inputs_build_one_composition_per_job() -> None:
    calls: list[object] = []
    expected = object()

    class Composition:
        def dispatch_inputs(self, settings: object, request: object) -> object:
            calls.append((settings, request))
            return expected

    class Loader:
        def __call__(self, settings: object) -> Composition:
            calls.append(settings)
            return Composition()

    selected = SimpleNamespace(profile="claims")
    inputs = _LazyProductionBenchmarkInputs(
        settings={JOB_IDS[0]: selected},  # type: ignore[arg-type]
        loader=Loader(),  # type: ignore[arg-type]
    )
    request = SimpleNamespace(job=SimpleNamespace(job_id=JOB_IDS[0]))

    assert inputs.resolve(request) is expected  # type: ignore[arg-type]
    assert inputs.resolve(request) is expected  # type: ignore[arg-type]
    assert calls.count(selected) == 1
    assert inputs.resolve(
        SimpleNamespace(job=SimpleNamespace(job_id=JOB_IDS[1]))  # type: ignore[arg-type]
    ) is None


def test_operator_cli_is_importable_from_scripts_directory() -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "run-agent-factory-business-demo.py"
    )
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=script.parent,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--hermes-max-usd" in completed.stdout
