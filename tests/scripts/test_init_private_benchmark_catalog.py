from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenten.agent_factory.private_benchmark_catalog_cli import main


ROOT = Path(__file__).resolve().parents[2]
INPUTS = (
    "demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md",
    "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md",
    "demo_inputs/agent_factory/claims_resolution/TO_BE_BUILT.md",
)


def _arguments(private_root: Path, seed_file: Path | None = None) -> list[str]:
    values = [
        "--repository-root", str(ROOT),
        "--private-root", str(private_root),
    ]
    for value in INPUTS:
        values.extend(("--input", value))
    if seed_file is not None:
        values.extend(("--seed-file", str(seed_file)))
    return values


def test_init_uses_secret_seed_file_without_printing_it_and_replays_exactly(
    tmp_path: Path, capsys,
) -> None:
    private_root = tmp_path / "private"
    seed_file = tmp_path / "seed.bin"
    seed = b"CAPTAIN-SECRET-SEED-MUST-NOT-PRINT" * 2
    seed_file.write_bytes(seed)

    assert main(_arguments(private_root, seed_file)) == 0
    first = json.loads(capsys.readouterr().out)
    assert seed.decode() not in json.dumps(first)
    assert first["manifest_path"] == "catalog.json"
    assert len(first["manifest_sha256"]) == 64

    seed_file.unlink()
    assert main(_arguments(private_root)) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay == first


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
