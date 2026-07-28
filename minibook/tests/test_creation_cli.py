from __future__ import annotations

import json
from pathlib import Path

import pytest

from minibook.swarm.contracts import CreationJobV1, CreationResultV1
from minibook.swarm.creation_cli import (
    load_creation_job,
    write_creation_result_atomic,
)


FIXTURE_ROOT = Path(__file__).parents[2] / "tests" / "fixtures" / "contracts"


def test_creation_cli_loads_exact_typed_job(tmp_path: Path) -> None:
    source = FIXTURE_ROOT / "minibook_creation_job.v1.json"
    job_path = tmp_path / "creation-job.json"
    job_path.write_bytes(source.read_bytes())

    loaded = load_creation_job(job_path)

    assert loaded == CreationJobV1.model_validate_json(source.read_text(encoding="utf-8"))


def test_creation_cli_writes_result_atomically_without_overwriting(tmp_path: Path) -> None:
    result = CreationResultV1.model_validate_json(
        (FIXTURE_ROOT / "minibook_creation_result.v1.json").read_text(
            encoding="utf-8"
        )
    )
    result_path = tmp_path / "creation-result.json"

    write_creation_result_atomic(result_path, result)

    assert CreationResultV1.model_validate_json(
        result_path.read_text(encoding="utf-8")
    ) == result
    assert not (tmp_path / "creation-result.json.tmp").exists()
    with pytest.raises(FileExistsError):
        write_creation_result_atomic(result_path, result)


def test_creation_cli_rejects_invalid_or_non_file_job(tmp_path: Path) -> None:
    invalid = tmp_path / "creation-job.json"
    invalid.write_text(json.dumps({"schema": "minibook.creation-job.v1"}), encoding="utf-8")

    with pytest.raises(ValueError):
        load_creation_job(invalid)
    with pytest.raises(FileNotFoundError):
        load_creation_job(tmp_path / "missing.json")
