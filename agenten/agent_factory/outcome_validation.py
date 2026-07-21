"""Captain-owned independent validation for sealed capability packages."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
import signal
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Literal, Protocol
import zipfile

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agenten.agent_factory.contracts import AgentFactoryJobV2
from agenten.agent_factory.forge_contracts import CreationResultV1
from agenten.agent_factory.outcome_contracts import (
    CapabilityPackageManifestV1,
    PackageArtifact,
)
from agenten.agent_runtime.contracts import ArtifactRef


class ReadOnlyCapabilityContentStore(Protocol):
    async def read(self, reference: ArtifactRef) -> bytes: ...


class CapabilityPackageValidationError(ValueError):
    """The candidate package failed Captain's independent validation."""


class _TeamManifestV1(BaseModel):
    """Small executable allowlist sealed inside the capability archive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["autogen-team.v1"] = Field(alias="schema")
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    autogen_modules: tuple[str, ...] = Field(min_length=1)
    test_paths: tuple[str, ...] = Field(min_length=1)

    @field_validator("autogen_modules", "test_paths")
    @classmethod
    def require_unique_safe_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("team manifest paths must be unique")
        for item in value:
            _require_safe_archive_path(item)
        return value


class CapabilityPackageValidator:
    """Validate untrusted Forge output using only Captain-owned references."""

    def __init__(
        self,
        *,
        content_store: ReadOnlyCapabilityContentStore,
        command_timeout_seconds: int = 60,
    ) -> None:
        if not 1 <= command_timeout_seconds <= 300:
            raise ValueError("command timeout must be between 1 and 300 seconds")
        self._content_store = content_store
        self._command_timeout_seconds = command_timeout_seconds

    async def validate(
        self,
        *,
        job: AgentFactoryJobV2,
        creation_result: CreationResultV1,
        package: CapabilityPackageManifestV1,
    ) -> CapabilityPackageManifestV1:
        self._validate_authority(job, creation_result, package)
        references = _collect_references(creation_result, package)
        content = await self._read_verified(references)
        self._validate_sealed_manifest(creation_result, package, content)
        archive_content = content[package.source_ref.uri]
        await asyncio.to_thread(
            self._validate_archive,
            job,
            package,
            archive_content,
            content,
        )
        return package

    @staticmethod
    def _validate_authority(
        job: AgentFactoryJobV2,
        creation_result: CreationResultV1,
        package: CapabilityPackageManifestV1,
    ) -> None:
        if creation_result.status != "succeeded":
            raise CapabilityPackageValidationError(
                "Forge result is not a successful candidate"
            )
        if package.factory_job_id != job.job_id:
            raise CapabilityPackageValidationError(
                "package factory job does not match Captain job"
            )
        if package.correlation_id != job.correlation_id:
            raise CapabilityPackageValidationError(
                "package correlation does not match Captain job"
            )
        if package.subject_version != job.subject_version:
            raise CapabilityPackageValidationError(
                "package subject version does not match Captain job"
            )
        if package.capability_id != job.required_capability:
            raise CapabilityPackageValidationError(
                "package capability does not match Captain job"
            )
        if creation_result.creation_job_id != package.creation_job_id:
            raise CapabilityPackageValidationError(
                "creation job does not match package"
            )
        if creation_result.correlation_id != job.correlation_id:
            raise CapabilityPackageValidationError(
                "creation correlation does not match Captain job"
            )
        if creation_result.subject_version != job.subject_version:
            raise CapabilityPackageValidationError(
                "creation subject version does not match Captain job"
            )
        if creation_result.package_manifest_ref is None:
            raise CapabilityPackageValidationError(
                "creation result is missing its package manifest"
            )
        if not _contains_reference(creation_result.artifact_refs, package.source_ref):
            raise CapabilityPackageValidationError(
                "creation result does not bind the package source archive"
            )
        if creation_result.skill_usage_receipt_ref is None or not _same_reference(
            creation_result.skill_usage_receipt_ref,
            package.skill_usage_receipt_ref,
        ):
            raise CapabilityPackageValidationError(
                "creation result does not bind the skill usage receipt"
            )

        expected_assertions = set(job.acceptance_assertion_ids)
        actual_assertions = {item.assertion_id for item in package.assertion_outcomes}
        if actual_assertions != expected_assertions or any(
            item.status != "passed" for item in package.assertion_outcomes
        ):
            raise CapabilityPackageValidationError(
                "package does not prove exactly the Captain acceptance assertions"
            )
        expected_holdouts = {item.holdout_id for item in job.private_holdout_refs}
        actual_holdouts = {item.holdout_id for item in package.private_holdout_receipts}
        if actual_holdouts != expected_holdouts or any(
            item.status != "passed" for item in package.private_holdout_receipts
        ):
            raise CapabilityPackageValidationError(
                "package holdout receipts do not match Captain private holdout refs"
            )
        if package.recovery_receipt.assertion_id not in expected_assertions:
            raise CapabilityPackageValidationError(
                "recovery receipt does not match a Captain acceptance assertion"
            )
        if not _contains_reference(
            package.release_evidence_refs,
            package.recovery_receipt.evidence_ref,
        ):
            raise CapabilityPackageValidationError(
                "release evidence does not bind the controlled recovery receipt"
            )
        if any(marker.severity == "required" and marker.status == "unresolved" for marker in package.tool_gaps):
            raise CapabilityPackageValidationError(
                "package contains an unresolved required tool gap"
            )
        creation_gaps = {
            (gap.gap_id, gap.severity, gap.status, _reference_identity(gap.evidence_ref))
            for gap in creation_result.tool_gaps
        }
        package_gaps = {
            (gap.gap_id, gap.severity, gap.status, _reference_identity(gap.evidence_ref))
            for gap in package.tool_gaps
        }
        if creation_gaps != package_gaps:
            raise CapabilityPackageValidationError(
                "package tool gaps do not match the creation result"
            )

    async def _read_verified(
        self,
        references: tuple[ArtifactRef, ...],
    ) -> dict[str, bytes]:
        content: dict[str, bytes] = {}
        for reference in references:
            try:
                resolved = await self._content_store.read(reference)
            except (KeyError, FileNotFoundError, OSError) as exc:
                raise CapabilityPackageValidationError(
                    "content store could not resolve a referenced artifact"
                ) from exc
            if not isinstance(resolved, bytes):
                raise CapabilityPackageValidationError(
                    "content store returned non-byte artifact content"
                )
            if hashlib.sha256(resolved).hexdigest() != reference.sha256:
                raise CapabilityPackageValidationError(
                    "artifact content digest does not match its reference"
                )
            content[reference.uri] = resolved
        return content

    @staticmethod
    def _validate_sealed_manifest(
        creation_result: CreationResultV1,
        package: CapabilityPackageManifestV1,
        content: dict[str, bytes],
    ) -> None:
        reference = creation_result.package_manifest_ref
        if reference is None:  # guarded above; keeps type narrowing local
            raise CapabilityPackageValidationError("creation result has no manifest")
        try:
            sealed = CapabilityPackageManifestV1.model_validate_json(
                content[reference.uri]
            )
        except (ValidationError, ValueError) as exc:
            raise CapabilityPackageValidationError(
                "sealed package manifest is invalid"
            ) from exc
        if sealed != package:
            raise CapabilityPackageValidationError(
                "sealed manifest does not describe the supplied package"
            )

    def _validate_archive(
        self,
        job: AgentFactoryJobV2,
        package: CapabilityPackageManifestV1,
        archive_content: bytes,
        resolved_content: dict[str, bytes],
    ) -> None:
        if package.source_ref.media_type != "application/zip":
            raise CapabilityPackageValidationError(
                "package source archive must use application/zip"
            )
        try:
            with TemporaryDirectory(prefix="captain-capability-validation-") as temporary:
                workspace = Path(temporary) / "package"
                workspace.mkdir()
                self._extract_verified(
                    package,
                    archive_content,
                    workspace,
                    resolved_content,
                )
                team_manifest = self._validate_manifest_relationships(
                    package,
                    workspace,
                )
                self._run_package_checks(job, team_manifest, workspace)
        except zipfile.BadZipFile as exc:
            raise CapabilityPackageValidationError(
                "package source is not a valid ZIP archive"
            ) from exc

    @staticmethod
    def _extract_verified(
        package: CapabilityPackageManifestV1,
        archive_content: bytes,
        workspace: Path,
        resolved_content: dict[str, bytes],
    ) -> None:
        declared = {item.path: item for item in package.artifacts}
        seen: set[str] = set()
        total_size = 0
        with zipfile.ZipFile(io.BytesIO(archive_content)) as archive:
            entries = archive.infolist()
            for entry in entries:
                path = _require_safe_archive_path(entry.filename)
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise CapabilityPackageValidationError(
                        "package archive contains a symlink"
                    )
                if entry.flag_bits & 0x1:
                    raise CapabilityPackageValidationError(
                        "package archive contains an encrypted member"
                    )
                if entry.is_dir():
                    continue
                normalized = path.as_posix()
                if normalized in seen:
                    raise CapabilityPackageValidationError(
                        "package archive contains duplicate paths"
                    )
                seen.add(normalized)
                total_size += entry.file_size
                if entry.file_size > 8 * 1024 * 1024 or total_size > 64 * 1024 * 1024:
                    raise CapabilityPackageValidationError(
                        "package archive exceeds the validation size limit"
                    )
                artifact = declared.get(normalized)
                if artifact is None:
                    raise CapabilityPackageValidationError(
                        "package archive does not match declared artifacts"
                    )
                data = archive.read(entry)
                if hashlib.sha256(data).hexdigest() != artifact.reference.sha256:
                    raise CapabilityPackageValidationError(
                        "archive artifact digest does not match its reference"
                    )
                if resolved_content[artifact.reference.uri] != data:
                    raise CapabilityPackageValidationError(
                        "archive artifact differs from content-store bytes"
                    )
                destination = workspace.joinpath(*path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        if seen != set(declared):
            raise CapabilityPackageValidationError(
                "package archive does not match declared artifacts"
            )

    @staticmethod
    def _validate_manifest_relationships(
        package: CapabilityPackageManifestV1,
        workspace: Path,
    ) -> _TeamManifestV1:
        for artifact in package.artifacts:
            _require_kind_path_relationship(artifact)
        try:
            manifest = _TeamManifestV1.model_validate_json(
                (workspace / "team-manifest.json").read_bytes()
            )
        except (OSError, ValidationError, ValueError) as exc:
            raise CapabilityPackageValidationError(
                "team manifest schema is invalid"
            ) from exc
        if manifest.capability_id != package.capability_id:
            raise CapabilityPackageValidationError(
                "team manifest capability does not match package"
            )
        if manifest.capability_version != package.capability_version:
            raise CapabilityPackageValidationError(
                "team manifest capability version does not match package"
            )
        autogen_paths = tuple(
            item.path for item in package.artifacts if item.kind == "autogen_source"
        )
        if set(manifest.autogen_modules) != set(autogen_paths):
            raise CapabilityPackageValidationError(
                "team manifest AutoGen modules do not match sealed sources"
            )
        test_paths = tuple(
            item.path for item in package.artifacts if item.kind == "test"
        )
        if set(manifest.test_paths) != set(test_paths):
            raise CapabilityPackageValidationError(
                "team manifest allowlisted test paths do not match sealed tests"
            )
        return manifest

    def _run_package_checks(
        self,
        job: AgentFactoryJobV2,
        manifest: _TeamManifestV1,
        workspace: Path,
    ) -> None:
        compile_result = _run_isolated(
            (sys.executable, "-I", "-m", "compileall", "-q", "autogen"),
            workspace,
            str(job.correlation_id),
            self._command_timeout_seconds,
        )
        if compile_result != 0:
            raise CapabilityPackageValidationError("AutoGen compilation failed")

        import_script = (
            "import importlib.util,json,pathlib,sys;"
            "root=pathlib.Path.cwd();"
            "paths=json.loads(sys.argv[1]);"
            "[(lambda spec: (sys.modules.__setitem__(spec.name, "
            "importlib.util.module_from_spec(spec)), "
            "spec.loader.exec_module(sys.modules[spec.name])))("
            "importlib.util.spec_from_file_location(f'_captain_autogen_{index}', "
            "root/path)) for index,path in enumerate(paths)]"
        )
        import_result = _run_isolated(
            (
                sys.executable,
                "-I",
                "-c",
                import_script,
                json.dumps(manifest.autogen_modules),
            ),
            workspace,
            str(job.correlation_id),
            self._command_timeout_seconds,
        )
        if import_result != 0:
            raise CapabilityPackageValidationError("AutoGen import failed")

        test_script = (
            "import json,pathlib,pytest,sys;"
            "sys.path.insert(0,str(pathlib.Path.cwd()));"
            "raise SystemExit(pytest.main(['-q',*json.loads(sys.argv[1])]))"
        )
        test_result = _run_isolated(
            (
                sys.executable,
                "-I",
                "-c",
                test_script,
                json.dumps(manifest.test_paths),
            ),
            workspace,
            str(job.correlation_id),
            self._command_timeout_seconds,
        )
        if test_result != 0:
            raise CapabilityPackageValidationError("allowlisted package tests failed")


def _collect_references(
    creation_result: CreationResultV1,
    package: CapabilityPackageManifestV1,
) -> tuple[ArtifactRef, ...]:
    raw_references: list[object | None] = [
        creation_result.package_manifest_ref,
        *creation_result.artifact_refs,
        *creation_result.evidence_refs,
        creation_result.skill_usage_receipt_ref,
        creation_result.private_skill_candidate_ref,
        *(gap.evidence_ref for gap in creation_result.tool_gaps),
        package.source_ref,
        package.team_manifest_ref,
        *(item.reference for item in package.artifacts),
        *(ref for item in package.assertion_outcomes for ref in item.evidence_refs),
        *(item.evidence_ref for item in package.private_holdout_receipts),
        package.recovery_receipt.evidence_ref,
        *package.release_evidence_refs,
        package.skill_usage_receipt_ref,
        *(gap.input_contract_ref for gap in package.tool_gaps),
        *(gap.output_contract_ref for gap in package.tool_gaps),
        *(gap.evidence_ref for gap in package.tool_gaps),
        package.runbook_ref,
    ]
    by_uri: dict[str, ArtifactRef] = {}
    for value in raw_references:
        if value is None:
            continue
        reference = ArtifactRef.model_validate(
            {
                "uri": getattr(value, "uri"),
                "sha256": getattr(value, "sha256"),
                "media_type": getattr(value, "media_type"),
            }
        )
        existing = by_uri.get(reference.uri)
        if existing is not None and existing != reference:
            raise CapabilityPackageValidationError(
                "artifact URI is bound to conflicting digests or media types"
            )
        by_uri[reference.uri] = reference
    return tuple(by_uri.values())


def _same_reference(left: object, right: object) -> bool:
    return _reference_identity(left) == _reference_identity(right)


def _reference_identity(reference: object) -> tuple[str, str, str]:
    return (
        str(getattr(reference, "uri")),
        str(getattr(reference, "sha256")),
        str(getattr(reference, "media_type")),
    )


def _contains_reference(references: Iterable[object], expected: object) -> bool:
    return any(_same_reference(reference, expected) for reference in references)


def _require_safe_archive_path(value: str) -> PurePosixPath:
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or (len(value) >= 2 and value[0].isalpha() and value[1] == ":")
    ):
        raise CapabilityPackageValidationError(
            "package archive contains an unsafe path"
        )
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise CapabilityPackageValidationError(
            "package archive contains an unsafe path"
        )
    return path


def _require_kind_path_relationship(artifact: PackageArtifact) -> None:
    path = artifact.path
    valid = {
        "team_manifest": path == "team-manifest.json",
        "autogen_source": path.startswith("autogen/") and path.endswith(".py"),
        "n8n_workflow": path.startswith("n8n/") and path.endswith(".json"),
        "local_adapter": path.startswith("adapters/") and path.endswith(".py"),
        "skill": path.startswith("skills/"),
        "test": path.startswith("tests/test_") and path.endswith(".py"),
        "evidence": path.startswith("evidence/"),
        "runbook": path == "RUNBOOK.md",
    }[artifact.kind]
    if not valid:
        raise CapabilityPackageValidationError(
            "package artifact kind does not match its logical path"
        )


def _run_isolated(
    command: tuple[str, ...],
    workspace: Path,
    correlation_id: str,
    timeout_seconds: int,
) -> int:
    environment_names = ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATHEXT")
    environment = {
        name: value
        for name in environment_names
        if (value := os.environ.get(name)) is not None
    }
    environment.update(
        {
            "CAPTAIN_CORRELATION_ID": correlation_id,
            "CAPTAIN_PACKAGE_VALIDATION": "1",
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
    )
    options: dict[str, object]
    if os.name == "nt":
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        options = {"start_new_session": True}
    process = subprocess.Popen(
        command,
        cwd=workspace,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **options,
    )
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        raise CapabilityPackageValidationError(
            "package validation command timed out"
        ) from exc


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        subprocess.run(
            ("taskkill.exe", "/PID", str(process.pid), "/T", "/F"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
