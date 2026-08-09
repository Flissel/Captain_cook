from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from minibook.swarm.artifact_store import FilesystemCreationArtifactStore
from minibook.swarm.contracts import AgentHandoffV1, ToolGapMarkerV1
from minibook.swarm.team_package import (
    TeamPackageError,
    seal_team_package,
    validate_team_package,
)
from minibook.swarm.v3_pipeline import TeamPackageV3Validator


AGENTS = (
    "account_researcher",
    "opportunity_qualifier",
    "deal_strategist",
    "executive_brief_writer",
)
HANDOFFS = tuple(zip(AGENTS, AGENTS[1:]))


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True).encode()


def _archive(overrides: dict[str, bytes] | None = None) -> bytes:
    files = {
        "src/team.py": b"TEAM = 'sales_pipeline_brief'\n",
        "src/tools/account_snapshot_reader.py": b"def read(value: dict) -> dict:\n    return value\n",
        "src/tools/opportunity_scoring_rules.py": b"def score(value: int) -> int:\n    return value\n",
        "tests/test_team.py": b"from src.team import TEAM\n\ndef test_team():\n    assert TEAM == 'sales_pipeline_brief'\n",
        "evidence/build.json": _json({"schema": "minibook.build-evidence.v1", "status": "passed"}),
        "evidence/real-case.json": _json({"schema": "minibook.real-case-evidence.v1", "status": "passed", "assertion_ids": ["deterministic_rank"]}),
        "evidence/hermes-factory-skill-usage-receipt.json": _json({"schema": "hermes.factory-skill-usage-receipt.v1", "status": "passed"}),
        "evidence/codex-build-receipt.json": _json({"schema": "captain.codex-build-receipt.v1", "status": "passed"}),
        "factory-candidate.json": _json({"schema": "captain.factory-candidate.v1", "team_manifest_sha256": "BOUND_AT_SEAL"}),
    }
    files.update(overrides or {})
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as package:
        for name, content in files.items():
            package.writestr(name, content)
    return stream.getvalue()


def _fixture(tmp_path: Path):
    store = FilesystemCreationArtifactStore(tmp_path / "cas")
    compiled = {
        "schema": "captain.compiled-factory-spec.v3",
        "required_agent_ids": list(AGENTS),
        "ordered_handoffs": [{"from_agent_id": left, "to_agent_id": right} for left, right in HANDOFFS],
        "tool_requirements": [
            {"tool_id": "account_snapshot_reader", "required": True},
            {"tool_id": "opportunity_scoring_rules", "required": True},
            {"tool_id": "industry_signal_reader", "required": False},
        ],
        "allowed_optional_tool_gaps": ["industry_signal_reader"],
        "public_assertion_ids": ["deterministic_rank"],
    }
    manifest = {
        "schema": "minibook.team-manifest.v2",
        "agents": [{"agent_id": value} for value in AGENTS],
        "handoffs": [{"from_agent_id": left, "to_agent_id": right} for left, right in HANDOFFS],
        "tools": [
            {"tool_id": "account_snapshot_reader", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "implementation_path": "src/tools/account_snapshot_reader.py", "test_path": "tests/test_team.py"},
            {"tool_id": "opportunity_scoring_rules", "input_schema": {"type": "integer"}, "output_schema": {"type": "integer"}, "implementation_path": "src/tools/opportunity_scoring_rules.py", "test_path": "tests/test_team.py"},
        ],
        "source_files": ["src/team.py", "src/tools/account_snapshot_reader.py", "src/tools/opportunity_scoring_rules.py"],
        "test_files": ["tests/test_team.py"],
    }
    refs = {
        "compiled_spec_ref": store.put(_json(compiled), "application/json", namespace="compiled-spec"),
        "team_manifest_ref": store.put(_json(manifest), "application/json", namespace="team-manifest"),
        "source_archive_ref": store.put(_archive(), "application/zip", namespace="source"),
    }
    gaps = (ToolGapMarkerV1(schema="TODO_TOOL.v1", gap_id="industry_signal_reader", severity="optional", evidence_ref=store.put(_json({"status": "unresolved"}), "application/json", namespace="tool-gap"), status="unresolved"),)
    return store, compiled, manifest, refs, gaps


def test_validate_and_seal_exact_team_package(tmp_path: Path) -> None:
    store, _, _, refs, gaps = _fixture(tmp_path)
    validated = validate_team_package(artifact_store=store, tool_gaps=gaps, **refs)
    sealed = seal_team_package(validated, artifact_store=store)

    assert sealed.readback_receipt["schema"] == "minibook.cas-readback-receipt.v1"
    assert all(item["verified"] for item in sealed.readback_receipt["artifacts"])
    candidate = json.loads(store.read_bytes(sealed.candidate_manifest_ref))
    assert {item["path"] for item in candidate["artifacts"]} >= {
        "src/team.py",
        "src/tools/account_snapshot_reader.py",
        "tests/test_team.py",
        "evidence/real-case.json",
    }
    with zipfile.ZipFile(io.BytesIO(store.read_bytes(sealed.package_ref))) as archive:
        assert archive.namelist() == sorted(archive.namelist())
        assert {"team_manifest.json", "factory-candidate.json", "evidence/hermes-factory-skill-usage-receipt.json", "evidence/codex-build-receipt.json"}.issubset(archive.namelist())


@pytest.mark.parametrize("mutation", ["missing_agent", "extra_agent", "handoff", "missing_tool", "untyped_tool", "empty_source", "syntax", "failed_test", "weights", "gap"])
def test_validate_team_package_rejects_compiled_contract_or_executable_drift(tmp_path: Path, mutation: str) -> None:
    store, _, manifest, refs, gaps = _fixture(tmp_path)
    archive_override: dict[str, bytes] = {}
    changed_manifest = json.loads(json.dumps(manifest))
    changed_gaps = gaps
    if mutation == "missing_agent": changed_manifest["agents"].pop()
    elif mutation == "extra_agent": changed_manifest["agents"].append({"agent_id": "invented_agent"})
    elif mutation == "handoff": changed_manifest["handoffs"].reverse()
    elif mutation == "missing_tool": changed_manifest["tools"].pop()
    elif mutation == "untyped_tool": changed_manifest["tools"][0]["input_schema"] = {}
    elif mutation == "empty_source": archive_override["src/team.py"] = b""
    elif mutation == "syntax": archive_override["src/team.py"] = b"def broken(:\n"
    elif mutation == "failed_test": archive_override["tests/test_team.py"] = b"def test_team():\n    assert False\n"
    elif mutation == "weights": archive_override["src/tools/opportunity_scoring_rules.py"] = b"SCORING_WEIGHTS = {'invented': 1}\n"
    elif mutation == "gap": changed_gaps = ()
    refs["team_manifest_ref"] = store.put(_json(changed_manifest), "application/json", namespace="team-manifest")
    refs["source_archive_ref"] = store.put(_archive(archive_override), "application/zip", namespace="source")
    with pytest.raises(TeamPackageError):
        validate_team_package(artifact_store=store, tool_gaps=changed_gaps, **refs)


def test_validate_team_package_rejects_archive_manifest_drift(tmp_path: Path) -> None:
    store, _, _, refs, gaps = _fixture(tmp_path)
    refs["source_archive_ref"] = store.put(_archive({"team_manifest.json": _json({"schema": "minibook.team-manifest.v2", "agents": []})}), "application/zip", namespace="source")
    with pytest.raises(TeamPackageError, match="manifest drift"):
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs)


def test_v3_validator_fails_closed_without_sealed_package_snapshot() -> None:
    fixture = Path(__file__).parents[2] / "tests/fixtures/contracts/minibook_creation_job.v3.json"
    from minibook.swarm.contracts import CreationJobV3

    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["deadline_at"] = "2099-08-09T12:00:00Z"
    result = TeamPackageV3Validator().assemble_result(CreationJobV3.model_validate(payload), {})
    assert result.status == "blocked"
    assert result.failure is not None
    assert result.failure.code == "validation_failed"


@pytest.mark.asyncio
async def test_v3_validator_promotes_only_after_public_cas_readback(tmp_path: Path) -> None:
    store, _, _, refs, gaps = _fixture(tmp_path)
    sealed = seal_team_package(
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs),
        artifact_store=store,
    )
    fixture = Path(__file__).parents[2] / "tests/fixtures/contracts/minibook_creation_job.v3.json"
    from minibook.swarm.contracts import CreationJobV3

    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["deadline_at"] = "2099-08-09T12:00:00Z"
    job = CreationJobV3.model_validate(payload)
    package = {
        "team_manifest_ref": sealed.team_manifest_ref.model_dump(mode="json"),
        "candidate_manifest_ref": sealed.candidate_manifest_ref.model_dump(mode="json"),
        "source_archive_ref": sealed.source_archive_ref.model_dump(mode="json"),
        "package_manifest_ref": sealed.package_manifest_ref.model_dump(mode="json"),
        "hermes_usage_receipt_ref": sealed.hermes_usage_receipt_ref.model_dump(mode="json"),
        "codex_build_receipt_ref": sealed.codex_build_receipt_ref.model_dump(mode="json"),
        "build_evidence_refs": [item.model_dump(mode="json") for item in sealed.build_evidence_refs],
        "real_case_evidence_refs": [item.model_dump(mode="json") for item in sealed.real_case_evidence_refs],
        "cas_readback_receipt_ref": sealed.readback_receipt_ref.model_dump(mode="json"),
    }
    validator = TeamPackageV3Validator()
    outcome = await validator.validate(job, {"sealed_team_package": package}, store)
    assert outcome.snapshot["team_package_validated"] is True
    assert validator.assemble_result(job, outcome.snapshot).status == "succeeded"


def test_validate_team_package_binds_referenced_receipt_bytes(tmp_path: Path) -> None:
    store, _, _, refs, gaps = _fixture(tmp_path)
    foreign = store.put(_json({"schema": "hermes.factory-skill-usage-receipt.v1", "status": "passed", "foreign": True}), "application/json", namespace="receipt")
    with pytest.raises(TeamPackageError, match="referenced evidence"):
        validate_team_package(
            artifact_store=store,
            tool_gaps=gaps,
            hermes_usage_receipt_ref=foreign,
            **refs,
        )
