"""Captain-owned independent validation for sealed capability packages."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
import stat
from tempfile import TemporaryDirectory
from typing import Literal, Protocol
import unicodedata
from uuid import UUID, uuid4
import zipfile

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agenten.agent_factory.contracts import AgentFactoryJobV2
from agenten.agent_factory.forge_contracts import CreationResultV1
from agenten.agent_factory.outcome_contracts import (
    AssertionOutcome,
    CapabilityPackageManifestV1,
    CapabilityReleaseEvidenceV1,
    canonical_capability_release_evidence_bytes,
    ControlledRecoveryReceipt,
    ForgeCapabilityPackageCandidateV1,
    PackageArtifact,
    PrivateHoldoutReceipt,
)
from agenten.agent_factory.skill_evaluation import HermesSkillUsageReceipt
from agenten.agent_runtime.contracts import ArtifactRef, SHA256_PATTERN


class ReadOnlyCapabilityContentStore(Protocol):
    async def read(self, reference: ArtifactRef) -> bytes: ...


class CapabilityPackageValidationError(ValueError):
    """The candidate package failed Captain's independent validation."""


class CapabilitySandboxRequest(BaseModel):
    """Exact, bounded request accepted only by a trusted external sandbox."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_digest: str = Field(pattern=SHA256_PATTERN)
    execution_id: UUID
    process_identity: str = Field(
        pattern=r"^(?:sandbox-handle|process)://[A-Za-z0-9._:/-]+$"
    )
    correlation_id: UUID
    workspace: Path
    python_path_root: Path
    module_names: tuple[str, ...] = Field(min_length=1)
    test_paths: tuple[str, ...] = Field(min_length=1)
    extracted_tree_sha256: str = Field(pattern=SHA256_PATTERN)
    package_archive_sha256: str = Field(pattern=SHA256_PATTERN)
    timeout_seconds: int = Field(ge=1, le=300, strict=True)
    workspace_access: Literal["read_only"] = "read_only"
    network_access: Literal["disabled"] = "disabled"
    max_memory_bytes: int = Field(default=512 * 1024 * 1024, ge=1, strict=True)
    max_processes: int = Field(default=8, ge=1, le=64, strict=True)
    kill_process_tree_on_cancel: Literal[True] = True
    require_process_identity: Literal[True] = True


class CapabilitySandboxResult(BaseModel):
    """Attestation returned by the trusted sandbox implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: UUID
    request_digest: str = Field(pattern=SHA256_PATTERN)
    status: Literal["passed", "failed", "isolation_failed"]
    failure_stage: Literal["compile", "import", "test", "isolation"] | None = None
    imported_modules: tuple[str, ...]
    executed_test_paths: tuple[str, ...]
    sandbox_identity: str = Field(pattern=r"^sandbox://[A-Za-z0-9._:/-]+$")
    process_identity: str = Field(
        pattern=r"^(?:sandbox-handle|process)://[A-Za-z0-9._:/-]+$"
    )
    process_identity_verified: Literal[True]
    extracted_tree_sha256: str = Field(pattern=SHA256_PATTERN)
    workspace_was_read_only: Literal[True]
    network_was_disabled: Literal[True]
    resource_limits_were_enforced: Literal[True]
    process_tree_termination_capable: Literal[True]


class CapabilitySandboxTermination(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: UUID
    request_digest: str = Field(pattern=SHA256_PATTERN)
    sandbox_identity: str = Field(pattern=r"^sandbox://[A-Za-z0-9._:/-]+$")
    process_identity: str = Field(
        pattern=r"^(?:sandbox-handle|process)://[A-Za-z0-9._:/-]+$"
    )
    process_identity_verified: Literal[True]
    extracted_tree_sha256: str = Field(pattern=SHA256_PATTERN)
    terminated: Literal[True]
    process_tree_terminated: Literal[True]


class TrustedCapabilitySandboxRunner(Protocol):
    async def validate(
        self,
        request: CapabilitySandboxRequest,
    ) -> CapabilitySandboxResult: ...

    async def cancel(self, execution_id: UUID) -> None: ...

    async def await_termination(
        self,
        execution_id: UUID,
    ) -> CapabilitySandboxTermination: ...


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


class _CapabilityAssertionEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["captain.capability-assertion-evidence.v1"] = Field(
        alias="schema"
    )
    run_id: str
    assertion_id: str
    status: Literal["passed", "failed"]
    producer: Literal["captain"]


class CapabilityPackageValidator:
    """Validate untrusted Forge output using only Captain-owned references."""

    def __init__(
        self,
        *,
        content_store: ReadOnlyCapabilityContentStore,
        sandbox_runner: TrustedCapabilitySandboxRunner | None = None,
        command_timeout_seconds: int = 60,
        termination_timeout_seconds: float = 5.0,
    ) -> None:
        if not 1 <= command_timeout_seconds <= 300:
            raise ValueError("command timeout must be between 1 and 300 seconds")
        if not 0 < termination_timeout_seconds <= 30:
            raise ValueError("termination timeout must be between 0 and 30 seconds")
        self._content_store = content_store
        self._sandbox_runner = sandbox_runner
        self._command_timeout_seconds = command_timeout_seconds
        self._termination_timeout_seconds = termination_timeout_seconds

    async def validate(
        self,
        *,
        job: AgentFactoryJobV2,
        creation_result: CreationResultV1,
        candidate: ForgeCapabilityPackageCandidateV1,
        release_evidence_refs: tuple[ArtifactRef, ...],
    ) -> CapabilityPackageManifestV1:
        if self._sandbox_runner is None:
            raise CapabilityPackageValidationError(
                "capability validation requires an explicit trusted sandbox runner"
            )
        self._validate_authority(job, creation_result, candidate)
        references = _collect_references(
            creation_result,
            candidate,
            release_evidence_refs,
        )
        content = await self._read_verified(references)
        self._validate_sealed_candidate(creation_result, candidate, content)
        release_evidence = self._validate_release_evidence(
            job,
            creation_result,
            candidate,
            release_evidence_refs,
            content,
        )
        skill_receipt = self._validate_skill_receipt(job, candidate, content)
        nested_references = (
            *(
                reference
                for item in release_evidence
                for result in item.assertion_results
                for reference in result.evidence_refs
            ),
            *(
                holdout.evidence_ref
                for item in release_evidence
                for holdout in item.private_holdout_evidence
            ),
            *skill_receipt.evidence_refs,
        )
        content = await self._read_verified(
            _unique_references((*references, *nested_references)),
            existing=content,
        )
        self._validate_assertion_evidence(release_evidence, content)
        archive_content = content[candidate.source_ref.uri]
        tree_digest = await self._validate_archive(
            job,
            candidate,
            archive_content,
            content,
        )
        if any(item.extracted_tree_sha256 != tree_digest for item in release_evidence):
            raise CapabilityPackageValidationError(
                "release evidence tree digest does not match extracted package"
            )
        try:
            return _build_captain_manifest(
                candidate,
                release_evidence_refs,
                release_evidence,
                job,
            )
        except ValidationError as exc:
            raise CapabilityPackageValidationError(
                f"Captain package construction failed: {exc}"
            ) from exc

    @staticmethod
    def _validate_authority(
        job: AgentFactoryJobV2,
        creation_result: CreationResultV1,
        candidate: ForgeCapabilityPackageCandidateV1,
    ) -> None:
        if creation_result.status != "succeeded":
            raise CapabilityPackageValidationError(
                "Forge result is not a successful candidate"
            )
        if candidate.factory_job_id != job.job_id:
            raise CapabilityPackageValidationError(
                "candidate factory job does not match Captain job"
            )
        if candidate.correlation_id != job.correlation_id:
            raise CapabilityPackageValidationError(
                "candidate correlation does not match Captain job"
            )
        if candidate.subject_version != job.subject_version:
            raise CapabilityPackageValidationError(
                "candidate subject version does not match Captain job"
            )
        if candidate.capability_id != job.required_capability:
            raise CapabilityPackageValidationError(
                "candidate capability does not match Captain job"
            )
        if creation_result.creation_job_id != candidate.creation_job_id:
            raise CapabilityPackageValidationError(
                "creation job does not match candidate"
            )
        if creation_result.correlation_id != job.correlation_id:
            raise CapabilityPackageValidationError(
                "creation correlation does not match Captain job"
            )
        if creation_result.subject_version != job.subject_version:
            raise CapabilityPackageValidationError(
                "creation subject version does not match Captain job"
            )
        if creation_result.attempt != candidate.attempt:
            raise CapabilityPackageValidationError(
                "creation attempt does not match candidate attempt"
            )
        if creation_result.package_manifest_ref is None:
            raise CapabilityPackageValidationError(
                "creation result is missing its package manifest"
            )
        if not _contains_reference(creation_result.artifact_refs, candidate.source_ref):
            raise CapabilityPackageValidationError(
                "creation result does not bind the package source archive"
            )
        if creation_result.skill_usage_receipt_ref is None or not _same_reference(
            creation_result.skill_usage_receipt_ref,
            candidate.skill_usage_receipt_ref,
        ):
            raise CapabilityPackageValidationError(
                "creation result does not bind the skill usage receipt"
            )

        creation_gaps = {
            (gap.gap_id, gap.severity, gap.status, _reference_identity(gap.evidence_ref))
            for gap in creation_result.tool_gaps
        }
        package_gaps = {
            (gap.gap_id, gap.severity, gap.status, _reference_identity(gap.evidence_ref))
            for gap in candidate.tool_gaps
        }
        if creation_gaps != package_gaps:
            raise CapabilityPackageValidationError(
                "package tool gaps do not match the creation result"
            )

    async def _read_verified(
        self,
        references: tuple[ArtifactRef, ...],
        *,
        existing: dict[str, bytes] | None = None,
    ) -> dict[str, bytes]:
        content = {} if existing is None else dict(existing)
        for reference in references:
            if reference.uri in content:
                continue
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
    def _validate_release_evidence(
        job: AgentFactoryJobV2,
        creation_result: CreationResultV1,
        candidate: ForgeCapabilityPackageCandidateV1,
        release_evidence_refs: tuple[ArtifactRef, ...],
        content: dict[str, bytes],
    ) -> tuple[CapabilityReleaseEvidenceV1, ...]:
        records: list[CapabilityReleaseEvidenceV1] = []
        for reference in release_evidence_refs:
            raw_content = content[reference.uri]
            try:
                record = CapabilityReleaseEvidenceV1.model_validate_json(raw_content)
            except (ValidationError, ValueError) as exc:
                raise CapabilityPackageValidationError(
                    "release reference does not contain typed release evidence"
                ) from exc
            if raw_content != canonical_capability_release_evidence_bytes(record):
                raise CapabilityPackageValidationError(
                    "release evidence JSON is not in canonical Captain serialization"
                )
            records.append(record)
        evidence = tuple(records)
        recovery = tuple(item for item in evidence if item.kind == "recovery")
        normal = tuple(item for item in evidence if item.kind == "normal")
        if len(recovery) != 1:
            raise CapabilityPackageValidationError(
                "release evidence requires exactly one recovery run"
            )
        if len(normal) != 3:
            raise CapabilityPackageValidationError(
                "release evidence requires exactly three normal E2E runs"
            )
        if len({item.run_id for item in evidence}) != len(evidence):
            raise CapabilityPackageValidationError(
                "release evidence run IDs must be distinct"
            )
        if len({item.run_number for item in evidence}) != len(evidence):
            raise CapabilityPackageValidationError(
                "release evidence run numbers must be distinct"
            )
        for item in evidence:
            if item.factory_job_id != job.job_id:
                raise CapabilityPackageValidationError(
                    "release evidence factory job does not match Captain job"
                )
            if item.correlation_id != job.correlation_id:
                raise CapabilityPackageValidationError(
                    "release evidence correlation does not match Captain job"
                )
            if item.subject_version != job.subject_version:
                raise CapabilityPackageValidationError(
                    "release evidence subject version does not match Captain job"
                )
            if item.creation_job_id != creation_result.creation_job_id:
                raise CapabilityPackageValidationError(
                    "release evidence creation job does not match candidate"
                )
            if item.attempt != creation_result.attempt:
                raise CapabilityPackageValidationError(
                    "release evidence attempt does not match creation attempt"
                )
            if item.capability_id != candidate.capability_id:
                raise CapabilityPackageValidationError(
                    "release evidence capability does not match candidate"
                )
            if item.capability_version != candidate.capability_version:
                raise CapabilityPackageValidationError(
                    "release evidence capability version does not match candidate"
                )
            manifest_ref = creation_result.package_manifest_ref
            if (
                manifest_ref is None
                or item.candidate_manifest_sha256 != manifest_ref.sha256
            ):
                raise CapabilityPackageValidationError(
                    "release evidence candidate digest does not match creation result"
                )
            if item.package_archive_sha256 != candidate.source_ref.sha256:
                raise CapabilityPackageValidationError(
                    "release evidence package digest does not match candidate archive"
                )
            if set(item.assertion_ids) != set(job.acceptance_assertion_ids) or any(
                result.status != "passed" for result in item.assertion_results
            ):
                raise CapabilityPackageValidationError(
                    "release evidence assertions do not match Captain assertions"
                )
        for assertion_id in job.acceptance_assertion_ids:
            intents = {
                result.integration_intent
                for item in evidence
                for result in item.assertion_results
                if result.assertion_id == assertion_id
            }
            if len(intents) != 1:
                raise CapabilityPackageValidationError(
                    "release evidence assertion integration intents do not match"
                )
        recovery_record = recovery[0]
        if any(item.outcome != "succeeded" for item in normal):
            raise CapabilityPackageValidationError(
                "all three normal release evidence runs must be succeeded"
            )
        normal_numbers = sorted(item.run_number for item in normal)
        expected_numbers = list(
            range(recovery_record.run_number + 1, recovery_record.run_number + 4)
        )
        if normal_numbers != expected_numbers:
            raise CapabilityPackageValidationError(
                "three normal runs must be consecutive and after recovery"
            )

        expected_holdouts = {item.holdout_id for item in job.private_holdout_refs}
        holdout_evidence = tuple(
            holdout
            for item in evidence
            for holdout in item.private_holdout_evidence
        )
        actual_holdouts = {item.holdout_id for item in holdout_evidence}
        if (
            actual_holdouts != expected_holdouts
            or len(holdout_evidence) != len(expected_holdouts)
        ):
            raise CapabilityPackageValidationError(
                "typed release evidence does not bind every Captain private holdout"
            )
        if any(
            item.status != "passed"
            or item.assertion_id not in job.acceptance_assertion_ids
            for item in holdout_evidence
        ):
            raise CapabilityPackageValidationError(
                "private holdout evidence must be passed and assertion-bound"
            )
        assertion_evidence_identities = [
            _reference_identity(reference)
            for item in evidence
            for result in item.assertion_results
            for reference in result.evidence_refs
        ]
        if len(assertion_evidence_identities) != len(
            set(assertion_evidence_identities)
        ):
            raise CapabilityPackageValidationError(
                "release assertion results must use distinct evidence"
            )
        return evidence

    @staticmethod
    def _validate_assertion_evidence(
        release_evidence: tuple[CapabilityReleaseEvidenceV1, ...],
        content: dict[str, bytes],
    ) -> None:
        for run in release_evidence:
            for result in run.assertion_results:
                for reference in result.evidence_refs:
                    try:
                        evidence = _CapabilityAssertionEvidenceV1.model_validate_json(
                            content[reference.uri]
                        )
                    except (ValidationError, ValueError) as exc:
                        raise CapabilityPackageValidationError(
                            "assertion evidence is not a typed Captain record"
                        ) from exc
                    if (
                        evidence.run_id != run.run_id
                        or evidence.assertion_id != result.assertion_id
                        or evidence.status != result.status
                    ):
                        raise CapabilityPackageValidationError(
                            "assertion evidence does not match its release result"
                        )

    @staticmethod
    def _validate_skill_receipt(
        job: AgentFactoryJobV2,
        candidate: ForgeCapabilityPackageCandidateV1,
        content: dict[str, bytes],
    ) -> HermesSkillUsageReceipt:
        try:
            receipt = HermesSkillUsageReceipt.model_validate_json(
                content[candidate.skill_usage_receipt_ref.uri]
            )
        except (ValidationError, ValueError) as exc:
            raise CapabilityPackageValidationError(
                "skill usage reference does not contain a typed receipt"
            ) from exc
        if receipt.job_id != job.job_id:
            raise CapabilityPackageValidationError(
                "skill receipt job does not match Captain job"
            )
        if receipt.correlation_id != job.correlation_id:
            raise CapabilityPackageValidationError(
                "skill receipt correlation does not match Captain job"
            )
        if receipt.released_skill.capability != job.required_capability:
            raise CapabilityPackageValidationError(
                "skill receipt capability does not match Captain job"
            )
        if set(receipt.assertion_ids) != set(job.acceptance_assertion_ids):
            raise CapabilityPackageValidationError(
                "skill receipt assertions do not match Captain job"
            )
        if receipt.outcome != "passed":
            raise CapabilityPackageValidationError(
                "skill receipt did not pass"
            )
        if not _contains_reference(
            (item.reference for item in candidate.artifacts),
            receipt.released_skill.content_ref,
        ):
            raise CapabilityPackageValidationError(
                "skill receipt released skill is not sealed in the package"
            )
        return receipt

    @staticmethod
    def _validate_sealed_candidate(
        creation_result: CreationResultV1,
        candidate: ForgeCapabilityPackageCandidateV1,
        content: dict[str, bytes],
    ) -> None:
        reference = creation_result.package_manifest_ref
        if reference is None:  # guarded above; keeps type narrowing local
            raise CapabilityPackageValidationError("creation result has no manifest")
        try:
            sealed = ForgeCapabilityPackageCandidateV1.model_validate_json(
                content[reference.uri]
            )
        except (ValidationError, ValueError) as exc:
            raise CapabilityPackageValidationError(
                "sealed candidate manifest is invalid"
            ) from exc
        if sealed != candidate:
            raise CapabilityPackageValidationError(
                "sealed manifest does not describe the supplied candidate"
            )

    async def _validate_archive(
        self,
        job: AgentFactoryJobV2,
        package: ForgeCapabilityPackageCandidateV1,
        archive_content: bytes,
        resolved_content: dict[str, bytes],
    ) -> str:
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
                tree_digest = _effective_tree_digest(workspace, package)
                await self._run_package_checks(
                    job,
                    package,
                    team_manifest,
                    workspace,
                    tree_digest,
                )
                if _effective_tree_digest(workspace, package) != tree_digest:
                    raise CapabilityPackageValidationError(
                        "trusted sandbox changed the effective extracted tree"
                    )
                return tree_digest
        except zipfile.BadZipFile as exc:
            raise CapabilityPackageValidationError(
                "package source is not a valid ZIP archive"
            ) from exc

    @staticmethod
    def _extract_verified(
        package: ForgeCapabilityPackageCandidateV1,
        archive_content: bytes,
        workspace: Path,
        resolved_content: dict[str, bytes],
    ) -> None:
        declared: dict[str, PackageArtifact] = {}
        declared_paths: dict[str, str] = {}
        for artifact in package.artifacts:
            canonical = _windows_canonical_path(artifact.path)
            existing_path = declared_paths.get(canonical)
            if existing_path is not None:
                raise CapabilityPackageValidationError(
                    "declared artifacts contain a case-insensitive path collision"
                )
            declared[canonical] = artifact
            declared_paths[canonical] = artifact.path
        seen_entries: dict[str, str] = {}
        seen_files: set[str] = set()
        total_size = 0
        with zipfile.ZipFile(io.BytesIO(archive_content)) as archive:
            entries = archive.infolist()
            for entry in entries:
                path = _require_safe_archive_path(entry.filename)
                canonical = _windows_canonical_path(entry.filename)
                existing_entry = seen_entries.get(canonical)
                if existing_entry is not None:
                    raise CapabilityPackageValidationError(
                        "package archive contains a case-insensitive path collision"
                    )
                seen_entries[canonical] = entry.filename
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
                file_type = stat.S_IFMT(mode)
                if file_type not in {0, stat.S_IFREG}:
                    raise CapabilityPackageValidationError(
                        "package archive contains a non-regular file"
                    )
                normalized = path.as_posix()
                seen_files.add(canonical)
                total_size += entry.file_size
                if entry.file_size > 8 * 1024 * 1024 or total_size > 64 * 1024 * 1024:
                    raise CapabilityPackageValidationError(
                        "package archive exceeds the validation size limit"
                    )
                artifact = declared.get(canonical)
                if artifact is None or artifact.path != normalized:
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
                with destination.open("xb") as handle:
                    handle.write(data)
        if seen_files != set(declared):
            raise CapabilityPackageValidationError(
                "package archive does not match declared artifacts"
            )

    @staticmethod
    def _validate_manifest_relationships(
        package: ForgeCapabilityPackageCandidateV1,
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

    async def _run_package_checks(
        self,
        job: AgentFactoryJobV2,
        package: ForgeCapabilityPackageCandidateV1,
        manifest: _TeamManifestV1,
        workspace: Path,
        tree_digest: str,
    ) -> None:
        runner = self._sandbox_runner
        if runner is None:  # validate() fails before resolving content
            raise CapabilityPackageValidationError("trusted sandbox runner is missing")
        module_names = tuple(
            sorted(
                (_canonical_module_name(path) for path in manifest.autogen_modules),
                key=lambda item: (item.count("."), item),
            )
        )
        execution_id = uuid4()
        process_identity = f"sandbox-handle://{execution_id}"
        request_payload = {
            "execution_id": str(execution_id),
            "process_identity": process_identity,
            "correlation_id": str(job.correlation_id),
            "workspace": _canonical_workspace_identity(workspace),
            "python_path_root": _canonical_workspace_identity(workspace),
            "package_sha256": package.source_ref.sha256,
            "tree_sha256": tree_digest,
            "module_names": module_names,
            "test_paths": manifest.test_paths,
            "timeout_seconds": self._command_timeout_seconds,
            "workspace_access": "read_only",
            "network_access": "disabled",
            "max_memory_bytes": 512 * 1024 * 1024,
            "max_processes": 8,
            "kill_process_tree_on_cancel": True,
            "require_process_identity": True,
        }
        request_digest = hashlib.sha256(
            json.dumps(
                request_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        request = CapabilitySandboxRequest(
            request_digest=request_digest,
            execution_id=execution_id,
            process_identity=process_identity,
            correlation_id=job.correlation_id,
            workspace=workspace,
            python_path_root=workspace,
            module_names=module_names,
            test_paths=manifest.test_paths,
            extracted_tree_sha256=tree_digest,
            package_archive_sha256=package.source_ref.sha256,
            timeout_seconds=self._command_timeout_seconds,
        )
        task = asyncio.create_task(runner.validate(request))
        done, _ = await asyncio.wait(
            {task},
            timeout=self._command_timeout_seconds,
        )
        if not done:
            try:
                await asyncio.wait_for(
                    runner.cancel(execution_id),
                    timeout=self._termination_timeout_seconds,
                )
                termination = await asyncio.wait_for(
                    runner.await_termination(execution_id),
                    timeout=self._termination_timeout_seconds,
                )
                if (
                    termination.execution_id != execution_id
                    or termination.request_digest != request.request_digest
                    or termination.process_identity != request.process_identity
                    or termination.process_identity_verified is not True
                    or termination.extracted_tree_sha256
                    != request.extracted_tree_sha256
                    or termination.terminated is not True
                    or termination.process_tree_terminated is not True
                ):
                    raise CapabilityPackageValidationError(
                        "trusted sandbox termination was not attested"
                    )
            except (asyncio.TimeoutError, Exception) as exc:
                raise CapabilityPackageValidationError(
                    "trusted sandbox cancellation failed closed"
                ) from exc
            finally:
                task.cancel()
                cleanup_done, _ = await asyncio.wait(
                    {task},
                    timeout=self._termination_timeout_seconds,
                )
                if not cleanup_done:
                    task.add_done_callback(_consume_task_result)
            raise CapabilityPackageValidationError(
                "trusted sandbox validation timed out"
            )
        try:
            result = task.result()
        except (asyncio.CancelledError, Exception) as exc:
            raise CapabilityPackageValidationError(
                "trusted sandbox validation failed closed"
            ) from exc
        if not isinstance(result, CapabilitySandboxResult):
            raise CapabilityPackageValidationError(
                "trusted sandbox returned an invalid result"
            )
        if (
            result.execution_id != request.execution_id
            or result.request_digest != request.request_digest
            or result.process_identity != request.process_identity
            or result.process_identity_verified is not True
            or result.extracted_tree_sha256 != request.extracted_tree_sha256
        ):
            raise CapabilityPackageValidationError(
                "trusted sandbox result request digest or process identity does not match"
            )
        if (
            result.workspace_was_read_only is not True
            or result.network_was_disabled is not True
            or result.resource_limits_were_enforced is not True
            or result.process_tree_termination_capable is not True
        ):
            raise CapabilityPackageValidationError(
                "trusted sandbox did not attest required isolation"
            )
        if (
            result.imported_modules != request.module_names
            or result.executed_test_paths != request.test_paths
        ):
            raise CapabilityPackageValidationError(
                "trusted sandbox did not execute the exact allowlist"
            )
        if result.status != "passed":
            stage = result.failure_stage or "isolation"
            raise CapabilityPackageValidationError(
                f"trusted sandbox {stage} validation failed"
            )


def _build_captain_manifest(
    candidate: ForgeCapabilityPackageCandidateV1,
    release_evidence_refs: tuple[ArtifactRef, ...],
    release_evidence: tuple[CapabilityReleaseEvidenceV1, ...],
    job: AgentFactoryJobV2,
) -> CapabilityPackageManifestV1:
    recovery = next(item for item in release_evidence if item.kind == "recovery")
    recovery_assertion_id = recovery.recovery_assertion_id
    if recovery_assertion_id is None:  # contract validation already prevents this
        raise CapabilityPackageValidationError(
            "recovery evidence is missing its assertion identity"
        )
    recovery_result = next(
        item
        for item in recovery.assertion_results
        if item.assertion_id == recovery_assertion_id
    )
    assertion_outcomes = tuple(
        AssertionOutcome(
            assertion_id=assertion_id,
            status="passed",
            integration_intent=next(
                result.integration_intent
                for item in release_evidence
                for result in item.assertion_results
                if result.assertion_id == assertion_id
            ),
            evidence_refs=_unique_references(
                reference
                for item in release_evidence
                for result in item.assertion_results
                if result.assertion_id == assertion_id
                for reference in result.evidence_refs
            ),
        )
        for assertion_id in job.acceptance_assertion_ids
    )
    holdout_evidence = tuple(
        holdout
        for item in release_evidence
        for holdout in item.private_holdout_evidence
    )
    private_holdout_receipts = tuple(
        PrivateHoldoutReceipt(
            holdout_id=evidence.holdout_id,
            assertion_id=evidence.assertion_id,
            status=evidence.status,
            evidence_ref=evidence.evidence_ref,
        )
        for evidence in holdout_evidence
    )
    recovery_index = release_evidence.index(recovery)
    return CapabilityPackageManifestV1(
        schema_name="captain.capability-package.v1",
        capability_id=candidate.capability_id,
        capability_version=candidate.capability_version,
        factory_job_id=candidate.factory_job_id,
        creation_job_id=candidate.creation_job_id,
        correlation_id=candidate.correlation_id,
        subject_version=candidate.subject_version,
        source_ref=candidate.source_ref,
        team_manifest_ref=candidate.team_manifest_ref,
        artifacts=candidate.artifacts,
        assertion_outcomes=assertion_outcomes,
        private_holdout_receipts=private_holdout_receipts,
        recovery_receipt=ControlledRecoveryReceipt(
            recovery_id=recovery.recovery_id,
            assertion_id=recovery_assertion_id,
            status="passed",
            evidence_ref=release_evidence_refs[recovery_index],
        ),
        release_evidence_refs=release_evidence_refs,
        skill_usage_receipt_ref=candidate.skill_usage_receipt_ref,
        tool_gaps=candidate.tool_gaps,
        runbook_ref=candidate.runbook_ref,
    )


def _collect_references(
    creation_result: CreationResultV1,
    package: ForgeCapabilityPackageCandidateV1,
    release_evidence_refs: tuple[ArtifactRef, ...],
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
        *release_evidence_refs,
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


def _unique_references(references: Iterable[object]) -> tuple[ArtifactRef, ...]:
    by_uri: dict[str, ArtifactRef] = {}
    for value in references:
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
    _windows_canonical_path(value)
    logical_value = value[:-1] if value.endswith("/") else value
    return PurePosixPath(logical_value)


_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


def _windows_canonical_path(value: str) -> str:
    logical_value = value[:-1] if value.endswith("/") else value
    if (
        not logical_value
        or "\\" in logical_value
        or "\x00" in logical_value
        or "//" in logical_value
    ):
        raise CapabilityPackageValidationError(
            "package archive contains an unsafe path"
        )
    path = PurePosixPath(logical_value)
    raw_parts = logical_value.split("/")
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or path.as_posix() != logical_value
    ):
        raise CapabilityPackageValidationError(
            "package archive contains an unsafe path"
        )
    canonical_parts: list[str] = []
    for part in raw_parts:
        if ":" in part or part.endswith((".", " ")):
            raise CapabilityPackageValidationError(
                "package archive contains an unsafe path"
            )
        normalized = unicodedata.normalize("NFC", part)
        device_name = normalized.split(".", 1)[0].casefold()
        if device_name in _WINDOWS_RESERVED_NAMES:
            raise CapabilityPackageValidationError(
                "package archive contains an unsafe path"
            )
        canonical_parts.append(normalized.casefold())
    return "/".join(canonical_parts)


def _effective_tree_digest(
    workspace: Path,
    package: ForgeCapabilityPackageCandidateV1,
) -> str:
    declared = {item.path: item for item in package.artifacts}
    actual_paths: set[str] = set()
    entries: list[tuple[str, str, int]] = []
    for path in workspace.rglob("*"):
        if path.is_symlink():
            raise CapabilityPackageValidationError(
                "effective extracted tree contains a symlink"
            )
        if not path.is_file():
            continue
        relative = path.relative_to(workspace).as_posix()
        _windows_canonical_path(relative)
        actual_paths.add(relative)
        artifact = declared.get(relative)
        if artifact is None:
            raise CapabilityPackageValidationError(
                "effective extracted tree contains an undeclared file"
            )
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != artifact.reference.sha256:
            raise CapabilityPackageValidationError(
                "effective extracted tree digest does not match artifact"
            )
        entries.append((relative, digest, len(data)))
    if actual_paths != set(declared):
        raise CapabilityPackageValidationError(
            "effective extracted tree does not match declared artifacts"
        )
    serialized = json.dumps(
        sorted(entries),
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _canonical_module_name(path: str) -> str:
    logical = PurePosixPath(path)
    if not path.startswith("autogen/") or logical.suffix != ".py":
        raise CapabilityPackageValidationError(
            "AutoGen module path is not importable"
        )
    parts = list(logical.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    if not parts or any(not part.isidentifier() for part in parts):
        raise CapabilityPackageValidationError(
            "AutoGen module path is not importable"
        )
    return ".".join(parts)


def _canonical_workspace_identity(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").casefold()


def _consume_task_result(task: asyncio.Task[object]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        return


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
