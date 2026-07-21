from __future__ import annotations

import json
import hashlib
import zipfile
from pathlib import Path

import pytest

from minibook.swarm.package_assembler import (
    LegacyPackageContractGap,
    PackageAssemblyError,
    PackageAssembler,
)


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


def integrated_candidate(tmp_path: Path) -> Path:
    root = candidate(tmp_path)
    (root / "agents/worker").mkdir(parents=True)
    (root / "agents/worker/agent.yml").write_text(
        "\n".join(
            (
                "name: worker",
                "role: integration worker",
                "model: gpt-5.2",
                "system_message: Process the request with the approved workflow.",
                "tools:",
                "  - customer_sync",
                "handoffs: []",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "project.yml").write_text(
        "name: Demo Team\npattern: single_agent\n",
        encoding="utf-8",
    )
    (root / "n8n").mkdir()
    (root / "adapters/schemas").mkdir(parents=True)
    (root / "n8n/customer_sync.json").write_text(
        '{"name":"customer-sync","nodes":[]}', encoding="utf-8"
    )
    (root / "adapters/schemas/customer_sync.input.json").write_text(
        '{"type":"object","title":"input"}', encoding="utf-8"
    )
    (root / "adapters/schemas/customer_sync.output.json").write_text(
        '{"type":"object","title":"output"}', encoding="utf-8"
    )
    return root


def integration_contract() -> dict[str, object]:
    return {
        "workflow": "n8n/customer_sync.json",
        "input_schema": "adapters/schemas/customer_sync.input.json",
        "output_schema": "adapters/schemas/customer_sync.output.json",
        "idempotency": "correlation_id",
        "timeout": 30,
        "retry": "bounded",
        "duplicate": "reject",
        "failure": "fail_closed",
        "compensation": "none",
    }


def test_same_inputs_produce_same_archive_digest_and_complete_manifest(tmp_path: Path) -> None:
    root = candidate(tmp_path)
    assembler = PackageAssembler()
    first = assembler.assemble(root, tmp_path / "one.zip", startup_command=("python", "autogen/main.py"))
    second = assembler.assemble(root, tmp_path / "two.zip", startup_command=("python", "autogen/main.py"))
    assert first.archive_sha256 == second.archive_sha256
    with zipfile.ZipFile(tmp_path / "one.zip") as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("evidence/package-index.json"))
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


def test_integration_export_seals_factory_candidate_descriptor_from_real_files(
    tmp_path: Path,
) -> None:
    source = integrated_candidate(tmp_path)
    assembler = PackageAssembler()

    first = assembler.assemble(
        source,
        tmp_path / "one.zip",
        startup_command=("python", "autogen/main.py"),
        integration_contracts=(integration_contract(),),
    )
    second = assembler.assemble(
        source,
        tmp_path / "two.zip",
        startup_command=("python", "autogen/main.py"),
        integration_contracts=(integration_contract(),),
    )

    assert first.archive_sha256 == second.archive_sha256
    assert first.candidate_descriptor_sha256 == second.candidate_descriptor_sha256
    with zipfile.ZipFile(first.archive_path) as archive:
        descriptor_bytes = archive.read("adapters/factory-candidate.json")
        descriptor = json.loads(descriptor_bytes)
        execution = json.loads(archive.read("adapters/execution-team.json"))
        prompt = archive.read("adapters/prompts/worker.md")
        package_manifest = json.loads(archive.read("evidence/package-index.json"))
    assert descriptor["schema"] == "captain.factory-candidate-descriptor.v1"
    assert descriptor["n8n_tools"][0]["name"] == "customer_sync"
    assert execution["conversation_pattern"] == "single_agent"
    assert execution["agents"][0]["system_prompt_ref"]["sha256"] == hashlib.sha256(
        prompt
    ).hexdigest()
    assert first.candidate_descriptor_sha256 == hashlib.sha256(
        descriptor_bytes
    ).hexdigest()
    paths = {item["path"] for item in package_manifest["files"]}
    assert {
        "adapters/factory-candidate.json",
        "adapters/execution-team.json",
        "adapters/prompts/worker.md",
        "n8n/customer_sync.json",
    }.issubset(paths)


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


def _legacy_export(tmp_path: Path) -> Path:
    root = tmp_path / "legacy-export"
    (root / "src").mkdir(parents=True)
    (root / "src/main.py").write_text("print('legacy-ready')\n", encoding="utf-8")
    (root / "skills/factory").mkdir(parents=True)
    (root / "skills/factory/SKILL.md").write_text(
        "# Released factory skill\n", encoding="utf-8"
    )
    (root / "tests").mkdir()
    (root / "tests/test_team.py").write_text(
        "def test_team():\n    assert 2 + 2 == 4\n", encoding="utf-8"
    )
    (root / "evidence").mkdir()
    (root / "evidence/tool-gaps.json").write_text(
        '{"schema":"minibook.creation-tool-gaps.v1","tool_gaps":[]}',
        encoding="utf-8",
    )
    (root / "SETUP.md").write_text(
        "Run `python autogen/main.py`.\n", encoding="utf-8"
    )
    return root


def test_legacy_export_materializes_deterministic_package_c_from_real_results(
    tmp_path: Path,
) -> None:
    source = _legacy_export(tmp_path)
    receipt = b'{"schema":"hermes.skill-usage-receipt.v1","outcome":"passed"}'
    pipeline_results = {
        "build": {"status": "PASS", "duration": 1.25},
        "run": {"status": "PASS", "duration": 2.5},
        "output_evaluation": {"status": "PASS", "score": 0.92},
    }
    assembler = PackageAssembler()

    first = assembler.materialize_legacy_export(
        source,
        tmp_path / "package-c-one",
        capability_id="legacy-team",
        capability_version=2,
        pipeline_results=pipeline_results,
        hermes_skill_usage_receipt=receipt,
    )
    second = assembler.materialize_legacy_export(
        source,
        tmp_path / "package-c-two",
        capability_id="legacy-team",
        capability_version=2,
        pipeline_results=pipeline_results,
        hermes_skill_usage_receipt=receipt,
    )

    required = {
        "team-manifest.json",
        "RUNBOOK.md",
        "evidence/tool-gaps.json",
        "evidence/hermes-skill-usage-receipt.json",
        "autogen/main.py",
        "skills/factory/SKILL.md",
        "tests/test_team.py",
    }
    assert required.issubset(
        path.relative_to(first).as_posix()
        for path in first.rglob("*")
        if path.is_file()
    )
    assert (first / "autogen/main.py").read_bytes() == (source / "src/main.py").read_bytes()
    assert (first / "RUNBOOK.md").read_bytes() == (source / "SETUP.md").read_bytes()
    assert (first / "evidence/hermes-skill-usage-receipt.json").read_bytes() == receipt
    assert json.loads((first / "evidence/legacy-pipeline-results.json").read_bytes()) == {
        "build": {"duration": 1.25, "status": "PASS"},
        "output_evaluation": {"score": 0.92, "status": "PASS"},
        "run": {"duration": 2.5, "status": "PASS"},
        "schema": "minibook.legacy-swarm-pipeline-results.v1",
    }
    first_bytes = {
        path.relative_to(first).as_posix(): path.read_bytes()
        for path in first.rglob("*")
        if path.is_file()
    }
    second_bytes = {
        path.relative_to(second).as_posix(): path.read_bytes()
        for path in second.rglob("*")
        if path.is_file()
    }
    assert first_bytes == second_bytes


def test_legacy_export_reports_exact_missing_real_outputs_as_todo_tool(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-export"
    (source / "src").mkdir(parents=True)
    (source / "src/main.py").write_text("print('legacy')\n", encoding="utf-8")

    with pytest.raises(LegacyPackageContractGap) as raised:
        PackageAssembler().materialize_legacy_export(
            source,
            tmp_path / "package-c",
            capability_id="legacy-team",
            capability_version=1,
            pipeline_results={},
            hermes_skill_usage_receipt=None,
        )

    assert raised.value.gap_id == "legacy-swarm-package-c-export"
    assert raised.value.required_outputs == (
        "RUNBOOK.md (from real RUNBOOK.md, SETUP.md, or README.md)",
        "evidence/hermes-skill-usage-receipt.json (from Hermes)",
        "evidence/tool-gaps.json (from Hermes ToolIntegrator)",
        "pipeline build_result",
        "pipeline output_eval",
        "pipeline run_result",
        "skills/ (released or Hermes-created skill bytes)",
        "tests/ (real executable tests)",
    )
