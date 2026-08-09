from __future__ import annotations

import json
import io
from pathlib import Path

import pytest

from agenten.agent_factory.private_benchmark_catalog_cli import main


ROOT = Path(__file__).resolve().parents[2]
INPUTS = (
    "demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md",
    "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md",
    "demo_inputs/agent_factory/claims_resolution/TO_BE_BUILT.md",
)


def _arguments(private_root: Path) -> list[str]:
    values = [
        "--repository-root", str(ROOT),
        "--private-root", str(private_root),
    ]
    for value in INPUTS:
        values.extend(("--input", value))
    return values


def test_init_accepts_ephemeral_stdin_seed_without_printing_or_persisting_it(
    tmp_path: Path, capsys,
) -> None:
    private_root = tmp_path / "private"
    seed = "CAPTAIN-SECRET-SEED-MUST-NOT-PRINT" * 2

    assert main([*_arguments(private_root), "--seed-stdin"], stdin=io.StringIO(seed + "\n")) == 0
    first = json.loads(capsys.readouterr().out)
    assert seed not in json.dumps(first)
    assert first["manifest_path"] == "catalog.json"
    assert len(first["manifest_sha256"]) == 64
    assert seed.encode() not in (private_root / "catalog.json").read_bytes()

    assert main(_arguments(private_root)) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay == first


def test_init_has_no_seed_argv_file_or_environment_channel(tmp_path: Path) -> None:
    for forbidden in ("--seed", "--seed-file", "--seed-env"):
        with pytest.raises(SystemExit):
            main([*_arguments(tmp_path / "private"), forbidden, "secret"])


def test_init_uses_os_random_for_first_creation_and_supports_explicit_rotation_path(
    tmp_path: Path, capsys,
) -> None:
    private_root = tmp_path / "private"

    assert main(_arguments(private_root)) == 0
    first = json.loads(capsys.readouterr().out)
    rotated_args = [*_arguments(private_root), "--manifest-path", "catalog-v2.json"]
    assert main(rotated_args) == 0
    rotated = json.loads(capsys.readouterr().out)

    assert rotated["manifest_path"] == "catalog-v2.json"
    assert rotated["manifest_sha256"] != first["manifest_sha256"]
    assert (private_root / "catalog.json").is_file()
    assert (private_root / "catalog-v2.json").is_file()


def test_init_rejects_input_symlink_that_escapes_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    linked_parent = repository / "linked"
    linked_parent.mkdir(parents=True)
    link = linked_parent / "TO_BE_BUILT.md"
    try:
        link.symlink_to(ROOT / INPUTS[0])
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ValueError, match="escape"):
        main([
            "--repository-root", str(repository),
            "--private-root", str(tmp_path / "private"),
            "--input", "linked/TO_BE_BUILT.md",
        ])
