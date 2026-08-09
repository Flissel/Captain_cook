"""Strict validation and deterministic sealing for V3 team packages."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .artifact_store import FilesystemCreationArtifactStore
from .contracts import (
    AgentHandoffV1,
    ArtifactRef,
    CreationJobV3,
    CreationResultV2,
    FactoryBuildAssignmentV2,
    FactoryToolRequirementV1,
    ToolGapMarkerV1,
)
from .creation_export import CreationExportError, _build_deterministic_zip


class TeamPackageError(RuntimeError):
    """A candidate does not satisfy its compiled Captain contract."""


@dataclass(frozen=True)
class ValidatedTeamPackage:
    authority_job: CreationJobV3
    compiled_spec: dict[str, Any]
    team_manifest: dict[str, Any]
    files: Mapping[PurePosixPath, bytes]
    tool_gaps: tuple[ToolGapMarkerV1, ...]


@dataclass(frozen=True)
class SealedTeamPackage:
    team_manifest_ref: ArtifactRef
    candidate_manifest_ref: ArtifactRef
    source_archive_ref: ArtifactRef
    package_manifest_ref: ArtifactRef
    hermes_usage_receipt_ref: ArtifactRef
    codex_build_receipt_ref: ArtifactRef
    build_evidence_refs: tuple[ArtifactRef, ...]
    real_case_evidence_refs: tuple[ArtifactRef, ...]
    package_ref: ArtifactRef
    readback_receipt_ref: ArtifactRef
    readback_receipt: dict[str, Any]


_FORBIDDEN_REQUIRED_TOOL_MARKERS = (
    re.compile(rb"\bTODO\b", re.IGNORECASE),
    re.compile(rb"\bNotImplementedError\b"),
    re.compile(rb"\bpass\s*(?:#.*)?$", re.MULTILINE),
)
_INVENTED_SCORING = re.compile(
    rb"(?:SCORING_WEIGHTS|scoring_weights|weights\s*=)", re.IGNORECASE
)
_MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
_MAX_MEMBER_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_UNCOMPRESSED_BYTES = 8 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_DECLARED_FILES = 4096
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{value}" for value in range(1, 10)),
    *(f"LPT{value}" for value in range(1, 10)),
}


def validate_team_package(
    *,
    authority_job: CreationJobV3,
    authority_assignment: FactoryBuildAssignmentV2,
    compiled_spec_ref: ArtifactRef,
    team_manifest_ref: ArtifactRef,
    source_archive_ref: ArtifactRef,
    artifact_store: FilesystemCreationArtifactStore,
    tool_gaps: Iterable[ToolGapMarkerV1] = (),
    hermes_usage_receipt_ref: ArtifactRef,
    codex_build_receipt_ref: ArtifactRef,
    build_evidence_refs: Iterable[ArtifactRef],
    test_evidence_refs: Iterable[ArtifactRef],
) -> ValidatedTeamPackage:
    """Read all authoritative bytes from CAS and execute the declared local gates."""

    if not isinstance(authority_job, CreationJobV3):
        raise TypeError("team package authority must be CreationJobV3")
    if not isinstance(authority_assignment, FactoryBuildAssignmentV2):
        raise TypeError("team package assignment authority is invalid")
    _bind_assignment_authority(authority_job, authority_assignment)
    if compiled_spec_ref != authority_job.compiled_spec_ref:
        raise TeamPackageError("compiled specification ref differs from CreationJobV3 authority")
    compiled = _json_object(artifact_store.read_bytes(compiled_spec_ref), "compiled specification")
    _bind_compiled_authority(compiled, authority_job)
    manifest_bytes = artifact_store.read_bytes(team_manifest_ref)
    manifest = _parse_team_manifest(
        _json_object(manifest_bytes, "team manifest"),
        max_files=min(authority_assignment.limits.max_files, _MAX_DECLARED_FILES),
    )
    archive_bytes = artifact_store.read_bytes(source_archive_ref)
    files = _read_safe_zip(
        archive_bytes,
        max_files=authority_assignment.limits.max_files,
    )
    archived_manifest = files.get(PurePosixPath("team_manifest.json"))
    if archived_manifest is not None and _canonical_json(_json_object(archived_manifest, "archived team manifest")) != _canonical_json(manifest):
        raise TeamPackageError("team manifest drift between archive and authoritative reference")

    required_agents = tuple(_require_strings(compiled.get("required_agent_ids"), "compiled agents"))
    required_handoffs = tuple(_handoff(item) for item in _require_list(compiled.get("ordered_handoffs"), "compiled handoffs"))
    require_exact_topology(manifest, required_agents, required_handoffs)
    _require_closed_safe_file_set(manifest, files)
    _validate_tools(compiled, manifest, files)
    typed_gaps = tuple(tool_gaps)
    typed_build_evidence_refs = tuple(build_evidence_refs)
    typed_test_evidence_refs = tuple(test_evidence_refs)
    if not typed_build_evidence_refs or not typed_test_evidence_refs:
        raise TeamPackageError("authorized build and real-case evidence refs are required")
    _validate_gaps(compiled, typed_gaps)
    _validate_declared_files(manifest, files)
    _require_receipts(
        compiled,
        files,
        authority_job=authority_job,
        authority_assignment=authority_assignment,
    )
    _bind_referenced_evidence(
        artifact_store,
        files,
        hermes_usage_receipt_ref=hermes_usage_receipt_ref,
        codex_build_receipt_ref=codex_build_receipt_ref,
        build_evidence_refs=typed_build_evidence_refs,
        test_evidence_refs=typed_test_evidence_refs,
    )
    _parse_and_compile_without_execution(files, manifest)
    return ValidatedTeamPackage(authority_job, compiled, manifest, files, typed_gaps)


def require_exact_topology(
    manifest: Mapping[str, Any],
    required_agents: tuple[str, ...],
    required_handoffs: tuple[AgentHandoffV1, ...],
) -> None:
    agents = _require_list(manifest.get("agents"), "team agents")
    actual_agents = tuple(
        item if isinstance(item, str) else _require_string(item.get("agent_id"), "agent id")
        for item in agents
        if isinstance(item, (str, dict))
    )
    actual_handoffs = tuple(_handoff(item) for item in _require_list(manifest.get("handoffs"), "team handoffs"))
    if actual_agents != required_agents:
        raise TeamPackageError("team agents do not match compiled specification")
    if actual_handoffs != required_handoffs:
        raise TeamPackageError("team handoffs do not match compiled specification")


def seal_team_package(
    validated: ValidatedTeamPackage,
    *,
    artifact_store: FilesystemCreationArtifactStore,
) -> SealedTeamPackage:
    """Publish every safe file and a deterministic ZIP, then read every digest back."""

    files = dict(validated.files)
    manifest_bytes = _canonical_json(validated.team_manifest)
    files[PurePosixPath("team_manifest.json")] = manifest_bytes
    candidate_path = PurePosixPath("factory-candidate.json")
    candidate = _json_object(_required_file(files, candidate_path), "candidate manifest")
    if candidate.get("schema") != "captain.factory-candidate.v1":
        raise TeamPackageError("candidate manifest schema is invalid")
    candidate["team_manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    candidate["compiled_spec_ref"] = validated.authority_job.compiled_spec_ref.model_dump(mode="json")
    candidate["required_agent_ids"] = list(validated.authority_job.required_agent_ids)
    candidate["ordered_handoffs"] = [
        value.model_dump(mode="json") for value in validated.authority_job.ordered_handoffs
    ]
    candidate["tool_requirements"] = [
        value.model_dump(mode="json") for value in validated.authority_job.tool_requirements
    ]
    candidate["public_assertion_ids"] = list(validated.authority_job.public_assertion_ids)
    candidate["allowed_optional_tool_gaps"] = [
        value.tool_id
        for value in validated.authority_job.tool_requirements
        if not value.required
    ]
    candidate["artifacts"] = [
        {
            "path": str(path),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for path, content in sorted(files.items(), key=lambda item: str(item[0]))
        if path != candidate_path
    ]
    files[candidate_path] = _canonical_json(candidate)

    published: dict[PurePosixPath, ArtifactRef] = {}
    for path in sorted(files, key=str):
        media_type = "application/json" if path.suffix == ".json" else "text/x-python" if path.suffix == ".py" else "application/octet-stream"
        published[path] = artifact_store.put(files[path], media_type, namespace="team-file")
    package_manifest = {
        "schema": "minibook.team-package-manifest.v1",
        "files": [
            {"path": str(path), **ref.model_dump(mode="json")}
            for path, ref in published.items()
        ],
    }
    package_manifest_bytes = _canonical_json(package_manifest)
    package_manifest_ref = artifact_store.put(package_manifest_bytes, "application/json", namespace="package-manifest")
    package_bytes = _build_deterministic_zip(files | {PurePosixPath("package-manifest.json"): package_manifest_bytes})
    package_ref = artifact_store.put(package_bytes, "application/zip", namespace="team-package")

    all_refs = (*published.values(), package_manifest_ref, package_ref)
    receipt = artifact_store.build_readback_receipt(all_refs)
    receipt_ref = artifact_store.put(_canonical_json(receipt), "application/json", namespace="cas-readback")
    artifact_store.read_bytes(receipt_ref)
    build_refs = tuple(ref for path, ref in published.items() if str(path).startswith("evidence/build"))
    real_refs = tuple(ref for path, ref in published.items() if str(path).startswith("evidence/real-case"))
    return SealedTeamPackage(
        team_manifest_ref=published[PurePosixPath("team_manifest.json")],
        candidate_manifest_ref=published[candidate_path],
        source_archive_ref=package_ref,
        package_manifest_ref=package_manifest_ref,
        hermes_usage_receipt_ref=published[PurePosixPath("evidence/hermes-factory-skill-usage-receipt.json")],
        codex_build_receipt_ref=published[PurePosixPath("evidence/codex-build-receipt.json")],
        build_evidence_refs=build_refs,
        real_case_evidence_refs=real_refs,
        package_ref=package_ref,
        readback_receipt_ref=receipt_ref,
        readback_receipt=receipt,
    )


def verify_sealed_team_package(
    result: CreationResultV2,
    *,
    authority_job: CreationJobV3,
    authority_assignment: FactoryBuildAssignmentV2,
    artifact_store: FilesystemCreationArtifactStore,
) -> None:
    """Re-read and cross-bind the final ZIP, manifest, receipt, and every CAS child."""

    _bind_assignment_authority(authority_job, authority_assignment)
    if result.status != "succeeded":
        raise ValueError("sealed package result is not successful")
    top_level = (
        result.team_manifest_ref,
        result.candidate_manifest_ref,
        result.source_archive_ref,
        result.package_manifest_ref,
        result.hermes_usage_receipt_ref,
        result.codex_build_receipt_ref,
        *result.build_evidence_refs,
        *result.real_case_evidence_refs,
        result.cas_readback_receipt_ref,
    )
    if any(reference is None for reference in top_level):
        raise ValueError("sealed package top-level refs are incomplete")
    typed_top_level = tuple(reference for reference in top_level if reference is not None)
    archive_bytes = artifact_store.read_bytes(result.source_archive_ref)  # type: ignore[arg-type]
    archive_files = _read_safe_zip(
        archive_bytes,
        max_files=authority_assignment.limits.max_files + 2,
    )
    manifest_bytes = artifact_store.read_bytes(result.package_manifest_ref)  # type: ignore[arg-type]
    if archive_files.get(PurePosixPath("package-manifest.json")) != manifest_bytes:
        raise ValueError("package manifest does not match sealed archive")
    package_manifest = _json_object(manifest_bytes, "package manifest")
    if package_manifest.get("schema") != "minibook.team-package-manifest.v1":
        raise ValueError("package manifest schema is invalid")
    if manifest_bytes != _canonical_json(package_manifest):
        raise ValueError("package manifest is not canonical")
    entries = package_manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("package manifest files are invalid")
    children: dict[PurePosixPath, ArtifactRef] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("package manifest child is invalid")
        path = _safe_path(entry.get("path"), "package manifest child")
        if path == PurePosixPath("package-manifest.json") or path in children:
            raise ValueError("package manifest child path is reserved or duplicated")
        try:
            reference = ArtifactRef.model_validate(
                {key: entry.get(key) for key in ("uri", "sha256", "media_type")}
            )
        except Exception as exc:
            raise ValueError("package manifest child ref is invalid") from exc
        child_bytes = artifact_store.read_bytes(reference)
        if archive_files.get(path) != child_bytes:
            raise ValueError("package manifest child differs from sealed archive")
        children[path] = reference
    if set(archive_files) != {*children, PurePosixPath("package-manifest.json")}:
        raise ValueError("sealed archive members differ from package manifest")

    expected_top_level_children = {
        PurePosixPath("team_manifest.json"): result.team_manifest_ref,
        PurePosixPath("factory-candidate.json"): result.candidate_manifest_ref,
        PurePosixPath("evidence/hermes-factory-skill-usage-receipt.json"): result.hermes_usage_receipt_ref,
        PurePosixPath("evidence/codex-build-receipt.json"): result.codex_build_receipt_ref,
    }
    for path, reference in expected_top_level_children.items():
        if children.get(path) != reference:
            raise ValueError("top-level ref differs from package manifest child")
    team_manifest = _json_object(
        artifact_store.read_bytes(result.team_manifest_ref),  # type: ignore[arg-type]
        "team manifest",
    )
    require_exact_topology(
        team_manifest,
        authority_job.required_agent_ids,
        authority_job.ordered_handoffs,
    )
    candidate = _json_object(
        artifact_store.read_bytes(result.candidate_manifest_ref),  # type: ignore[arg-type]
        "candidate manifest",
    )
    expected_candidate_authority = {
        "compiled_spec_ref": authority_job.compiled_spec_ref.model_dump(mode="json"),
        "required_agent_ids": list(authority_job.required_agent_ids),
        "ordered_handoffs": [
            value.model_dump(mode="json") for value in authority_job.ordered_handoffs
        ],
        "tool_requirements": [
            value.model_dump(mode="json") for value in authority_job.tool_requirements
        ],
        "public_assertion_ids": list(authority_job.public_assertion_ids),
        "allowed_optional_tool_gaps": [
            value.tool_id for value in authority_job.tool_requirements if not value.required
        ],
    }
    if any(candidate.get(key) != value for key, value in expected_candidate_authority.items()):
        raise ValueError("candidate package differs from CreationJobV3 authority")
    if candidate.get("team_manifest_sha256") != hashlib.sha256(
        artifact_store.read_bytes(result.team_manifest_ref)  # type: ignore[arg-type]
    ).hexdigest():
        raise ValueError("candidate team manifest binding differs from authority")
    if tuple(reference for path, reference in children.items() if str(path).startswith("evidence/build")) != result.build_evidence_refs:
        raise ValueError("build evidence refs differ from package manifest")
    if tuple(reference for path, reference in children.items() if str(path).startswith("evidence/real-case")) != result.real_case_evidence_refs:
        raise ValueError("real-case refs differ from package manifest")

    receipt_bytes = artifact_store.read_bytes(result.cas_readback_receipt_ref)  # type: ignore[arg-type]
    receipt = _json_object(receipt_bytes, "CAS readback receipt")
    if receipt.get("schema") != "minibook.cas-readback-receipt.v1" or receipt_bytes != _canonical_json(receipt):
        raise ValueError("CAS readback receipt is invalid or noncanonical")
    receipt_entries = receipt.get("artifacts")
    if not isinstance(receipt_entries, list):
        raise ValueError("CAS readback receipt artifacts are invalid")
    actual_receipt: set[tuple[str, str, str]] = set()
    for item in receipt_entries:
        if not isinstance(item, dict) or item.get("verified") is not True:
            raise ValueError("CAS readback receipt contains an unverified artifact")
        try:
            reference = ArtifactRef.model_validate(
                {key: item.get(key) for key in ("uri", "sha256", "media_type")}
            )
        except Exception as exc:
            raise ValueError("CAS readback receipt artifact is invalid") from exc
        identity = (reference.uri, reference.sha256, reference.media_type)
        if identity in actual_receipt:
            raise ValueError("CAS readback receipt contains duplicate artifacts")
        actual_receipt.add(identity)
    expected_receipt_refs = (*children.values(), result.package_manifest_ref, result.source_archive_ref)
    expected_receipt = {
        (reference.uri, reference.sha256, reference.media_type)
        for reference in expected_receipt_refs
        if reference is not None
    }
    if actual_receipt != expected_receipt or len(receipt_entries) != len(expected_receipt):
        raise ValueError("CAS readback receipt does not contain the exact complete ref set")
    artifact_store.build_readback_receipt(typed_top_level)


def _bind_compiled_authority(
    compiled: Mapping[str, Any], authority_job: CreationJobV3
) -> None:
    compiled_agents = _require_strings(compiled.get("required_agent_ids"), "compiled agents")
    compiled_handoffs = tuple(
        _handoff(item)
        for item in _require_list(compiled.get("ordered_handoffs"), "compiled handoffs")
    )
    try:
        compiled_tools = tuple(
            FactoryToolRequirementV1.model_validate(item)
            for item in _require_list(
                compiled.get("tool_requirements"), "compiled tool requirements"
            )
        )
    except Exception as exc:
        raise TeamPackageError("compiled tool requirements are invalid") from exc
    compiled_assertions = _require_strings(
        compiled.get("public_assertion_ids"), "compiled public assertions"
    )
    expected_optional = tuple(
        requirement.tool_id
        for requirement in authority_job.tool_requirements
        if not requirement.required
    )
    allowed_optional = tuple(
        _require_strings(
            compiled.get("allowed_optional_tool_gaps", []),
            "compiled optional tool gap policy",
        )
    )
    if (
        compiled_agents != authority_job.required_agent_ids
        or compiled_handoffs != authority_job.ordered_handoffs
        or compiled_tools != authority_job.tool_requirements
        or compiled_assertions != authority_job.public_assertion_ids
        or allowed_optional != expected_optional
    ):
        raise TeamPackageError("compiled specification differs from CreationJobV3 authority")


def _bind_assignment_authority(
    job: CreationJobV3, assignment: FactoryBuildAssignmentV2
) -> None:
    if (
        assignment.assignment_id != job.command_id
        or assignment.creation_job_id != job.creation_job_id
        or assignment.factory_job_id != job.factory_job_id
        or assignment.correlation_id != job.correlation_id
        or assignment.subject_version != job.subject_version
        or assignment.attempt != job.attempt
        or assignment.required_agent_ids != job.required_agent_ids
        or assignment.ordered_handoffs != job.ordered_handoffs
        or assignment.tool_requirements != job.tool_requirements
        or assignment.workspace_policy_sha256 != job.workspace_policy_ref.sha256
        or assignment.lease_ref != job.lease_ref
        or assignment.deadline_at != job.deadline_at
    ):
        raise TeamPackageError("FactoryBuildAssignmentV2 differs from CreationJobV3 authority")


def _parse_team_manifest(
    value: dict[str, Any], *, max_files: int
) -> dict[str, Any]:
    allowed_keys = {
        "schema", "agents", "handoffs", "tools", "source_files", "test_files"
    }
    if set(value) != allowed_keys or value.get("schema") != "minibook.team-manifest.v2":
        raise TeamPackageError("team manifest schema or fields are invalid")
    agents = _require_list(value.get("agents"), "team agents")
    if len(agents) > max_files:
        raise TeamPackageError("team manifest agent list limit exceeded")
    for item in agents:
        if not isinstance(item, dict) or set(item) != {"agent_id"}:
            raise TeamPackageError("team agent entry is malformed")
        _require_string(item.get("agent_id"), "agent id")
    handoffs = _require_list(value.get("handoffs"), "team handoffs")
    if len(handoffs) > max_files:
        raise TeamPackageError("team manifest handoff list limit exceeded")
    for item in handoffs:
        _handoff(item)
    tools = _require_list(value.get("tools"), "team tools")
    if len(tools) > max_files:
        raise TeamPackageError("team manifest tool list limit exceeded")
    tool_keys = {
        "tool_id", "input_schema", "output_schema", "implementation_path", "test_path"
    }
    for item in tools:
        if not isinstance(item, dict) or set(item) != tool_keys:
            raise TeamPackageError("team tool entry is malformed")
        _require_string(item.get("tool_id"), "tool id")
        if any(
            not isinstance(item.get(field), dict)
            or not isinstance(item[field].get("type"), str)
            for field in ("input_schema", "output_schema")
        ):
            raise TeamPackageError("team tool schema is untyped")
        _safe_path(item.get("implementation_path"), "tool implementation")
        _safe_path(item.get("test_path"), "tool test")
    source_files = _require_list(value.get("source_files"), "source files")
    test_files = _require_list(value.get("test_files"), "test files")
    if (
        len(source_files) > max_files
        or len(test_files) > max_files
        or len(source_files) + len(test_files) > max_files
    ):
        raise TeamPackageError("team manifest declared file limit exceeded")
    for field, entries in (("source", source_files), ("test", test_files)):
        paths = tuple(_safe_path(item, f"{field} file") for item in entries)
        if len(paths) != len(set(paths)):
            raise TeamPackageError(f"team manifest contains duplicate {field} paths")
    return value


def _require_closed_safe_file_set(
    manifest: Mapping[str, Any], files: Mapping[PurePosixPath, bytes]
) -> None:
    source_paths = tuple(
        _safe_path(value, "source file")
        for value in _require_list(manifest.get("source_files"), "source files")
    )
    test_paths = tuple(
        _safe_path(value, "test file")
        for value in _require_list(manifest.get("test_files"), "test files")
    )
    if any(path.parts[0] != "src" for path in source_paths):
        raise TeamPackageError("declared source file is outside the safe source root")
    if any(path.parts[0] != "tests" for path in test_paths):
        raise TeamPackageError("declared test file is outside the safe test root")
    for path in (*source_paths, *test_paths):
        lowered = tuple(part.casefold() for part in path.parts)
        filename = lowered[-1]
        if (
            any(part in {".git", "__pycache__", "secrets", "credentials", "logs"} for part in lowered)
            or any(part == ".env" or part.startswith(".env.") for part in lowered)
            or any("transcript" in part or "secret" in part or "credential" in part or "password" in part for part in lowered)
            or filename in {"token", "token.txt", "token.json"}
            or PurePosixPath(filename).suffix in {".log", ".pyc", ".pyo"}
        ):
            raise TeamPackageError("declared package path uses a forbidden sensitive name")
    canonical = {
        PurePosixPath("factory-candidate.json"),
        PurePosixPath("team_manifest.json"),
        PurePosixPath("evidence/build.json"),
        PurePosixPath("evidence/real-case.json"),
        PurePosixPath("evidence/hermes-factory-skill-usage-receipt.json"),
        PurePosixPath("evidence/codex-build-receipt.json"),
    }
    allowed = {*source_paths, *test_paths, *canonical}
    if not set(files).issubset(allowed):
        raise TeamPackageError("source archive contains a member outside the closed safe file set")
    tools = _require_list(manifest.get("tools"), "team tools")
    for tool in tools:
        if not isinstance(tool, dict):
            raise TeamPackageError("team tool declaration is invalid")
        if _safe_path(tool.get("implementation_path"), "tool implementation") not in source_paths:
            raise TeamPackageError("tool implementation is not a declared source file")
        if _safe_path(tool.get("test_path"), "tool test") not in test_paths:
            raise TeamPackageError("tool test is not a declared test file")


def _validate_tools(compiled: Mapping[str, Any], manifest: Mapping[str, Any], files: Mapping[PurePosixPath, bytes]) -> None:
    requirements = _require_list(compiled.get("tool_requirements"), "compiled tool requirements")
    required = tuple(_require_string(item.get("tool_id"), "tool id") for item in requirements if isinstance(item, dict) and item.get("required") is True)
    tools = _require_list(manifest.get("tools"), "team tools")
    by_id = {item.get("tool_id"): item for item in tools if isinstance(item, dict) and isinstance(item.get("tool_id"), str)}
    if any(tool_id not in by_id for tool_id in required):
        raise TeamPackageError("required tool is missing")
    for tool_id in required:
        tool = by_id[tool_id]
        for schema_name in ("input_schema", "output_schema"):
            schema = tool.get(schema_name)
            if not isinstance(schema, dict) or not isinstance(schema.get("type"), str):
                raise TeamPackageError("required tool schema is untyped")
        impl = _safe_path(tool.get("implementation_path"), "tool implementation")
        test = _safe_path(tool.get("test_path"), "tool test")
        source = _required_file(files, impl)
        _required_file(files, test)
        if any(pattern.search(source) for pattern in _FORBIDDEN_REQUIRED_TOOL_MARKERS):
            raise TeamPackageError("required tool contains a forbidden implementation marker")
        if tool_id == "opportunity_scoring_rules" and _INVENTED_SCORING.search(source):
            raise TeamPackageError("model-invented scoring weights are forbidden")


def _validate_gaps(compiled: Mapping[str, Any], gaps: tuple[ToolGapMarkerV1, ...]) -> None:
    requirements = _require_list(compiled.get("tool_requirements"), "compiled tool requirements")
    optional = tuple(_require_string(item.get("tool_id"), "optional tool id") for item in requirements if isinstance(item, dict) and item.get("required") is False)
    allowed = tuple(compiled.get("allowed_optional_tool_gaps", optional))
    actual = tuple(gap.gap_id for gap in gaps if gap.severity == "optional" and gap.status == "unresolved")
    if any(gap.severity != "optional" or gap.status != "unresolved" for gap in gaps) or actual != allowed or actual != optional:
        raise TeamPackageError("optional tool gaps do not match compiled policy")


def _validate_declared_files(manifest: Mapping[str, Any], files: Mapping[PurePosixPath, bytes]) -> None:
    for field in ("source_files", "test_files"):
        paths = tuple(_safe_path(value, field) for value in _require_list(manifest.get(field), field))
        if not paths:
            raise TeamPackageError(f"{field} must not be empty")
        for path in paths:
            if not _required_file(files, path).strip():
                raise TeamPackageError(f"declared file is empty: {path}")


def _require_receipts(
    compiled: Mapping[str, Any],
    files: Mapping[PurePosixPath, bytes],
    *,
    authority_job: CreationJobV3,
    authority_assignment: FactoryBuildAssignmentV2,
) -> None:
    required = (
        "evidence/build.json",
        "evidence/real-case.json",
        "evidence/hermes-factory-skill-usage-receipt.json",
        "evidence/codex-build-receipt.json",
        "factory-candidate.json",
    )
    receipts = {name: _json_object(_required_file(files, PurePosixPath(name)), name) for name in required}
    for name, value in receipts.items():
        if name.startswith("evidence/") and value.get("status") not in {"passed", "succeeded"}:
            raise TeamPackageError(f"declared evidence did not pass: {name}")
        expected_authority = {
            "assignment_id": str(authority_assignment.assignment_id),
            "creation_job_id": str(authority_job.creation_job_id),
            "factory_job_id": str(authority_job.factory_job_id),
            "correlation_id": str(authority_job.correlation_id),
            "subject_version": authority_job.subject_version,
            "attempt": authority_job.attempt,
        }
        if name.startswith("evidence/") and any(
            value.get(key) != expected for key, expected in expected_authority.items()
        ):
            raise TeamPackageError(f"declared evidence authority is invalid: {name}")
    assertions = tuple(_require_strings(compiled.get("public_assertion_ids"), "public assertions"))
    if tuple(receipts["evidence/real-case.json"].get("assertion_ids", ())) != assertions:
        raise TeamPackageError("real-case evidence does not match compiled assertions")


def _bind_referenced_evidence(
    artifact_store: FilesystemCreationArtifactStore,
    files: Mapping[PurePosixPath, bytes],
    *,
    hermes_usage_receipt_ref: ArtifactRef | None,
    codex_build_receipt_ref: ArtifactRef | None,
    build_evidence_refs: tuple[ArtifactRef, ...],
    test_evidence_refs: tuple[ArtifactRef, ...],
) -> None:
    bindings: tuple[tuple[ArtifactRef | None, tuple[PurePosixPath, ...]], ...] = (
        (
            hermes_usage_receipt_ref,
            (PurePosixPath("evidence/hermes-factory-skill-usage-receipt.json"),),
        ),
        (
            codex_build_receipt_ref,
            (PurePosixPath("evidence/codex-build-receipt.json"),),
        ),
    )
    for reference, paths in bindings:
        content = artifact_store.read_bytes(reference)
        if all(content != _required_file(files, path) for path in paths):
            raise TeamPackageError("referenced evidence does not match package bytes")
    evidence_groups = (
        (build_evidence_refs, tuple(path for path in files if str(path).startswith("evidence/build") and path.suffix == ".json")),
        (test_evidence_refs, tuple(path for path in files if str(path).startswith("evidence/real-case") and path.suffix == ".json")),
    )
    for references, paths in evidence_groups:
        if not references:
            continue
        referenced = tuple(artifact_store.read_bytes(reference) for reference in references)
        packaged = tuple(files[path] for path in sorted(paths, key=str))
        if len(referenced) != len(packaged) or sorted(referenced) != sorted(packaged):
            raise TeamPackageError("referenced evidence does not match package bytes")


def _parse_and_compile_without_execution(
    files: Mapping[PurePosixPath, bytes], manifest: Mapping[str, Any]
) -> None:
    declared = (
        *_require_list(manifest.get("source_files"), "source files"),
        *_require_list(manifest.get("test_files"), "test files"),
    )
    for raw_path in declared:
        path = _safe_path(raw_path, "Python file")
        content = _required_file(files, path)
        try:
            source = content.decode("utf-8")
            tree = ast.parse(source, filename=str(path), mode="exec")
            compile(tree, str(path), "exec", dont_inherit=True, optimize=0)
        except (UnicodeDecodeError, SyntaxError, ValueError, MemoryError, RecursionError) as exc:
            raise TeamPackageError("declared Python source failed safe compilation") from exc


def _read_safe_zip(content: bytes, *, max_files: int) -> dict[PurePosixPath, bytes]:
    if len(content) > _MAX_ARCHIVE_BYTES:
        raise TeamPackageError("source archive compressed size limit exceeded")
    result: dict[PurePosixPath, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > min(max_files, _MAX_DECLARED_FILES):
                raise TeamPackageError("source archive file limit exceeded")
            total_size = 0
            normalized_names: set[str] = set()
            for info in infos:
                normalized_key = unicodedata.normalize("NFC", info.filename).casefold()
                if normalized_key in normalized_names:
                    raise TeamPackageError("source archive path collision is unsafe")
                normalized_names.add(normalized_key)
                path = _safe_path(info.filename, "archive member")
                mode = (info.external_attr >> 16) & 0o170000
                if info.is_dir() or mode == stat.S_IFLNK or info.flag_bits & 0x1 or path in result:
                    raise TeamPackageError("source archive contains an unsafe member")
                if info.file_size > _MAX_MEMBER_BYTES:
                    raise TeamPackageError("source archive member size limit exceeded")
                total_size += info.file_size
                if total_size > _MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise TeamPackageError("source archive total size limit exceeded")
                if info.file_size and info.file_size / max(info.compress_size, 1) > _MAX_COMPRESSION_RATIO:
                    raise TeamPackageError("source archive compression ratio limit exceeded")
            for info in infos:
                path = _safe_path(info.filename, "archive member")
                with archive.open(info, "r") as handle:
                    chunks: list[bytes] = []
                    read_size = 0
                    while True:
                        chunk = handle.read(min(64 * 1024, _MAX_MEMBER_BYTES + 1 - read_size))
                        if not chunk:
                            break
                        read_size += len(chunk)
                        if read_size > _MAX_MEMBER_BYTES:
                            raise TeamPackageError("source archive member size limit exceeded")
                        chunks.append(chunk)
                if read_size != info.file_size:
                    raise TeamPackageError("source archive member size changed during read")
                result[path] = b"".join(chunks)
    except TeamPackageError:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise TeamPackageError("source archive is invalid") from exc
    if not result:
        raise TeamPackageError("source archive is empty")
    return result


def _json_object(content: bytes, label: str) -> dict[str, Any]:
    if len(content) > _MAX_MANIFEST_BYTES:
        raise TeamPackageError(f"{label} size limit exceeded")
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TeamPackageError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise TeamPackageError(f"{label} must be a JSON object")
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode() + b"\n"
    except (TypeError, ValueError) as exc:
        raise TeamPackageError("package JSON is not canonicalizable") from exc


def _safe_path(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or re.match(r"^[A-Za-z]:", value):
        raise TeamPackageError(f"{label} path is unsafe")
    if unicodedata.normalize("NFC", value) != value:
        raise TeamPackageError(f"{label} path is not Unicode-normalized")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise TeamPackageError(f"{label} path is unsafe")
    for part in path.parts:
        if part.endswith((".", " ")) or ":" in part:
            raise TeamPackageError(f"{label} path contains a Windows-unsafe name")
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED:
            raise TeamPackageError(f"{label} path contains a Windows reserved name")
    return path


def _required_file(files: Mapping[PurePosixPath, bytes], path: PurePosixPath) -> bytes:
    try:
        return files[path]
    except KeyError as exc:
        raise TeamPackageError(f"required package file is missing: {path}") from exc


def _require_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise TeamPackageError(f"{label} must be a list")
    return value


def _require_strings(value: object, label: str) -> tuple[str, ...]:
    values = _require_list(value, label)
    if any(not isinstance(item, str) or not item for item in values):
        raise TeamPackageError(f"{label} must contain nonblank strings")
    return tuple(values)


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TeamPackageError(f"{label} must be a nonblank string")
    return value


def _handoff(value: object) -> AgentHandoffV1:
    try:
        return AgentHandoffV1.model_validate(value)
    except Exception as exc:
        raise TeamPackageError("team handoff is invalid") from exc


__all__ = [
    "SealedTeamPackage",
    "TeamPackageError",
    "ValidatedTeamPackage",
    "require_exact_topology",
    "seal_team_package",
    "validate_team_package",
    "verify_sealed_team_package",
]
