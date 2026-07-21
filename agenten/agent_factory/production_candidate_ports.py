"""Shared-CAS candidate resolution and Docker-backed sandbox attestation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import stat
import unicodedata
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.candidate_evaluation import (
    FactoryAutoGenTeamManifestV1,
    FactoryCandidateArtifact,
    FactoryCandidateManifest,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.capability_factory_entrypoint import (
    DockerCapabilitySandboxRunner,
    DockerCommandRunner,
)
from agenten.agent_factory.capability_live_adapters import ContentAddressedArtifactStore
from agenten.agent_factory.capability_v3_evidence_bridge import (
    CapabilityCandidateAttestationPort,
    CapabilityCandidateAttestationV1,
    CapabilityCandidateProviderPort,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.outcome_contracts import (
    ForgeCapabilityPackageCandidateV1,
    PackageArtifact,
)
from agenten.agent_factory.outcome_validation import (
    CapabilitySandboxRequest,
    CapabilitySandboxResult,
    TrustedCapabilitySandboxRunner,
)
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_runtime.contracts import ArtifactRef


_CAPTAIN_IMAGE = re.compile(
    r"^(?:(?:[a-z0-9.-]+(?::[0-9]+)?)/)?"
    r"captain-[a-z0-9._/-]+@sha256:[0-9a-f]{64}$"
)
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


class ProductionCandidatePortError(ValueError):
    """The sealed candidate or its sandbox evidence is not trustworthy."""


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ProductionCandidateSandboxEvidenceV1(_FrozenContract):
    """Durable, redacted Docker isolation evidence for one exact V3 job."""

    schema_name: Literal["captain.production-candidate-sandbox-evidence.v1"] = Field(
        default="captain.production-candidate-sandbox-evidence.v1",
        alias="schema",
        serialization_alias="schema",
    )
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    candidate_ref: ArtifactRef
    extracted_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sandbox_image: str
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    sandbox_identity: str
    imported_modules: tuple[str, ...] = Field(min_length=1)
    executed_test_paths: tuple[str, ...] = Field(min_length=1)
    workspace_was_read_only: Literal[True]
    network_was_disabled: Literal[True]
    resource_limits_were_enforced: Literal[True]
    process_tree_termination_capable: Literal[True]
    attested_at: datetime

    @field_validator("sandbox_image")
    @classmethod
    def require_pinned_captain_image(cls, value: str) -> str:
        if _CAPTAIN_IMAGE.fullmatch(value) is None:
            raise ValueError("sandbox image is not Captain-owned and digest-pinned")
        return value

    @field_validator("attested_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("sandbox attestation clock must be UTC")
        return value

    @model_validator(mode="after")
    def require_candidate_digest_uri(self) -> "ProductionCandidateSandboxEvidenceV1":
        if self.candidate_ref.uri.rsplit("/", 1)[-1] != self.candidate_ref.sha256:
            raise ValueError("sandbox candidate reference is not content-addressed")
        return self


class FactoryCandidateExecutionDescriptorV1(_FrozenContract):
    """Archive-internal descriptor without the circular source-archive digest."""

    schema_name: Literal["captain.factory-candidate-descriptor.v1"] = Field(
        default="captain.factory-candidate-descriptor.v1",
        alias="schema",
        serialization_alias="schema",
    )
    candidate_id: str
    team_manifest: FactoryCandidateArtifact
    workflow_artifacts: tuple[FactoryCandidateArtifact, ...] = Field(min_length=1)
    tool_schema_artifacts: tuple[FactoryCandidateArtifact, ...] = Field(min_length=2)
    n8n_tools: tuple[TypedN8nTool, ...] = Field(min_length=1)
    build_command: tuple[str, ...] = Field(min_length=1)
    real_case_command: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(ge=1, le=300, strict=True)

    def materialize(self, source_ref: ArtifactRef) -> FactoryCandidateManifest:
        payload = self.model_dump(mode="python", exclude={"schema_name"})
        return FactoryCandidateManifest(
            source_archive_ref=source_ref,
            **payload,
        )


class SharedCasFactoryCandidateManifestPublisher:
    """Atomically publish the executable descriptor sealed by Package C."""

    _DESCRIPTOR_PATH = "adapters/factory-candidate.json"

    def __init__(self, artifacts: ContentAddressedArtifactStore) -> None:
        self._artifacts = artifacts

    def publish(
        self,
        candidate: ForgeCapabilityPackageCandidateV1,
    ) -> ArtifactRef:
        descriptor_artifacts = tuple(
            item
            for item in candidate.artifacts
            if item.path == self._DESCRIPTOR_PATH
            and item.kind == "local_adapter"
        )
        if len(descriptor_artifacts) != 1:
            raise ProductionCandidatePortError(
                "TODO_TOOL.v1 required capability=factory_candidate_descriptor; "
                "reason=Package-C archive lacks adapters/factory-candidate.json"
            )
        descriptor_artifact = descriptor_artifacts[0]
        try:
            descriptor = FactoryCandidateExecutionDescriptorV1.model_validate_json(
                self._artifacts.read_bytes(descriptor_artifact.reference)
            )
        except (TypeError, ValueError) as exc:
            raise ProductionCandidatePortError(
                "Package-C Factory candidate descriptor is invalid"
            ) from exc
        manifest = descriptor.materialize(candidate.source_ref)
        return bind_factory_candidate_manifest(
            self._artifacts,
            candidate,
            manifest,
        )


@dataclass(frozen=True)
class ProductionCandidatePorts:
    candidate_provider: CapabilityCandidateProviderPort
    candidate_attestation: CapabilityCandidateAttestationPort
    candidate_manifest_publisher: SharedCasFactoryCandidateManifestPublisher


class SharedCasCapabilityCandidateProvider:
    """Resolve only a Package-C archive and descriptor bound in the shared CAS."""

    def __init__(
        self,
        artifacts: ContentAddressedArtifactStore,
        publisher: SharedCasFactoryCandidateManifestPublisher,
    ) -> None:
        self._artifacts = artifacts
        self._publisher = publisher

    def candidate_for(
        self,
        job: AgentFactoryJobV3,
        candidate: ForgeCapabilityPackageCandidateV1,
    ) -> ResolvedFactoryCandidate:
        _require_job_binding(job, candidate)
        _verified_archive_files(self._artifacts, candidate)
        reference = self._artifacts.binding(
            "factory-candidate-manifest", candidate.source_ref.sha256
        )
        if reference is None:
            reference = self._publisher.publish(candidate)
        try:
            manifest = FactoryCandidateManifest.model_validate_json(
                self._artifacts.read_bytes(reference)
            )
        except (TypeError, ValueError) as exc:
            raise ProductionCandidatePortError(
                "bound Factory candidate manifest is invalid"
            ) from exc
        _require_manifest_binding(self._artifacts, candidate, manifest)
        return ResolvedFactoryCandidate(
            candidate=manifest,
            source_archive=self._artifacts.local_path(candidate.source_ref),
        )


class DockerCapabilityCandidateAttestor:
    """Attest one exact candidate in the trusted read-only Docker sandbox."""

    def __init__(
        self,
        *,
        artifacts: ContentAddressedArtifactStore,
        provider: SharedCasCapabilityCandidateProvider,
        sandbox_image: str,
        sandbox: TrustedCapabilitySandboxRunner,
        clock: Callable[[], datetime],
    ) -> None:
        if _CAPTAIN_IMAGE.fullmatch(sandbox_image) is None:
            raise ProductionCandidatePortError(
                "sandbox image must be Captain-owned and digest-pinned"
            )
        self._artifacts = artifacts
        self._provider = provider
        self._image = sandbox_image
        self._sandbox = sandbox
        self._clock = clock

    async def attest(
        self,
        job: AgentFactoryJobV3,
        resolved: ResolvedFactoryCandidate,
        candidate: ForgeCapabilityPackageCandidateV1,
    ) -> CapabilityCandidateAttestationV1:
        canonical = self._provider.candidate_for(job, candidate)
        if canonical != resolved:
            raise ProductionCandidatePortError(
                "resolved candidate differs from the shared CAS authority"
            )
        identity = str(job.job_id)
        existing = self._artifacts.binding("candidate-sandbox-attestation", identity)
        if existing is not None:
            evidence = ProductionCandidateSandboxEvidenceV1.model_validate_json(
                self._artifacts.read_bytes(existing)
            )
            self._require_evidence_binding(evidence, job, candidate)
            return CapabilityCandidateAttestationV1(
                job_id=job.job_id,
                candidate_ref=candidate.source_ref,
                extracted_tree_sha256=evidence.extracted_tree_sha256,
                sandbox_evidence_ref=existing,
            )
        files = _verified_archive_files(self._artifacts, candidate)
        with TemporaryDirectory(prefix="captain-v3-candidate-") as temporary:
            workspace = Path(temporary) / "candidate"
            workspace.mkdir()
            _write_verified_workspace(workspace, files)
            tree_digest = _tree_digest(workspace, candidate)
            request = _sandbox_request(
                job,
                candidate,
                workspace,
                tree_digest,
                timeout_seconds=resolved.candidate.timeout_seconds,
            )
            result = await self._run_sandbox(request)
        evidence = ProductionCandidateSandboxEvidenceV1(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            candidate_ref=candidate.source_ref,
            extracted_tree_sha256=tree_digest,
            sandbox_image=self._image,
            request_digest=result.request_digest,
            sandbox_identity=result.sandbox_identity,
            imported_modules=result.imported_modules,
            executed_test_paths=result.executed_test_paths,
            workspace_was_read_only=result.workspace_was_read_only,
            network_was_disabled=result.network_was_disabled,
            resource_limits_were_enforced=result.resource_limits_were_enforced,
            process_tree_termination_capable=result.process_tree_termination_capable,
            attested_at=self._utc_now(),
        )
        evidence_ref = self._artifacts.put(
            evidence.model_dump_json(by_alias=True).encode("utf-8"),
            "application/json",
            namespace="candidate-sandbox-evidence",
        )
        self._artifacts.bind("candidate-sandbox-attestation", identity, evidence_ref)
        return CapabilityCandidateAttestationV1(
            job_id=job.job_id,
            candidate_ref=candidate.source_ref,
            extracted_tree_sha256=tree_digest,
            sandbox_evidence_ref=evidence_ref,
        )

    async def _run_sandbox(
        self,
        request: CapabilitySandboxRequest,
    ) -> CapabilitySandboxResult:
        task = asyncio.create_task(self._sandbox.validate(request))
        try:
            result = await asyncio.wait_for(
                asyncio.shield(task), timeout=float(request.timeout_seconds)
            )
        except asyncio.TimeoutError as exc:
            await self._cancel_and_verify(request)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise ProductionCandidatePortError("candidate sandbox timed out") from exc
        except asyncio.CancelledError:
            await asyncio.shield(self._cancel_and_verify(request))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        except Exception as exc:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise ProductionCandidatePortError(
                "candidate sandbox execution failed closed"
            ) from exc
        if not isinstance(result, CapabilitySandboxResult):
            raise ProductionCandidatePortError("candidate sandbox returned an invalid result")
        if (
            result.status != "passed"
            or result.failure_stage is not None
            or result.execution_id != request.execution_id
            or result.request_digest != request.request_digest
            or result.process_identity != request.process_identity
            or result.process_identity_verified is not True
            or result.extracted_tree_sha256 != request.extracted_tree_sha256
            or result.imported_modules != request.module_names
            or result.executed_test_paths != request.test_paths
            or result.workspace_was_read_only is not True
            or result.network_was_disabled is not True
            or result.resource_limits_were_enforced is not True
            or result.process_tree_termination_capable is not True
        ):
            raise ProductionCandidatePortError(
                "candidate sandbox did not pass with the required isolation"
            )
        return result

    async def _cancel_and_verify(self, request: CapabilitySandboxRequest) -> None:
        await self._sandbox.cancel(request.execution_id)
        termination = await self._sandbox.await_termination(request.execution_id)
        if (
            termination.execution_id != request.execution_id
            or termination.request_digest != request.request_digest
            or termination.process_identity != request.process_identity
            or termination.extracted_tree_sha256 != request.extracted_tree_sha256
            or termination.process_identity_verified is not True
            or termination.terminated is not True
            or termination.process_tree_terminated is not True
        ):
            raise ProductionCandidatePortError(
                "sandbox termination was not attestably completed"
            )

    def _require_evidence_binding(
        self,
        evidence: ProductionCandidateSandboxEvidenceV1,
        job: AgentFactoryJobV3,
        candidate: ForgeCapabilityPackageCandidateV1,
    ) -> None:
        if (
            evidence.job_id != job.job_id
            or evidence.correlation_id != job.correlation_id
            or evidence.subject_version != job.subject_version
            or evidence.candidate_ref != candidate.source_ref
            or evidence.sandbox_image != self._image
        ):
            raise ProductionCandidatePortError(
                "cached candidate sandbox attestation changed authority"
            )

    def _utc_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ProductionCandidatePortError("candidate attestation clock must be UTC")
        return now


def bind_factory_candidate_manifest(
    artifacts: ContentAddressedArtifactStore,
    candidate: ForgeCapabilityPackageCandidateV1,
    manifest: FactoryCandidateManifest,
) -> ArtifactRef:
    """Write-once bind an externally assembled execution descriptor to an archive."""

    _require_manifest_binding(artifacts, candidate, manifest)
    reference = artifacts.put(
        manifest.model_dump_json(by_alias=True).encode("utf-8"),
        "application/json",
        namespace="factory-candidate-manifest",
    )
    return artifacts.bind(
        "factory-candidate-manifest", candidate.source_ref.sha256, reference
    )


def build_production_candidate_ports(
    *,
    artifacts: ContentAddressedArtifactStore,
    sandbox_image: str,
    sandbox_runner: TrustedCapabilitySandboxRunner | None = None,
    docker_command_runner: DockerCommandRunner | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ProductionCandidatePorts:
    """Build candidate-side ports without performing Docker or provider effects."""

    if _CAPTAIN_IMAGE.fullmatch(sandbox_image) is None:
        raise ProductionCandidatePortError(
            "sandbox image must be Captain-owned and digest-pinned"
        )
    if sandbox_runner is not None and docker_command_runner is not None:
        raise ProductionCandidatePortError(
            "inject either a sandbox runner or Docker command runner, not both"
        )
    publisher = SharedCasFactoryCandidateManifestPublisher(artifacts)
    provider = SharedCasCapabilityCandidateProvider(artifacts, publisher)
    sandbox = sandbox_runner or DockerCapabilitySandboxRunner(
        image=sandbox_image,
        command_runner=docker_command_runner,
    )
    attestor = DockerCapabilityCandidateAttestor(
        artifacts=artifacts,
        provider=provider,
        sandbox_image=sandbox_image,
        sandbox=sandbox,
        clock=clock or (lambda: datetime.now(timezone.utc)),
    )
    return ProductionCandidatePorts(
        candidate_provider=provider,
        candidate_attestation=attestor,
        candidate_manifest_publisher=publisher,
    )


def _require_job_binding(
    job: AgentFactoryJobV3,
    candidate: ForgeCapabilityPackageCandidateV1,
) -> None:
    if (
        candidate.correlation_id != job.correlation_id
        or candidate.subject_version != job.subject_version
        or candidate.capability_id != job.required_capability
    ):
        raise ProductionCandidatePortError(
            "Package-C candidate does not match the V3 job authority"
        )


def _require_manifest_binding(
    artifacts: ContentAddressedArtifactStore,
    package: ForgeCapabilityPackageCandidateV1,
    manifest: FactoryCandidateManifest,
) -> None:
    if manifest.source_archive_ref != package.source_ref:
        raise ProductionCandidatePortError(
            "Factory candidate manifest references a different Package-C archive"
        )
    artifacts.read_bytes(package.source_ref)
    declared = {
        (artifact.path, artifact.reference): artifact for artifact in package.artifacts
    }
    bound = (
        manifest.team_manifest,
        *manifest.workflow_artifacts,
        *manifest.tool_schema_artifacts,
    )
    for item in bound:
        if (item.relative_path, item.reference) not in declared:
            raise ProductionCandidatePortError(
                "Factory candidate manifest contains an undeclared package artifact"
            )
        artifacts.read_bytes(item.reference)
    try:
        team = FactoryAutoGenTeamManifestV1.model_validate_json(
            artifacts.read_bytes(manifest.team_manifest.reference),
            context={"allowed_tools": {tool.name for tool in manifest.n8n_tools}},
        )
    except (TypeError, ValueError) as exc:
        raise ProductionCandidatePortError(
            "Factory candidate AutoGen team manifest is invalid"
        ) from exc
    package_refs = {artifact.reference for artifact in package.artifacts}
    if any(agent.system_prompt_ref not in package_refs for agent in team.agents):
        raise ProductionCandidatePortError(
            "AutoGen system prompt is not sealed in the Package-C archive"
        )


def _verified_archive_files(
    artifacts: ContentAddressedArtifactStore,
    package: ForgeCapabilityPackageCandidateV1,
) -> dict[str, bytes]:
    if package.source_ref.media_type != "application/zip":
        raise ProductionCandidatePortError("candidate source must be application/zip")
    archive_content = artifacts.read_bytes(package.source_ref)
    declared = {_canonical_path(item.path): item for item in package.artifacts}
    seen: set[str] = set()
    seen_entries: set[str] = set()
    files: dict[str, bytes] = {}
    total_size = 0
    try:
        with zipfile.ZipFile(Path(artifacts.local_path(package.source_ref))) as archive:
            for entry in archive.infolist():
                logical = entry.filename.rstrip("/") if entry.is_dir() else entry.filename
                canonical = _canonical_path(logical)
                if canonical in seen_entries:
                    raise ProductionCandidatePortError(
                        "candidate archive contains a case-insensitive path collision"
                    )
                seen_entries.add(canonical)
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or entry.flag_bits & 0x1:
                    raise ProductionCandidatePortError(
                        "candidate archive contains an unsafe path or encrypted member"
                    )
                if entry.is_dir():
                    continue
                if stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                    raise ProductionCandidatePortError(
                        "candidate archive contains a non-regular file"
                    )
                total_size += entry.file_size
                if entry.file_size > 8 * 1024 * 1024 or total_size > 64 * 1024 * 1024:
                    raise ProductionCandidatePortError(
                        "candidate archive exceeds the safe extraction limit"
                    )
                artifact = declared.get(canonical)
                if artifact is None or artifact.path != PurePosixPath(logical).as_posix():
                    raise ProductionCandidatePortError(
                        "candidate archive contains an undeclared file"
                    )
                content = archive.read(entry)
                if (
                    len(content) != entry.file_size
                    or hashlib.sha256(content).hexdigest() != artifact.reference.sha256
                    or artifacts.read_bytes(artifact.reference) != content
                ):
                    raise ProductionCandidatePortError(
                        "candidate archive differs from its shared-CAS artifact"
                    )
                seen.add(canonical)
                files[artifact.path] = content
    except zipfile.BadZipFile as exc:
        raise ProductionCandidatePortError("candidate source is not a valid ZIP") from exc
    if seen != set(declared):
        raise ProductionCandidatePortError(
            "candidate archive does not contain the complete declared package"
        )
    if hashlib.sha256(archive_content).hexdigest() != package.source_ref.sha256:
        raise ProductionCandidatePortError("candidate archive digest changed")
    return files


def _canonical_path(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise ProductionCandidatePortError("candidate archive contains an unsafe path")
    path = PurePosixPath(value)
    parts = value.split("/")
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or path.as_posix() != value
    ):
        raise ProductionCandidatePortError("candidate archive contains an unsafe path")
    canonical: list[str] = []
    for part in parts:
        normalized = unicodedata.normalize("NFC", part)
        device = normalized.split(".", 1)[0].casefold()
        if ":" in part or part.endswith((".", " ")) or device in _WINDOWS_RESERVED_NAMES:
            raise ProductionCandidatePortError("candidate archive contains an unsafe path")
        canonical.append(normalized.casefold())
    return "/".join(canonical)


def _write_verified_workspace(workspace: Path, files: Mapping[str, bytes]) -> None:
    for relative, content in files.items():
        path = workspace.joinpath(*PurePosixPath(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(content)


def _tree_digest(
    workspace: Path,
    package: ForgeCapabilityPackageCandidateV1,
) -> str:
    entries: list[tuple[str, str, int]] = []
    for artifact in package.artifacts:
        path = workspace.joinpath(*PurePosixPath(artifact.path).parts)
        if path.is_symlink() or not path.is_file():
            raise ProductionCandidatePortError("extracted candidate tree changed")
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != artifact.reference.sha256:
            raise ProductionCandidatePortError("extracted candidate tree digest changed")
        entries.append((artifact.path, digest, len(content)))
    return hashlib.sha256(
        json.dumps(sorted(entries), separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sandbox_request(
    job: AgentFactoryJobV3,
    package: ForgeCapabilityPackageCandidateV1,
    workspace: Path,
    tree_digest: str,
    *,
    timeout_seconds: int,
) -> CapabilitySandboxRequest:
    modules = tuple(
        sorted(
            {
                _module_name(item.path)
                for item in package.artifacts
                if item.kind == "autogen_source"
            },
            key=lambda name: (name.count("."), name),
        )
    )
    tests = tuple(
        sorted(item.path for item in package.artifacts if item.kind == "test")
    )
    if not modules or not tests:
        raise ProductionCandidatePortError(
            "candidate sandbox requires sealed AutoGen modules and tests"
        )
    execution_id = uuid4()
    process_identity = f"sandbox-handle://{execution_id}"
    workspace_identity = str(workspace.resolve()).replace("\\", "/").casefold()
    payload = {
        "execution_id": str(execution_id),
        "process_identity": process_identity,
        "correlation_id": str(job.correlation_id),
        "workspace": workspace_identity,
        "python_path_root": workspace_identity,
        "package_sha256": package.source_ref.sha256,
        "tree_sha256": tree_digest,
        "module_names": modules,
        "test_paths": tests,
        "timeout_seconds": timeout_seconds,
        "workspace_access": "read_only",
        "network_access": "disabled",
        "max_memory_bytes": 512 * 1024 * 1024,
        "max_processes": 8,
        "kill_process_tree_on_cancel": True,
        "require_process_identity": True,
    }
    request_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CapabilitySandboxRequest(
        request_digest=request_digest,
        execution_id=execution_id,
        process_identity=process_identity,
        correlation_id=job.correlation_id,
        workspace=workspace,
        python_path_root=workspace,
        module_names=modules,
        test_paths=tests,
        extracted_tree_sha256=tree_digest,
        package_archive_sha256=package.source_ref.sha256,
        timeout_seconds=timeout_seconds,
    )


def _module_name(path: str) -> str:
    logical = PurePosixPath(path)
    if not path.startswith("autogen/") or logical.suffix != ".py":
        raise ProductionCandidatePortError("AutoGen source path is not importable")
    parts = list(logical.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    if not parts or any(not item.isidentifier() for item in parts):
        raise ProductionCandidatePortError("AutoGen source path is not importable")
    return ".".join(parts)
