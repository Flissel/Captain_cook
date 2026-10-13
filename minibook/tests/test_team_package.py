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
AUTHORITY = {
    "assignment_id": "00000000-0000-0000-0000-000000000201",
    "creation_job_id": "00000000-0000-0000-0000-000000000401",
    "factory_job_id": "00000000-0000-0000-0000-000000000301",
    "correlation_id": "00000000-0000-0000-0000-000000000302",
    "subject_version": 1,
    "attempt": 1,
}


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True).encode()


def _archive(overrides: dict[str, bytes] | None = None) -> bytes:
    files = {
        "src/team.py": b"TEAM = 'sales_pipeline_brief'\n",
        "src/tools/account_snapshot_reader.py": b"def read(value: dict) -> dict:\n    return value\n",
        "src/tools/opportunity_scoring_rules.py": b"def score(value: int) -> int:\n    return value\n",
        "tests/test_team.py": b"from src.team import TEAM\n\ndef test_team():\n    assert TEAM == 'sales_pipeline_brief'\n",
        "factory-candidate.json": _json({"schema": "captain.factory-candidate.v1", "team_manifest_sha256": "BOUND_AT_SEAL"}),
    }
    files.update(overrides or {})
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as package:
        for name, content in files.items():
            package.writestr(name, content)
    return stream.getvalue()


def _receipt(schema: str, **extra: object) -> bytes:
    return _json({"schema": schema, "status": "passed", **AUTHORITY, **extra})


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
    refs: dict[str, object] = {
        "compiled_spec_ref": store.put(_json(compiled), "application/json", namespace="compiled-spec"),
        "team_manifest_ref": store.put(_json(manifest), "application/json", namespace="team-manifest"),
        "source_archive_ref": store.put(_archive(), "application/zip", namespace="source"),
    }
    from minibook.swarm.contracts import CreationJobV3

    job_payload = json.loads(
        (Path(__file__).parents[2] / "tests/fixtures/contracts/minibook_creation_job.v3.json").read_text(encoding="utf-8")
    )
    job_payload.update(
        {
            "deadline_at": "2099-08-09T12:00:00Z",
            "compiled_spec_ref": refs["compiled_spec_ref"].model_dump(mode="json"),
            "required_agent_ids": list(AGENTS),
            "ordered_handoffs": compiled["ordered_handoffs"],
            "tool_requirements": compiled["tool_requirements"],
            "public_assertion_ids": compiled["public_assertion_ids"],
        }
    )
    refs["authority_job"] = CreationJobV3.model_validate(job_payload)
    from minibook.swarm.contracts import FactoryBuildAssignmentV2

    refs["authority_assignment"] = FactoryBuildAssignmentV2.model_validate(
        {
            **AUTHORITY,
            "checkpoint_version": 1,
            "effect_key": "9" * 64,
            "workspace_ref": "workspace://captain/minibook-v3",
            "workspace_policy_sha256": job_payload["workspace_policy_ref"]["sha256"],
            "required_agent_ids": list(AGENTS),
            "ordered_handoffs": compiled["ordered_handoffs"],
            "tool_requirements": compiled["tool_requirements"],
            "allowed_output_roots": ["src", "tests", "evidence"],
            "allowed_commands": [["python", "-m", "compileall", "src"], ["python", "-m", "pytest"]],
            "limits": {"max_wall_clock_seconds": 60, "max_provider_cost_usd": 1.0, "max_retries": 0, "max_files": 32},
            "lease_ref": job_payload["lease_ref"],
            "deadline_at": job_payload["deadline_at"],
        }
    )
    gaps = (ToolGapMarkerV1(schema="TODO_TOOL.v1", gap_id="industry_signal_reader", severity="optional", evidence_ref=store.put(_json({"status": "unresolved"}), "application/json", namespace="tool-gap"), status="unresolved"),)
    assignment = refs["authority_assignment"]
    job = refs["authority_job"]
    session_ref = store.put(_json({"session": "sealed"}), "application/json", namespace="session")
    snapshot_ref = store.put(b"snapshot", "application/zip", namespace="snapshot")
    common = {**AUTHORITY, "checkpoint_version": 1, "effect_key": "9" * 64, "source_archive_ref": refs["source_archive_ref"].model_dump(mode="json"), "codex_session_ref": session_ref.model_dump(mode="json"), "completed_at": "2026-08-09T08:00:00Z"}
    build_bytes = _json({"schema": "captain.package-build-evidence.v1", "producer": "captain", "outcome": "passed", **common, "workspace_snapshot_ref": snapshot_ref.model_dump(mode="json")})
    build_ref = store.put(build_bytes, "application/json", namespace="evidence")
    real_bytes = _json({"schema": "captain.package-real-case-evidence.v1", "producer": "captain", "outcome": "passed", **common, "assertion_ids": ["deterministic_rank"], "declared_test_paths": ["tests/test_team.py"], "evidence_refs": [build_ref.model_dump(mode="json")]})
    real_ref = store.put(real_bytes, "application/json", namespace="evidence")
    hermes_bytes = _json({"schema": "hermes.forge-build-skill-usage-receipt.v1", "producer": "hermes", "outcome": "fulfilled", "creation_job_id": str(job.creation_job_id), "factory_job_id": str(job.factory_job_id), "correlation_id": str(job.correlation_id), "subject_version": job.subject_version, "attempt": job.attempt, "idempotency_key": job.idempotency_key, "released_skill": job.released_skill.model_dump(mode="json"), "public_assertion_ids": list(job.public_assertion_ids), "evidence_refs": [job.compiled_spec_ref.model_dump(mode="json"), job.blueprint_ref.model_dump(mode="json"), job.tool_plan_ref.model_dump(mode="json")]})
    hermes_ref = store.put(hermes_bytes, "application/json", namespace="receipt")
    codex_bytes = _json({"schema": "captain.codex-build-receipt.v1", "receipt_id": "00000000-0000-0000-0000-000000000601", "producer": "captain", "outcome": "sealed", **AUTHORITY, "idempotency_key": assignment.effect_key, "seal_idempotency_key": "a" * 64, "build_brief_ref": job.blueprint_ref.model_dump(mode="json"), "codex_session_ref": session_ref.model_dump(mode="json"), "workspace_ref": assignment.workspace_ref, "workspace_snapshot_ref": snapshot_ref.model_dump(mode="json"), "source_archive_ref": refs["source_archive_ref"].model_dump(mode="json"), "candidate_manifest_ref": refs["team_manifest_ref"].model_dump(mode="json"), "test_evidence_refs": [real_ref.model_dump(mode="json")], "acceptance_assertion_ids": list(job.public_assertion_ids), "completed_at": "2026-08-09T08:00:00Z"})
    codex_ref = store.put(codex_bytes, "application/json", namespace="receipt")
    from minibook.swarm.contracts import FactoryBuildEffectV2
    effect = FactoryBuildEffectV2.model_validate({**AUTHORITY, "checkpoint_version": 1, "effect_key": assignment.effect_key, "hermes_usage_receipt_ref": hermes_ref.model_dump(mode="json"), "codex_build_receipt_ref": codex_ref.model_dump(mode="json"), "codex_session_ref": session_ref.model_dump(mode="json"), "workspace_snapshot_ref": snapshot_ref.model_dump(mode="json"), "source_archive_ref": refs["source_archive_ref"].model_dump(mode="json"), "preliminary_team_manifest_ref": refs["team_manifest_ref"].model_dump(mode="json"), "build_evidence_refs": [build_ref.model_dump(mode="json")], "test_evidence_refs": [real_ref.model_dump(mode="json")], "tool_gaps": [gap.model_dump(mode="json", by_alias=True) for gap in gaps]})
    refs.update({"authority_effect": effect, "hermes_usage_receipt_ref": hermes_ref, "codex_build_receipt_ref": codex_ref, "build_evidence_refs": (build_ref,), "test_evidence_refs": (real_ref,)})
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
    elif mutation == "failed_test":
        failed = _receipt("minibook.real-case-evidence.v1", status="failed", assertion_ids=["deterministic_rank"])
        archive_override["evidence/real-case.json"] = failed
        refs["test_evidence_refs"] = (
            store.put(failed, "application/json", namespace="evidence"),
        )
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
    job = refs["authority_job"]
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
    outcome = await validator.validate(
        job,
        {
            "sealed_team_package": package,
            "authority_assignment": refs["authority_assignment"].model_dump(mode="json"),
        },
        store,
    )
    assert outcome.snapshot["team_package_validated"] is True
    assert validator.assemble_result(job, outcome.snapshot).status == "succeeded"


def test_validate_team_package_binds_referenced_receipt_bytes(tmp_path: Path) -> None:
    store, _, _, refs, gaps = _fixture(tmp_path)
    foreign = store.put(_json({"schema": "hermes.factory-skill-usage-receipt.v1", "status": "passed", "foreign": True}), "application/json", namespace="receipt")
    refs["hermes_usage_receipt_ref"] = foreign
    with pytest.raises(TeamPackageError, match="receipt refs"):
        validate_team_package(
            artifact_store=store,
            tool_gaps=gaps,
            **refs,
        )


def test_compiled_topology_cannot_override_authoritative_v3_job(tmp_path: Path) -> None:
    store, compiled, manifest, refs, gaps = _fixture(tmp_path)
    compiled["required_agent_ids"] = [AGENTS[0]]
    compiled["ordered_handoffs"] = []
    manifest["agents"] = manifest["agents"][:1]
    manifest["handoffs"] = []
    refs["compiled_spec_ref"] = store.put(_json(compiled), "application/json", namespace="compiled-spec")
    refs["team_manifest_ref"] = store.put(_json(manifest), "application/json", namespace="team-manifest")
    with pytest.raises(TeamPackageError, match="CreationJobV3 authority"):
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs)


@pytest.mark.parametrize(
    "member",
    (
        ".env",
        "secrets/token.txt",
        "evidence/session-transcript.json",
        "build/metadata.json",
        "package-manifest.json",
        "evidence/minibook-cas-readback-receipt.json",
    ),
)
def test_source_archive_rejects_undeclared_and_publisher_owned_members(
    tmp_path: Path, member: str
) -> None:
    store, _, _, refs, gaps = _fixture(tmp_path)
    refs["source_archive_ref"] = store.put(
        _archive({member: b"unsafe"}), "application/zip", namespace="source"
    )
    with pytest.raises(TeamPackageError, match="safe file set"):
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs)


def test_source_archive_rejects_forbidden_name_even_when_declared(tmp_path: Path) -> None:
    store, _, manifest, refs, gaps = _fixture(tmp_path)
    manifest["source_files"].append("src/session-transcript.py")
    refs["team_manifest_ref"] = store.put(_json(manifest), "application/json", namespace="team-manifest")
    refs["source_archive_ref"] = store.put(
        _archive({"src/session-transcript.py": b"TRANSCRIPT = 'private'\n"}),
        "application/zip",
        namespace="source",
    )
    with pytest.raises(TeamPackageError, match="forbidden"):
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs)


@pytest.mark.asyncio
async def test_final_validation_rejects_package_under_foreign_job_authority(tmp_path: Path) -> None:
    store, _, _, refs, gaps = _fixture(tmp_path)
    sealed = seal_team_package(
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs),
        artifact_store=store,
    )
    from minibook.swarm.contracts import CreationJobV3

    foreign_payload = json.loads(
        (Path(__file__).parents[2] / "tests/fixtures/contracts/minibook_creation_job.v3.json").read_text(encoding="utf-8")
    )
    foreign_payload["deadline_at"] = "2099-08-09T12:00:00Z"
    foreign = CreationJobV3.model_validate(foreign_payload)
    with pytest.raises((ValueError, TeamPackageError), match="agents|authority"):
        await TeamPackageV3Validator().validate(
            foreign, _sealed_snapshot(sealed, refs["authority_assignment"]), store
        )


@pytest.mark.asyncio
async def test_final_validation_rereads_every_manifest_child(tmp_path: Path) -> None:
    store, _, _, refs, gaps = _fixture(tmp_path)
    sealed = seal_team_package(
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs),
        artifact_store=store,
    )
    package_manifest = json.loads(store.read_bytes(sealed.package_manifest_ref))
    source_entry = next(item for item in package_manifest["files"] if item["path"] == "src/team.py")
    from minibook.swarm.contracts import ArtifactRef

    store.local_path(
        ArtifactRef.model_validate(
            {key: source_entry[key] for key in ("uri", "sha256", "media_type")}
        )
    ).write_bytes(b"TEAM = 'tampered'\n")
    snapshot = _sealed_snapshot(sealed, refs["authority_assignment"])
    with pytest.raises(Exception, match="digest"):
        await TeamPackageV3Validator().validate(refs["authority_job"], snapshot, store)


@pytest.mark.asyncio
async def test_final_validation_requires_exact_complete_readback_ref_set(tmp_path: Path) -> None:
    store, _, _, refs, gaps = _fixture(tmp_path)
    sealed = seal_team_package(
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs),
        artifact_store=store,
    )
    receipt = dict(sealed.readback_receipt)
    receipt["artifacts"] = receipt["artifacts"][:-1]
    replacement = store.put(_json(receipt), "application/json", namespace="cas-readback")
    snapshot = _sealed_snapshot(sealed, refs["authority_assignment"])
    snapshot["sealed_team_package"]["cas_readback_receipt_ref"] = replacement.model_dump(mode="json")
    with pytest.raises(ValueError, match="readback receipt"):
        await TeamPackageV3Validator().validate(refs["authority_job"], snapshot, store)


def _sealed_snapshot(sealed: object, assignment: object) -> dict[str, object]:
    return {
        "authority_assignment": assignment.model_dump(mode="json"),
        "sealed_team_package": {
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
    }


def test_worker_controlled_test_is_never_executed_on_host(tmp_path: Path) -> None:
    store, _, _, refs, gaps = _fixture(tmp_path)
    marker = tmp_path / "host-side-effect.txt"
    malicious = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
        "def test_receipt_only():\n    assert True\n"
    ).encode()
    refs["source_archive_ref"] = store.put(
        _archive({"tests/test_team.py": malicious}), "application/zip", namespace="source"
    )

    validate_team_package(artifact_store=store, tool_gaps=gaps, **refs)

    assert not marker.exists()


def test_archive_file_limit_is_enforced_before_decompression(tmp_path: Path) -> None:
    store, _, _, refs, gaps = _fixture(tmp_path)
    assignment = refs["authority_assignment"]
    refs["authority_assignment"] = assignment.model_copy(
        update={"limits": assignment.limits.model_copy(update={"max_files": 4})}
    )
    with pytest.raises(TeamPackageError, match="file limit"):
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs)


def test_archive_rejects_excessive_member_or_compression_ratio(tmp_path: Path) -> None:
    store, _, manifest, refs, gaps = _fixture(tmp_path)
    manifest["source_files"].append("src/oversized.py")
    refs["team_manifest_ref"] = store.put(_json(manifest), "application/json", namespace="team-manifest")
    refs["source_archive_ref"] = store.put(
        _archive({"src/oversized.py": b"#" + b"x" * (3 * 1024 * 1024)}),
        "application/zip",
        namespace="source",
    )
    with pytest.raises(TeamPackageError, match="size|compression"):
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs)


@pytest.mark.parametrize(
    ("first", "second"),
    (
        ("src/team.py", "src/TEAM.py"),
        ("src/caf\N{LATIN SMALL LETTER E WITH ACUTE}.py", "src/cafe\N{COMBINING ACUTE ACCENT}.py"),
    ),
)
def test_archive_rejects_casefold_or_unicode_normalization_collision(
    tmp_path: Path, first: str, second: str
) -> None:
    store, _, manifest, refs, gaps = _fixture(tmp_path)
    if first not in manifest["source_files"]:
        manifest["source_files"].append(first)
    manifest["source_files"].append(second)
    refs["team_manifest_ref"] = store.put(_json(manifest), "application/json", namespace="team-manifest")
    refs["source_archive_ref"] = store.put(
        _archive({first: b"VALUE = 1\n", second: b"VALUE = 1\n"}),
        "application/zip",
        namespace="source",
    )
    with pytest.raises(TeamPackageError, match="collision|normalized"):
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs)


@pytest.mark.parametrize("unsafe", ("src/CON.py", "src/trailing.py. ", "src/tool.py:stream"))
def test_archive_rejects_windows_reserved_or_unsafe_names(
    tmp_path: Path, unsafe: str
) -> None:
    store, _, manifest, refs, gaps = _fixture(tmp_path)
    manifest["source_files"].append(unsafe)
    refs["team_manifest_ref"] = store.put(_json(manifest), "application/json", namespace="team-manifest")
    refs["source_archive_ref"] = store.put(
        _archive({unsafe: b"VALUE = 1\n"}), "application/zip", namespace="source"
    )
    with pytest.raises(TeamPackageError, match="Windows|unsafe"):
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs)


def test_exact_topology_rejects_malformed_extra_agent_entry(tmp_path: Path) -> None:
    store, _, manifest, refs, gaps = _fixture(tmp_path)
    manifest["agents"].append(7)
    refs["team_manifest_ref"] = store.put(_json(manifest), "application/json", namespace="team-manifest")
    with pytest.raises(TeamPackageError, match="agent"):
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs)


def test_identical_content_paths_seal_with_one_receipt_identity(tmp_path: Path) -> None:
    store, _, manifest, refs, gaps = _fixture(tmp_path)
    manifest["source_files"].append("src/team_copy.py")
    refs["team_manifest_ref"] = store.put(_json(manifest), "application/json", namespace="team-manifest")
    refs["source_archive_ref"] = store.put(
        _archive({"src/team_copy.py": b"TEAM = 'sales_pipeline_brief'\n"}),
        "application/zip",
        namespace="source",
    )
    sealed = seal_team_package(
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs),
        artifact_store=store,
    )
    package_manifest = json.loads(store.read_bytes(sealed.package_manifest_ref))
    entries = {item["path"]: item for item in package_manifest["files"]}
    assert entries["src/team.py"]["uri"] == entries["src/team_copy.py"]["uri"]
    identities = [(item["uri"], item["sha256"], item["media_type"]) for item in sealed.readback_receipt["artifacts"]]
    assert len(identities) == len(set(identities))


@pytest.mark.parametrize("mutation", ("schema", "producer", "effect", "session", "archive"))
def test_typed_codex_receipt_rejects_forged_authority(tmp_path: Path, mutation: str) -> None:
    store, _, _, refs, gaps = _fixture(tmp_path)
    payload = json.loads(store.read_bytes(refs["codex_build_receipt_ref"]))
    if mutation == "schema": payload["schema"] = "captain.codex-build-receipt.v0"
    elif mutation == "producer": payload["producer"] = "hermes"
    elif mutation == "effect": payload["idempotency_key"] = "0" * 64
    elif mutation == "session": payload["codex_session_ref"] = payload["build_brief_ref"]
    else: payload["source_archive_ref"] = payload["workspace_snapshot_ref"]
    forged = store.put(_json(payload), "application/json", namespace="receipt")
    refs["codex_build_receipt_ref"] = forged
    refs["authority_effect"] = refs["authority_effect"].model_copy(update={"codex_build_receipt_ref": forged})
    with pytest.raises(TeamPackageError, match="typed|Codex"):
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs)


def test_minimal_known_id_receipt_is_not_authority(tmp_path: Path) -> None:
    store, _, _, refs, gaps = _fixture(tmp_path)
    forged = store.put(_json({"schema": "hermes.forge-build-skill-usage-receipt.v1", "producer": "hermes", "outcome": "fulfilled", **AUTHORITY}), "application/json", namespace="receipt")
    refs["hermes_usage_receipt_ref"] = forged
    refs["authority_effect"] = refs["authority_effect"].model_copy(update={"hermes_usage_receipt_ref": forged})
    with pytest.raises(TeamPackageError, match="typed"):
        validate_team_package(artifact_store=store, tool_gaps=gaps, **refs)
