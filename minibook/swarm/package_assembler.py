"""Deterministic, fail-closed assembly of private capability candidates."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml


class PackageAssemblyError(RuntimeError):
    pass


class LegacyPackageContractGap(PackageAssemblyError):
    """Exact Package-C outputs a legacy run did not actually produce."""

    def __init__(self, required_outputs: tuple[str, ...]) -> None:
        self.gap_id = "legacy-swarm-package-c-export"
        self.required_outputs = required_outputs
        super().__init__(
            "TODO_TOOL.v1 required capability=legacy_swarm_package_c_export; "
            "required_outputs=" + "; ".join(required_outputs)
        )


@dataclass(frozen=True)
class AssembledArtifact:
    path: str
    kind: str
    uri: str
    sha256: str
    media_type: str
    size: int


@dataclass(frozen=True)
class AssembledPackage:
    archive_path: Path
    archive_sha256: str
    manifest_sha256: str
    artifacts: tuple[AssembledArtifact, ...] = ()
    candidate_descriptor_sha256: str | None = None


_EXCLUDED_NAMES = {".env", ".git", ".hg", "__pycache__", ".pytest_cache"}
_EXCLUDED_SUFFIXES = {".log", ".pyc", ".pyo"}
_INTEGRATION_FIELDS = {
    "workflow", "input_schema", "output_schema", "idempotency", "timeout",
    "retry", "duplicate", "failure", "compensation",
}
_PATTERNS = {
    "swarm": "swarm",
    "selector": "selector_group_chat",
    "selector_group_chat": "selector_group_chat",
    "round_robin": "round_robin_group_chat",
    "round_robin_group_chat": "round_robin_group_chat",
    "single_agent": "single_agent",
}
_VALIDATION_SECRET_ENV = re.compile(
    r"(?:key|token|secret|password|credential|auth|dsn|connection)", re.I
)


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or "." in path.parts or not path.parts:
        raise PackageAssemblyError("package path must be safe and relative")
    return path


class PackageAssembler:
    def materialize_legacy_export(
        self,
        source: Path,
        destination: Path,
        *,
        capability_id: str,
        capability_version: int,
        pipeline_results: Mapping[str, object],
        hermes_skill_usage_receipt: bytes | None,
        hermes_tool_gaps: bytes | None = None,
        released_skill: tuple[str, bytes] | None = None,
    ) -> Path:
        """Repackage only observed legacy/Hermes bytes into Package C.

        This method deliberately does not manufacture tests, empty tool-gap
        declarations, or a passing Hermes receipt.  Missing evidence becomes a
        typed gap that the caller can persist as ``TODO_TOOL.v1``.
        """

        source = source.resolve()
        missing: list[str] = []
        runbook = next(
            (
                source / name
                for name in ("RUNBOOK.md", "SETUP.md", "README.md")
                if (source / name).is_file()
            ),
            None,
        )
        if runbook is None:
            missing.append("RUNBOOK.md (from real RUNBOOK.md, SETUP.md, or README.md)")
        receipt_path = source / "evidence/hermes-skill-usage-receipt.json"
        if hermes_skill_usage_receipt is None and not receipt_path.is_file():
            missing.append("evidence/hermes-skill-usage-receipt.json (from Hermes)")
        gaps_path = source / "evidence/tool-gaps.json"
        if hermes_tool_gaps is None and not gaps_path.is_file():
            missing.append("evidence/tool-gaps.json (from Hermes ToolIntegrator)")
        required_results = (
            ("build", "pipeline build_result"),
            ("output_evaluation", "pipeline output_eval"),
            ("run", "pipeline run_result"),
        )
        for field, label in required_results:
            if not isinstance(pipeline_results.get(field), Mapping):
                missing.append(label)
        has_skill_files = (source / "skills").is_dir() and any(
            path.is_file() for path in (source / "skills").rglob("*")
        )
        if not has_skill_files and released_skill is None:
            missing.append("skills/ (released or Hermes-created skill bytes)")
        has_tests = (source / "tests").is_dir() and any(
            path.is_file() and path.name.startswith("test_") and path.suffix == ".py"
            for path in (source / "tests").rglob("*")
        )
        if not has_tests:
            missing.append("tests/ (real executable tests)")
        source_modules = (
            tuple(path for path in sorted((source / "src").rglob("*.py")) if path.is_file())
            if (source / "src").is_dir()
            else ()
        )
        if not source_modules or not (source / "src/main.py").is_file():
            missing.append("autogen/ (from real legacy src/*.py including src/main.py)")
        if missing:
            raise LegacyPackageContractGap(tuple(sorted(missing)))

        with tempfile.TemporaryDirectory(prefix="minibook-legacy-package-") as temporary:
            temporary_root = Path(temporary)
            staged = temporary_root / "candidate"
            staged.mkdir()
            for path in source_modules:
                relative = path.relative_to(source / "src")
                target = staged / "autogen" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            self._copy_observed_tree(source / "tests", staged / "tests")
            if has_skill_files:
                self._copy_observed_tree(source / "skills", staged / "skills")
            else:
                assert released_skill is not None
                skill_id, skill_bytes = released_skill
                skill_target = staged / "skills" / _identifier(skill_id) / "SKILL.md"
                skill_target.parent.mkdir(parents=True, exist_ok=True)
                skill_target.write_bytes(skill_bytes)
            assert runbook is not None
            shutil.copy2(runbook, staged / "RUNBOOK.md")
            evidence = staged / "evidence"
            evidence.mkdir()
            receipt = (
                hermes_skill_usage_receipt
                if hermes_skill_usage_receipt is not None
                else receipt_path.read_bytes()
            )
            gaps = hermes_tool_gaps if hermes_tool_gaps is not None else gaps_path.read_bytes()
            (evidence / "hermes-skill-usage-receipt.json").write_bytes(receipt)
            (evidence / "tool-gaps.json").write_bytes(gaps)
            observed_results = {
                "schema": "minibook.legacy-swarm-pipeline-results.v1",
                **{
                    name: self._pipeline_result_summary(pipeline_results[name])
                    for name, _label in required_results
                },
            }
            (evidence / "legacy-pipeline-results.json").write_bytes(
                _canonical_json(observed_results)
            )
            for optional_root in ("n8n", "adapters"):
                candidate = source / optional_root
                if candidate.is_dir():
                    self._copy_observed_tree(candidate, staged / optional_root)
            for metadata in ("agents",):
                candidate = source / metadata
                if candidate.is_dir():
                    self._copy_observed_tree(candidate, staged / metadata)
            if (source / "project.yml").is_file():
                shutil.copy2(source / "project.yml", staged / "project.yml")

            archive_path = temporary_root / "candidate.zip"
            assembled = self.assemble(
                staged,
                archive_path,
                startup_command=("python", "autogen/main.py"),
                capability_id=capability_id,
                capability_version=capability_version,
            )
            with zipfile.ZipFile(assembled.archive_path) as archive:
                expected = {
                    info.filename: archive.read(info)
                    for info in archive.infolist()
                    if not info.is_dir()
                }
            self._materialize_immutable(destination.resolve(), expected)
        return destination.resolve()

    @staticmethod
    def _copy_observed_tree(source: Path, destination: Path) -> None:
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise PackageAssemblyError("legacy export contains a symbolic link")
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

    @staticmethod
    def _pipeline_result_summary(value: object) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise PackageAssemblyError("legacy pipeline result is not structured")
        allowed = {
            "status",
            "duration",
            "score",
            "docker_down",
            "total_chars",
            "eval_mode",
        }
        summary = {str(key): item for key, item in value.items() if key in allowed}
        if summary.get("status") != "PASS":
            raise PackageAssemblyError("legacy pipeline result is not an observed pass")
        try:
            _canonical_json(summary)
        except (TypeError, ValueError) as exc:
            raise PackageAssemblyError("legacy pipeline result is not JSON-safe") from exc
        return summary

    @staticmethod
    def _materialize_immutable(destination: Path, files: Mapping[str, bytes]) -> None:
        if destination.exists():
            existing = {
                path.relative_to(destination).as_posix(): path.read_bytes()
                for path in destination.rglob("*")
                if path.is_file()
            }
            if existing != dict(files):
                raise PackageAssemblyError("legacy Package-C export already differs")
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(
                prefix=destination.name + ".",
                dir=destination.parent,
            )
        )
        try:
            for name, content in sorted(files.items()):
                relative = _safe_relative(name)
                target = temporary / Path(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            temporary.replace(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def assemble(
        self,
        source: Path,
        archive_path: Path,
        *,
        startup_command: tuple[str, ...],
        integration_contracts: tuple[dict[str, object], ...] = (),
        capability_id: str | None = None,
        capability_version: int = 1,
    ) -> AssembledPackage:
        source = source.resolve()
        if not source.is_dir():
            raise PackageAssemblyError("candidate source is not a directory")
        self._validate_startup(source, startup_command)
        contracts = self._validate_integrations(source, integration_contracts)
        files, directories = self._source_files(source)
        if any(
            path in {
                "adapters/factory-candidate.json",
                "adapters/execution-team.json",
                "evidence/package-index.json",
            }
            or path.startswith("adapters/prompts/")
            for path in files
        ):
            raise PackageAssemblyError(
                "candidate source collides with Captain-generated package metadata"
            )
        descriptor_digest: str | None = None
        if contracts:
            generated = self._candidate_execution_files(
                source,
                files,
                startup_command=startup_command,
                integration_contracts=contracts,
            )
            files.update(generated)
            descriptor_digest = hashlib.sha256(
                generated["adapters/factory-candidate.json"]
            ).hexdigest()
            directories.update({"adapters/", "adapters/prompts/"})
        required = {"autogen/", "skills/", "tests/", "evidence/", "RUNBOOK.md"}
        if contracts:
            required.update({"n8n/", "adapters/"})
        present = directories | set(files)
        if not required.issubset(present):
            raise PackageAssemblyError("candidate package is missing required layout")
        if isinstance(capability_version, bool) or capability_version < 1:
            raise PackageAssemblyError("capability version must be a positive integer")
        base_exported = tuple(
            _artifact(path, content)
            for path, content in sorted(files.items())
        )
        entries = [
            {
                "path": item.path,
                "kind": item.kind,
                "uri": item.uri,
                "sha256": item.sha256,
                "media_type": item.media_type,
                "size": item.size,
            }
            for item in base_exported
        ]
        package_index = {
            "schema": "minibook.package-index.v1",
            "startup_command": list(startup_command),
            "required_layout": sorted(required),
            "files": entries,
            "integrations": list(contracts),
        }
        files["evidence/package-index.json"] = _canonical_json(package_index)
        exported = tuple(
            _artifact(path, content)
            for path, content in sorted(files.items())
        )
        manifest = {
            "schema": "autogen-team.v1",
            "capability_id": _identifier(capability_id or source.name),
            "capability_version": capability_version,
            "autogen_modules": [
                item.path for item in exported if item.kind == "autogen_source"
            ],
            "test_paths": [item.path for item in exported if item.kind == "test"],
        }
        manifest_bytes = _canonical_json(manifest)
        manifest_artifact = _artifact(
            "team-manifest.json", manifest_bytes, kind="team_manifest"
        )
        archive_entries = dict(files)
        archive_entries[manifest_artifact.path] = manifest_bytes
        for directory in directories:
            archive_entries.setdefault(directory, b"")
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(
            archive_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name, content in sorted(archive_entries.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o755 if name.endswith("/") else 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
        archive_bytes = archive_path.read_bytes()
        return AssembledPackage(
            archive_path=archive_path,
            archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
            manifest_sha256=manifest_artifact.sha256,
            artifacts=tuple(sorted((*exported, manifest_artifact), key=lambda item: item.path)),
            candidate_descriptor_sha256=descriptor_digest,
        )

    def _source_files(self, source: Path) -> tuple[dict[str, bytes], set[str]]:
        files: dict[str, bytes] = {}
        directories: set[str] = set()
        for path in sorted(source.rglob("*")):
            parts = path.relative_to(source).parts
            if path.is_symlink():
                raise PackageAssemblyError("candidate packages must not contain symlinks")
            if any(part in _EXCLUDED_NAMES for part in parts):
                continue
            if (parts and parts[0] == "agents") or path.name == "project.yml":
                continue
            relative = path.relative_to(source).as_posix()
            if path.is_dir():
                directories.add(relative.rstrip("/") + "/")
                continue
            if path.suffix.lower() in _EXCLUDED_SUFFIXES or "transcript" in path.name.lower():
                continue
            _safe_relative(relative)
            files[relative] = path.read_bytes()
        return files, directories

    def _validate_integrations(
        self,
        source: Path,
        integration_contracts: tuple[dict[str, object], ...],
    ) -> tuple[dict[str, object], ...]:
        normalized: list[dict[str, object]] = []
        for contract in integration_contracts:
            if set(contract) != _INTEGRATION_FIELDS:
                raise PackageAssemblyError("integration contract is incomplete")
            checked = dict(contract)
            for field in ("workflow", "input_schema", "output_schema"):
                path = _safe_relative(str(checked[field])).as_posix()
                if not (source / Path(*PurePosixPath(path).parts)).is_file():
                    raise PackageAssemblyError(
                        f"integration contract {field} is not an exported file"
                    )
                checked[field] = path
            normalized.append(checked)
        return tuple(normalized)

    def _candidate_execution_files(
        self,
        source: Path,
        files: dict[str, bytes],
        *,
        startup_command: tuple[str, ...],
        integration_contracts: tuple[dict[str, object], ...],
    ) -> dict[str, bytes]:
        agents = self._exported_agents(source)
        tool_records = tuple(
            self._tool_record(contract, files) for contract in integration_contracts
        )
        allowed_tools = {str(item["name"]) for item in tool_records}
        prompts: dict[str, bytes] = {}
        manifest_agents: list[dict[str, object]] = []
        for agent in agents:
            prompt_path = f"adapters/prompts/{agent['name']}.md"
            prompt = (str(agent["system_message"]).rstrip() + "\n").encode("utf-8")
            prompts[prompt_path] = prompt
            tools = tuple(str(item) for item in agent["tools"])
            if set(tools) - allowed_tools:
                raise PackageAssemblyError(
                    "exported agent references a tool outside integration contracts"
                )
            manifest_agents.append(
                {
                    "name": agent["name"],
                    "tools": list(tools),
                    "system_prompt_ref": _reference(prompt, prompt_path),
                    "handoffs": list(agent["handoffs"]),
                }
            )
        names = {str(item["name"]) for item in manifest_agents}
        if any(set(item["handoffs"]) - names for item in manifest_agents):
            raise PackageAssemblyError("exported agent handoff names are not closed")
        pattern = self._conversation_pattern(source, manifest_agents)
        execution_manifest = {
            "schema": "autogen-team.v1",
            "name": _identifier(source.name),
            "conversation_pattern": pattern,
            "agents": manifest_agents,
            "memory_policy": "bounded",
            "max_messages": 40,
            "max_handoffs": 20 if any(item["handoffs"] for item in manifest_agents) else 0,
            "max_tool_calls": max(1, len(tool_records) * 4),
            "termination_conditions": ["task_completed", "max_messages", "max_tool_calls"],
            "entrypoint_command": list(startup_command),
        }
        execution_bytes = _canonical_json(execution_manifest)
        execution_path = "adapters/execution-team.json"
        generated = {**prompts, execution_path: execution_bytes}
        combined = {**files, **generated}
        descriptor = {
            "schema": "captain.factory-candidate-descriptor.v1",
            "candidate_id": _identifier(source.name),
            "team_manifest": _candidate_artifact(execution_path, execution_bytes),
            "workflow_artifacts": [
                _candidate_artifact(str(item["workflow"]), combined[str(item["workflow"])])
                for item in tool_records
            ],
            "tool_schema_artifacts": [
                _candidate_artifact(path, combined[path])
                for item in tool_records
                for path in (str(item["input_schema"]), str(item["output_schema"]))
            ],
            "n8n_tools": [
                {
                    "name": item["name"],
                    "description": item["description"],
                    "input_schema_ref": _reference(
                        combined[str(item["input_schema"])], str(item["input_schema"])
                    )["uri"],
                    "output_schema_ref": _reference(
                        combined[str(item["output_schema"])], str(item["output_schema"])
                    )["uri"],
                }
                for item in tool_records
            ],
            "build_command": ["python", "-m", "compileall", "-q", "autogen"],
            "real_case_command": list(startup_command),
            "timeout_seconds": min(
                300,
                max(1, max(int(item["timeout"]) for item in tool_records)),
            ),
        }
        generated["adapters/factory-candidate.json"] = _canonical_json(descriptor)
        return generated

    def _exported_agents(self, source: Path) -> tuple[dict[str, object], ...]:
        root = source / "agents"
        records: list[dict[str, object]] = []
        for path in sorted(root.glob("*/agent.yml")) if root.is_dir() else ():
            try:
                payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError) as exc:
                raise PackageAssemblyError("exported agent YAML is invalid") from exc
            if not isinstance(payload, dict):
                raise PackageAssemblyError("exported agent YAML must be an object")
            name = _identifier(str(payload.get("name", "")))
            prompt = payload.get("system_message")
            if not isinstance(prompt, str) or not prompt.strip():
                raise PackageAssemblyError("exported agent lacks a system prompt")
            raw_tools = payload.get("domain_tools", payload.get("tools", ()))
            if not isinstance(raw_tools, list) or any(not isinstance(item, str) for item in raw_tools):
                raise PackageAssemblyError("exported agent tools must be named strings")
            raw_handoffs = payload.get("handoffs", ())
            if not isinstance(raw_handoffs, list) or any(
                not isinstance(item, str) for item in raw_handoffs
            ):
                raise PackageAssemblyError("exported agent handoffs must be named strings")
            records.append(
                {
                    "name": name,
                    "system_message": prompt,
                    "tools": tuple(raw_tools),
                    "handoffs": tuple(_identifier(item) for item in raw_handoffs),
                }
            )
        if not records or len({item["name"] for item in records}) != len(records):
            raise PackageAssemblyError("candidate export requires unique agent.yml files")
        return tuple(records)

    def _conversation_pattern(
        self,
        source: Path,
        agents: list[dict[str, object]],
    ) -> str:
        project = source / "project.yml"
        raw_pattern = ""
        if project.is_file():
            try:
                payload = yaml.safe_load(project.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    raw_pattern = str(payload.get("pattern", ""))
            except (OSError, UnicodeError, yaml.YAMLError) as exc:
                raise PackageAssemblyError("exported project YAML is invalid") from exc
        if raw_pattern in _PATTERNS:
            selected = _PATTERNS[raw_pattern]
        elif len(agents) == 1:
            selected = "single_agent"
        elif any(item["handoffs"] for item in agents):
            selected = "swarm"
        else:
            selected = "round_robin_group_chat"
        if selected == "single_agent" and len(agents) != 1:
            raise PackageAssemblyError("single_agent pattern requires exactly one export")
        return selected

    @staticmethod
    def _tool_record(
        contract: dict[str, object],
        files: dict[str, bytes],
    ) -> dict[str, object]:
        paths = tuple(str(contract[field]) for field in ("workflow", "input_schema", "output_schema"))
        if any(path not in files for path in paths):
            raise PackageAssemblyError("integration files were excluded from the package")
        name = _identifier(PurePosixPath(paths[0]).stem)
        return {
            **contract,
            "name": name,
            "description": f"Execute the sealed {name} integration workflow.",
        }

    def _validate_startup(self, source: Path, command: tuple[str, ...]) -> None:
        if len(command) < 2 or command[0] != "python":
            raise PackageAssemblyError("startup executable is not allow-listed")
        entry = _safe_relative(command[1])
        entry_path = source / Path(*entry.parts)
        if not entry_path.is_file() or entry_path.suffix != ".py":
            raise PackageAssemblyError("startup entrypoint is missing or unsupported")
        for path in source.rglob("*.py"):
            if path.is_symlink():
                raise PackageAssemblyError("candidate packages must not contain symlinks")
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except (OSError, SyntaxError, UnicodeError) as exc:
                raise PackageAssemblyError("candidate Python import validation failed") from exc
        with tempfile.TemporaryDirectory(prefix="minibook-candidate-") as temporary:
            workspace = Path(temporary) / "candidate"
            shutil.copytree(source, workspace)
            environment = {
                key: value
                for key, value in os.environ.items()
                if _VALIDATION_SECRET_ENV.search(key) is None
            }
            environment["CAPTAIN_PACKAGE_VALIDATE"] = "1"
            try:
                completed = subprocess.run(
                    [sys.executable, *command[1:]], cwd=workspace, env=environment,
                    capture_output=True, timeout=10, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise PackageAssemblyError("candidate startup validation failed") from exc
            if completed.returncode != 0:
                raise PackageAssemblyError("candidate startup validation failed")


def _identifier(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    if not normalized or not normalized[0].isalpha() or len(normalized) > 64:
        raise PackageAssemblyError("exported identifier is invalid")
    return normalized


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _reference(content: bytes, path: str) -> dict[str, str]:
    digest = hashlib.sha256(content).hexdigest()
    return {
        "uri": f"artifact://capability-factory/package-file/{digest}",
        "sha256": digest,
        "media_type": _media_type(path),
    }


def _candidate_artifact(path: str, content: bytes) -> dict[str, object]:
    return {"reference": _reference(content, path), "relative_path": path}


def _artifact(path: str, content: bytes, *, kind: str | None = None) -> AssembledArtifact:
    resolved_kind = kind or _kind(path)
    reference = _reference(content, path)
    media_type = _media_type(path)
    return AssembledArtifact(
        path=path,
        kind=resolved_kind,
        uri=reference["uri"],
        sha256=reference["sha256"],
        media_type=media_type,
        size=len(content),
    )


def _kind(path: str) -> str:
    if path.startswith("autogen/"):
        return "autogen_source"
    if path.startswith("n8n/"):
        return "n8n_workflow"
    if path.startswith("adapters/"):
        return "local_adapter"
    if path.startswith("skills/"):
        return "skill"
    if path.startswith("tests/"):
        return "test"
    if path.startswith("evidence/"):
        return "evidence"
    if path == "RUNBOOK.md":
        return "runbook"
    raise PackageAssemblyError(f"unsupported package artifact path: {path}")


def _media_type(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    return {
        ".json": "application/json",
        ".py": "text/x-python",
        ".md": "text/markdown",
        ".yaml": "application/yaml",
        ".yml": "application/yaml",
    }.get(suffix, "application/octet-stream")
