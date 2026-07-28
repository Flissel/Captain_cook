"""Reproducible, public-safe seed candidates for the business benchmark.

The source directories contain reusable prompts and workflow contracts only.
Private benchmark cases and expected labels are never inputs to this packager.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Literal
import zipfile

from agenten.agent_factory.candidate_evaluation import (
    FactoryCandidateArtifact,
    FactoryCandidateManifest,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_runtime.contracts import ArtifactRef


CLAIMS_SEED_PROFILE = "insurance_claims_resolution_swarm"
RENEWAL_SEED_PROFILE = "customer_renewal_orchestration_team"
SeedProfile = Literal[
    "insurance_claims_resolution_swarm",
    "customer_renewal_orchestration_team",
]
_EXAMPLE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "business_benchmark_candidates"
_FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def package_business_benchmark_seed(
    profile_id: str,
    destination: Path,
) -> ResolvedFactoryCandidate:
    """Seal one known seed directory into a byte-reproducible candidate archive."""

    if profile_id not in {CLAIMS_SEED_PROFILE, RENEWAL_SEED_PROFILE}:
        raise ValueError("unknown business benchmark seed profile")
    source = _EXAMPLE_ROOT / profile_id
    config = _read_json(source / "seed.json")
    candidate_id = _required_string(config, "candidate_id")
    agents = _build_agents(source, candidate_id, config)
    tool = _build_tool(source, candidate_id, config)
    team_payload = {
        "schema": "autogen-team.v1",
        "name": profile_id,
        "conversation_pattern": _required_string(config, "conversation_pattern"),
        "agents": agents,
        "memory_policy": "buffered",
        "max_messages": _required_integer(config, "max_messages"),
        "max_handoffs": _required_integer(config, "max_handoffs"),
        "max_tool_calls": _required_integer(config, "max_tool_calls"),
        "termination_conditions": config["termination_conditions"],
        "entrypoint_command": ["python", "-m", "compileall", "-q", "."],
    }
    archive_files = _source_files(source)
    archive_files.pop("seed.json")
    archive_files["team_manifest.json"] = _canonical_json(team_payload)

    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination / f"{candidate_id}.zip"
    _write_reproducible_zip(archive_path, archive_files)

    team_bytes = archive_files["team_manifest.json"]
    workflow_path = _safe_relative_path(_required_string(config, "workflow_path"))
    input_schema_path = _safe_relative_path(_required_string(config, "input_schema_path"))
    output_schema_path = _safe_relative_path(_required_string(config, "output_schema_path"))
    workflow = _artifact(candidate_id, "workflow", archive_files[workflow_path], "application/json")
    input_schema = _artifact(candidate_id, "tool-input", archive_files[input_schema_path], "application/schema+json")
    output_schema = _artifact(candidate_id, "tool-output", archive_files[output_schema_path], "application/schema+json")
    if tool.input_schema_ref != input_schema.uri or tool.output_schema_ref != output_schema.uri:
        raise ValueError("seed tool schema references were not content bound")
    source_bytes = archive_path.read_bytes()
    manifest = FactoryCandidateManifest(
        candidate_id=candidate_id,
        source_archive_ref=_artifact(candidate_id, "source", source_bytes, "application/zip"),
        team_manifest=FactoryCandidateArtifact(
            reference=_artifact(candidate_id, "team", team_bytes, "application/json"),
            relative_path="team_manifest.json",
        ),
        workflow_artifacts=(
            FactoryCandidateArtifact(reference=workflow, relative_path=workflow_path),
        ),
        tool_schema_artifacts=(
            FactoryCandidateArtifact(reference=input_schema, relative_path=input_schema_path),
            FactoryCandidateArtifact(reference=output_schema, relative_path=output_schema_path),
        ),
        n8n_tools=(tool,),
        n8n_tool_references=(tool.opaque_reference(),),
        build_command=("python", "-m", "compileall", "-q", "."),
        real_case_command=("python", "-m", "compileall", "-q", "."),
        timeout_seconds=30,
    )
    return ResolvedFactoryCandidate(candidate=manifest, source_archive=archive_path)


def _build_agents(
    source: Path,
    candidate_id: str,
    config: dict[str, object],
) -> list[dict[str, object]]:
    configured = config.get("agents")
    if not isinstance(configured, list) or not configured:
        raise ValueError("seed agents must be a non-empty list")
    agents: list[dict[str, object]] = []
    for raw in configured:
        if not isinstance(raw, dict):
            raise ValueError("seed agent must be an object")
        prompt_path = _safe_relative_path(_required_string(raw, "prompt_path"))
        prompt = (source / prompt_path).read_bytes()
        agents.append(
            {
                "name": _required_string(raw, "name"),
                "tools": raw.get("tools", []),
                "system_prompt_ref": _artifact(
                    candidate_id,
                    f"prompt-{_required_string(raw, 'name')}",
                    prompt,
                    "text/plain",
                ).model_dump(mode="json"),
                "handoffs": raw.get("handoffs", []),
            }
        )
    return agents


def _build_tool(
    source: Path,
    candidate_id: str,
    config: dict[str, object],
) -> TypedN8nTool:
    raw = config.get("tool")
    if not isinstance(raw, dict):
        raise ValueError("seed requires exactly one typed tool descriptor")
    input_path = _safe_relative_path(_required_string(config, "input_schema_path"))
    output_path = _safe_relative_path(_required_string(config, "output_schema_path"))
    input_ref = _artifact(
        candidate_id,
        "tool-input",
        (source / input_path).read_bytes(),
        "application/schema+json",
    )
    output_ref = _artifact(
        candidate_id,
        "tool-output",
        (source / output_path).read_bytes(),
        "application/schema+json",
    )
    return TypedN8nTool(
        name=_required_string(raw, "name"),
        description=_required_string(raw, "description"),
        input_schema_ref=input_ref.uri,
        output_schema_ref=output_ref.uri,
    )


def _source_files(source: Path) -> dict[str, bytes]:
    if not source.is_dir():
        raise FileNotFoundError("business benchmark seed directory is missing")
    return {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in sorted(source.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    }


def _write_reproducible_zip(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for relative_path in sorted(files):
            info = zipfile.ZipInfo(relative_path, date_time=_FIXED_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, files[relative_path])


def _artifact(
    candidate_id: str,
    kind: str,
    content: bytes,
    media_type: str,
) -> ArtifactRef:
    digest = hashlib.sha256(content).hexdigest()
    return ArtifactRef(
        uri=f"artifact://factory-seed/{candidate_id}/{kind}/{digest}",
        sha256=digest,
        media_type=media_type,
    )


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("seed configuration must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("seed configuration must be an object")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_relative_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("seed path must be a safe relative path")
    return path.as_posix()


def _required_string(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"seed {key} must be a non-empty string")
    return item


def _required_integer(value: dict[str, object], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"seed {key} must be an integer")
    return item


__all__ = [
    "CLAIMS_SEED_PROFILE",
    "RENEWAL_SEED_PROFILE",
    "package_business_benchmark_seed",
]
