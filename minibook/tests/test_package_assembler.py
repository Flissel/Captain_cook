from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from minibook.swarm.package_assembler import PackageAssemblyError, PackageAssembler


def candidate(tmp_path: Path) -> Path:
    root = tmp_path / "candidate"
    for directory in ("autogen", "skills", "tests", "evidence"):
        (root / directory).mkdir(parents=True)
    (root / "autogen/main.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "skills/SKILL.md").write_text("# Skill\n", encoding="utf-8")
    (root / "tests/test_team.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    (root / "evidence/build.json").write_text('{"passed":true}\n', encoding="utf-8")
    (root / "RUNBOOK.md").write_text("Run `python autogen/main.py`.\n", encoding="utf-8")
    return root


def test_same_inputs_produce_same_archive_digest_and_complete_manifest(tmp_path: Path) -> None:
    root = candidate(tmp_path)
    assembler = PackageAssembler()
    first = assembler.assemble(root, tmp_path / "one.zip", startup_command=("python", "autogen/main.py"))
    second = assembler.assemble(root, tmp_path / "two.zip", startup_command=("python", "autogen/main.py"))
    assert first.archive_sha256 == second.archive_sha256
    with zipfile.ZipFile(tmp_path / "one.zip") as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("team-manifest.json"))
    assert names == sorted(names)
    assert {"autogen/", "skills/", "tests/", "evidence/", "RUNBOOK.md"}.issubset(
        set(manifest["required_layout"])
    )
    assert "n8n/" not in manifest["required_layout"]
    assert all("sha256" in entry for entry in manifest["files"])


def test_integration_requires_typed_n8n_behavior_contract(tmp_path: Path) -> None:
    root = candidate(tmp_path)
    (root / "n8n").mkdir()
    (root / "n8n/workflow.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PackageAssemblyError, match="integration contract"):
        PackageAssembler().assemble(
            root, tmp_path / "bad.zip", startup_command=("python", "autogen/main.py"),
            integration_contracts=({"workflow": "n8n/workflow.json"},),
        )


@pytest.mark.parametrize("command", [("powershell", "x.ps1"), ("sh", "x.sh"), ("python", "../escape.py")])
def test_unknown_or_traversing_startup_commands_are_rejected(tmp_path: Path, command: tuple[str, str]) -> None:
    with pytest.raises(PackageAssemblyError):
        PackageAssembler().assemble(candidate(tmp_path), tmp_path / "bad.zip", startup_command=command)


def test_symlink_and_secret_files_are_rejected_or_excluded(tmp_path: Path) -> None:
    root = candidate(tmp_path)
    (root / ".env").write_text("SECRET=value", encoding="utf-8")
    result = PackageAssembler().assemble(root, tmp_path / "safe.zip", startup_command=("python", "autogen/main.py"))
    with zipfile.ZipFile(result.archive_path) as archive:
        assert ".env" not in archive.namelist()
