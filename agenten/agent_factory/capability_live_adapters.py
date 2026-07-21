"""Production-safe adapters for Package-C creation and release evidence ports.

External Hermes/AutoGen/Codex work remains behind injected execution ports.  These
adapters turn only validated, identity-bound observations into Captain lifecycle
records and persist their exact bytes in a local content-addressed store.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.contracts import (
    AgentFactoryJobV2,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.forge_contracts import (
    CreationJobV1,
    CreationResultV1,
    CreationSubmissionReceipt,
)
from agenten.agent_factory.outcome_contracts import (
    CapabilityAssertionResult,
    CapabilityReleaseEvidenceV1,
    ForgeCapabilityPackageCandidateV1,
    PrivateHoldoutEvidence,
    canonical_capability_release_evidence_bytes,
)
from agenten.agent_runtime.contracts import ArtifactRef


_NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_FACTORY_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CAPABILITY_ADAPTER_SCHEMA = "captain.capability-factory-adapter-manifest.v2"


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapabilityCreationPreparation(_FrozenContract):
    """Hermes-authored references needed for the three creation lifecycle blocks."""

    factory_job_id: UUID
    creation_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    occurred_at: datetime
    blueprint_lease_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    blueprint_evidence_ref: ArtifactRef
    tool_lease_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    tool_evidence_ref: ArtifactRef
    code_lease_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

    @field_validator("occurred_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("creation preparation timestamp must be UTC")
        return value


class CapabilityReleaseObservation(_FrozenContract):
    """Untrusted provider observation from which Captain derives release evidence."""

    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    capability_version: int = Field(ge=1, strict=True)
    extracted_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["recovery", "normal"]
    outcome: Literal["expected_failure_recovered", "succeeded", "failed"]
    assertion_results: tuple[CapabilityAssertionResult, ...] = Field(min_length=1)
    recovery_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    recovery_assertion_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    private_holdout_evidence: tuple[PrivateHoldoutEvidence, ...] = ()
    build_lease_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    tester_lease_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    quality_lease_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("release observation timestamp must be UTC")
        return value

    @model_validator(mode="after")
    def require_kind_semantics(self) -> "CapabilityReleaseObservation":
        if self.kind == "recovery":
            if (
                self.outcome != "expected_failure_recovered"
                or self.recovery_id is None
                or self.recovery_assertion_id is None
            ):
                raise ValueError("controlled recovery observation is incomplete")
        elif self.recovery_id is not None or self.recovery_assertion_id is not None:
            raise ValueError("normal observation cannot carry recovery identity")
        return self


class CapabilityCreationBackendPort(Protocol):
    """Backend implemented by the existing Hermes six-skill runtime graph."""

    async def prepare(
        self,
        job: AgentFactoryJobV2,
        creation_job: CreationJobV1,
    ) -> CapabilityCreationPreparation: ...

    async def submit(self, creation_job: CreationJobV1) -> CreationSubmissionReceipt: ...

    async def result(self, creation_job_id: UUID) -> CreationResultV1: ...


class CapabilityReleaseExecutorPort(Protocol):
    """Runs one controlled-recovery or normal case in the real team runtime."""

    async def execute(
        self,
        job: AgentFactoryJobV2,
        creation_result: CreationResultV1,
        candidate: ForgeCapabilityPackageCandidateV1,
        run_number: int,
    ) -> CapabilityReleaseObservation | None: ...


@dataclass(frozen=True)
class GeneratedCapabilityAdapterManifest:
    content: bytes
    sha256: str
    module_sha256: str


@dataclass(frozen=True)
class WrittenCapabilityAdapterManifest(GeneratedCapabilityAdapterManifest):
    path: Path


def generate_capability_adapter_manifest(
    *,
    workspace_root: Path,
    module_path: Path,
    factory_symbol: str,
) -> GeneratedCapabilityAdapterManifest:
    """Generate Package-C's static adapter manifest, never a runtime graph manifest."""

    root = workspace_root.resolve()
    module = module_path.resolve()
    try:
        relative = module.relative_to(root)
    except ValueError as exc:
        raise ValueError("capability adapter module is outside the workspace") from exc
    if module.suffix.casefold() != ".py" or not module.is_file():
        raise ValueError("capability adapter module must be a readable Python file")
    if _FACTORY_SYMBOL.fullmatch(factory_symbol) is None:
        raise ValueError("capability adapter factory symbol is invalid")
    content = module.read_bytes()
    if len(content) > 1_048_576:
        raise ValueError("capability adapter module exceeds the size limit")
    try:
        tree = ast.parse(content.decode("utf-8"), filename=str(module))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise ValueError("capability adapter module is not valid UTF-8 Python") from exc
    matches = tuple(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == factory_symbol
    )
    if len(matches) != 1:
        raise ValueError("capability adapter factory symbol is missing or ambiguous")
    module_sha256 = hashlib.sha256(content).hexdigest()
    payload = {
        "schema": _CAPABILITY_ADAPTER_SCHEMA,
        "module_path": relative.as_posix(),
        "module_sha256": module_sha256,
        "factory_symbol": factory_symbol,
    }
    manifest = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return GeneratedCapabilityAdapterManifest(
        content=manifest,
        sha256=hashlib.sha256(manifest).hexdigest(),
        module_sha256=module_sha256,
    )


def write_capability_adapter_manifest(
    *,
    workspace_root: Path,
    module_path: Path,
    factory_symbol: str,
    output_directory: Path,
) -> WrittenCapabilityAdapterManifest:
    """Write one immutable manifest named by its own digest."""

    generated = generate_capability_adapter_manifest(
        workspace_root=workspace_root,
        module_path=module_path,
        factory_symbol=factory_symbol,
    )
    root = workspace_root.resolve()
    output = output_directory.resolve()
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise ValueError("capability adapter manifest output is outside the workspace") from exc
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{generated.sha256}.json"
    ContentAddressedArtifactStore._write_immutable(path, generated.content)
    return WrittenCapabilityAdapterManifest(
        content=generated.content,
        sha256=generated.sha256,
        module_sha256=generated.module_sha256,
        path=path,
    )


class ContentAddressedArtifactStore:
    """Immutable on-disk bytes and replay bindings under a gitignored root."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._content_root = self._root / "content" / "sha256"
        self._binding_root = self._root / "bindings"
        self._content_root.mkdir(parents=True, exist_ok=True)
        self._binding_root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def put(self, content: bytes, media_type: str, *, namespace: str) -> ArtifactRef:
        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes")
        if _NAMESPACE.fullmatch(namespace) is None:
            raise ValueError("artifact namespace is invalid")
        digest = hashlib.sha256(content).hexdigest()
        target = self._content_root / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_immutable(target, content)
        return ArtifactRef(
            uri=f"artifact://capability-factory/{namespace}/{digest}",
            sha256=digest,
            media_type=media_type,
        )

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        self._require_reference(reference)
        return self.read_sha256(reference.sha256)

    def read_sha256(self, digest: str) -> bytes:
        """Read shared CAS content by an already-authorized contract digest."""

        if (
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("artifact content digest is invalid")
        target = self._content_root / digest[:2] / digest
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise ValueError("artifact content is unavailable") from exc
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("artifact content digest changed")
        return content

    def local_path(self, reference: ArtifactRef) -> Path:
        """Return the immutable local CAS path after verifying its exact digest."""

        self.read_bytes(reference)
        return self._content_root / reference.sha256[:2] / reference.sha256

    async def read(self, reference: ArtifactRef) -> bytes:
        """Implement ``ReadOnlyCapabilityContentStore`` without blocking effects."""

        return self.read_bytes(reference)

    def bind(self, kind: str, identity: str, reference: ArtifactRef) -> ArtifactRef:
        if _NAMESPACE.fullmatch(kind) is None or not identity:
            raise ValueError("artifact binding identity is invalid")
        self.read_bytes(reference)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        target = self._binding_root / kind / f"{digest}.json"
        payload = json.dumps(
            {
                "identity": identity,
                "reference": reference.model_dump(mode="json"),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._write_immutable(target, payload)
        except ValueError as exc:
            raise ValueError("immutable artifact binding changed") from exc
        return reference

    def binding(self, kind: str, identity: str) -> ArtifactRef | None:
        if _NAMESPACE.fullmatch(kind) is None or not identity:
            raise ValueError("artifact binding identity is invalid")
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        target = self._binding_root / kind / f"{digest}.json"
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            if payload.get("identity") != identity:
                raise ValueError("artifact binding identity digest collision")
            reference = ArtifactRef.model_validate(payload["reference"])
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("artifact binding is invalid") from exc
        self.read_bytes(reference)
        return reference

    @staticmethod
    def _write_immutable(target: Path, content: bytes) -> None:
        try:
            with target.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            try:
                existing = target.read_bytes()
            except OSError as exc:
                raise ValueError("immutable artifact could not be verified") from exc
            if existing != content:
                raise ValueError("immutable artifact content changed")

    @staticmethod
    def _require_reference(reference: ArtifactRef) -> None:
        if (
            not reference.uri.startswith("artifact://capability-factory/")
            or reference.uri.rsplit("/", 1)[-1] != reference.sha256
        ):
            raise ValueError("artifact reference is outside the capability store")


class HermesCapabilityCreationAdapter:
    """Validate Hermes creation effects and expose Package-C's creation port."""

    def __init__(
        self,
        *,
        backend: CapabilityCreationBackendPort,
        artifact_store: ContentAddressedArtifactStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._backend = backend
        self._artifacts = artifact_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def preparation_blocks(
        self,
        job: AgentFactoryJobV2,
        creation_job: CreationJobV1,
    ) -> tuple[FactoryEvidenceBlock, FactoryEvidenceBlock]:
        _require_creation_job(job, creation_job)
        binding_id = str(creation_job.creation_job_id)
        existing = self._artifacts.binding("creation-preparation", binding_id)
        if existing is None:
            observation = await self._backend.prepare(job, creation_job)
            _require_preparation(job, creation_job, observation)
            content = observation.model_dump_json().encode("utf-8")
            reference = self._artifacts.put(
                content,
                "application/json",
                namespace="creation-preparation",
            )
            self._artifacts.bind("creation-preparation", binding_id, reference)
        else:
            observation = CapabilityCreationPreparation.model_validate_json(
                self._artifacts.read_bytes(existing)
            )
            _require_preparation(job, creation_job, observation)
        return (
            _hermes_block(
                job,
                phase=FactoryPhase.BLUEPRINT_CREATED,
                role=FactoryRole.AGENT_ARCHITECT,
                occurred_at=observation.occurred_at,
                evidence_refs=(observation.blueprint_evidence_ref,),
                lease_id=observation.blueprint_lease_id,
            ),
            _hermes_block(
                job,
                phase=FactoryPhase.TOOL_CANDIDATE_TESTED,
                role=FactoryRole.TOOL_INTEGRATOR,
                occurred_at=observation.occurred_at + timedelta(microseconds=1),
                evidence_refs=(observation.tool_evidence_ref,),
                lease_id=observation.tool_lease_id,
            ),
        )

    async def submit(self, creation_job: CreationJobV1) -> CreationSubmissionReceipt:
        preparation = self._artifacts.binding(
            "creation-preparation", str(creation_job.creation_job_id)
        )
        if preparation is None:
            raise ValueError("creation submission requires durable Hermes preparation")
        job_content = creation_job.model_dump_json(by_alias=True).encode("utf-8")
        job_ref = self._artifacts.put(
            job_content,
            "application/json",
            namespace="creation-job",
        )
        self._artifacts.bind("creation-job", str(creation_job.creation_job_id), job_ref)
        receipt = await self._backend.submit(creation_job)
        if (
            receipt.creation_job_id != creation_job.creation_job_id
            or receipt.subject_version != creation_job.subject_version
        ):
            raise ValueError("creation submission receipt identity changed")
        return receipt

    async def result(self, creation_job_id: UUID) -> CreationResultV1:
        job_ref = self._artifacts.binding("creation-job", str(creation_job_id))
        if job_ref is None:
            raise ValueError("creation result has no durable submitted job")
        creation_job = CreationJobV1.model_validate_json(self._artifacts.read_bytes(job_ref))
        existing = self._artifacts.binding("creation-result", str(creation_job_id))
        if existing is None:
            result = await self._backend.result(creation_job_id)
            _require_creation_result(creation_job, result)
            result_ref = self._artifacts.put(
                result.model_dump_json(by_alias=True).encode("utf-8"),
                "application/json",
                namespace="creation-result",
            )
            self._artifacts.bind("creation-result", str(creation_job_id), result_ref)
            return result
        result = CreationResultV1.model_validate_json(self._artifacts.read_bytes(existing))
        _require_creation_result(creation_job, result)
        return result

    async def completion_block(
        self,
        job: AgentFactoryJobV2,
        result: CreationResultV1,
    ) -> FactoryEvidenceBlock:
        preparation_ref = self._artifacts.binding(
            "creation-preparation", str(result.creation_job_id)
        )
        result_ref = self._artifacts.binding("creation-result", str(result.creation_job_id))
        if preparation_ref is None or result_ref is None:
            raise ValueError("creation completion lacks durable preparation or result")
        observation = CapabilityCreationPreparation.model_validate_json(
            self._artifacts.read_bytes(preparation_ref)
        )
        if (
            observation.factory_job_id != job.job_id
            or observation.correlation_id != job.correlation_id
            or result.correlation_id != job.correlation_id
            or result.subject_version != job.subject_version
            or result.status != "succeeded"
            or result.package_manifest_ref is None
        ):
            raise ValueError("creation completion identity changed")
        manifest_ref = ArtifactRef.model_validate(
            result.package_manifest_ref.model_dump(mode="json")
        )
        return _hermes_block(
            job,
            phase=FactoryPhase.AGENT_CODE_CREATED,
            role=FactoryRole.TOOL_INTEGRATOR,
            occurred_at=self._utc_now(),
            evidence_refs=(manifest_ref,),
            lease_id=observation.code_lease_id,
        )

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("capability creation clock must be UTC")
        return value


class CaptainCapabilityReleaseReceipt(_FrozenContract):
    """Canonical receipt structurally implementing Package-C's receipt contract."""

    record: CapabilityReleaseEvidenceV1
    reference: ArtifactRef

    @model_validator(mode="after")
    def require_canonical_reference(self) -> "CaptainCapabilityReleaseReceipt":
        content = canonical_capability_release_evidence_bytes(self.record)
        if (
            self.reference.media_type != "application/json"
            or self.reference.sha256 != hashlib.sha256(content).hexdigest()
            or self.reference.uri.rsplit("/", 1)[-1] != self.reference.sha256
        ):
            raise ValueError("release receipt reference is not canonical")
        return self


class CaptainEvidenceIssuerAdapter:
    """Derive Captain evidence from exact team observations and persist it once."""

    def __init__(
        self,
        *,
        executor: CapabilityReleaseExecutorPort,
        artifact_store: ContentAddressedArtifactStore,
    ) -> None:
        self._executor = executor
        self._artifacts = artifact_store

    async def run(
        self,
        job: AgentFactoryJobV2,
        creation_result: CreationResultV1,
        candidate: ForgeCapabilityPackageCandidateV1,
        run_number: int,
    ) -> CaptainCapabilityReleaseReceipt | None:
        _require_release_inputs(job, creation_result, candidate, run_number)
        binding_id = f"{job.job_id}/{run_number}"
        existing = self._artifacts.binding("release-run", binding_id)
        if existing is not None:
            record = CapabilityReleaseEvidenceV1.model_validate_json(
                self._artifacts.read_bytes(existing)
            )
            _require_release_record(job, creation_result, candidate, record, run_number)
            return CaptainCapabilityReleaseReceipt(record=record, reference=existing)
        observation = await self._executor.execute(
            job,
            creation_result,
            candidate,
            run_number,
        )
        if observation is None:
            return None
        _require_release_observation(job, candidate, observation, run_number)
        for assertion in observation.assertion_results:
            for reference in assertion.evidence_refs:
                self._artifacts.read_bytes(reference)
        for holdout in observation.private_holdout_evidence:
            self._artifacts.read_bytes(holdout.evidence_ref)
        for prior_number in range(1, run_number):
            prior_ref = self._artifacts.binding(
                "release-run", f"{job.job_id}/{prior_number}"
            )
            if prior_ref is None:
                raise ValueError("release evidence run sequence has a gap")
            prior = CapabilityReleaseEvidenceV1.model_validate_json(
                self._artifacts.read_bytes(prior_ref)
            )
            if prior.run_id == observation.run_id:
                raise ValueError("release evidence run IDs must be distinct")
        assert creation_result.package_manifest_ref is not None
        record = CapabilityReleaseEvidenceV1(
            schema_name="captain.capability-release-evidence.v1",
            run_id=observation.run_id,
            run_number=run_number,
            factory_job_id=job.job_id,
            creation_job_id=creation_result.creation_job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=creation_result.attempt,
            capability_id=candidate.capability_id,
            capability_version=observation.capability_version,
            candidate_manifest_sha256=creation_result.package_manifest_ref.sha256,
            package_archive_sha256=candidate.source_ref.sha256,
            extracted_tree_sha256=observation.extracted_tree_sha256,
            kind=observation.kind,
            outcome=observation.outcome,
            producer="captain",
            assertion_results=observation.assertion_results,
            recovery_id=observation.recovery_id,
            recovery_assertion_id=observation.recovery_assertion_id,
            private_holdout_evidence=observation.private_holdout_evidence,
        )
        content = canonical_capability_release_evidence_bytes(record)
        reference = self._artifacts.put(
            content,
            "application/json",
            namespace="release-evidence",
        )
        observation_ref = self._artifacts.put(
            observation.model_dump_json().encode("utf-8"),
            "application/json",
            namespace="release-observation",
        )
        self._artifacts.bind("release-observation", binding_id, observation_ref)
        self._artifacts.bind("release-run", binding_id, reference)
        return CaptainCapabilityReleaseReceipt(record=record, reference=reference)

    async def lifecycle_blocks(
        self,
        job: AgentFactoryJobV2,
        receipts: tuple[CaptainCapabilityReleaseReceipt, ...],
    ) -> tuple[FactoryEvidenceBlock, FactoryEvidenceBlock, FactoryEvidenceBlock]:
        if tuple(item.record.run_number for item in receipts) != (1, 2, 3, 4):
            raise ValueError("lifecycle evidence requires recovery then three normal runs")
        observations: list[CapabilityReleaseObservation] = []
        for receipt in receipts:
            _require_lifecycle_record(job, receipt.record)
            if self._artifacts.read_bytes(receipt.reference) != canonical_capability_release_evidence_bytes(
                receipt.record
            ):
                raise ValueError("release evidence artifact bytes changed")
            observation_ref = self._artifacts.binding(
                "release-observation",
                f"{job.job_id}/{receipt.record.run_number}",
            )
            if observation_ref is None:
                raise ValueError("release evidence lacks its provider observation")
            observations.append(
                CapabilityReleaseObservation.model_validate_json(
                    self._artifacts.read_bytes(observation_ref)
                )
            )
        lease_sets = {
            (
                item.build_lease_id,
                item.tester_lease_id,
                item.quality_lease_id,
            )
            for item in observations
        }
        if len(lease_sets) != 1:
            raise ValueError("release lifecycle lease identity changed across runs")
        build_lease, tester_lease, quality_lease = next(iter(lease_sets))
        evidence_refs = tuple(item.reference for item in receipts)
        occurred_at = max(item.occurred_at for item in observations)
        return (
            _hermes_block(
                job,
                phase=FactoryPhase.BUILD_PASSED,
                role=FactoryRole.TOOL_INTEGRATOR,
                occurred_at=occurred_at,
                evidence_refs=evidence_refs,
                lease_id=build_lease,
            ),
            _hermes_block(
                job,
                phase=FactoryPhase.REAL_CASE_EVIDENCE,
                role=FactoryRole.REAL_CASE_TESTER,
                occurred_at=occurred_at + timedelta(microseconds=1),
                evidence_refs=evidence_refs,
                lease_id=tester_lease,
                assertion_ids=job.acceptance_assertion_ids,
            ),
            _hermes_block(
                job,
                phase=FactoryPhase.QUALITY_REVIEWED,
                role=FactoryRole.QUALITY_WARDEN,
                occurred_at=occurred_at + timedelta(microseconds=2),
                evidence_refs=evidence_refs,
                lease_id=quality_lease,
                assertion_ids=job.acceptance_assertion_ids,
            ),
        )


def _require_creation_job(job: AgentFactoryJobV2, creation: CreationJobV1) -> None:
    if (
        creation.factory_job_id != job.job_id
        or creation.correlation_id != job.correlation_id
        or creation.causation_id != job.event_id
        or creation.subject_version != job.subject_version
        or creation.input_ref != job.input_ref
        or creation.compiled_spec_ref != job.compiled_spec_ref
        or creation.dependency_graph_ref != job.dependency_graph_ref
        or creation.public_assertion_ids != job.acceptance_assertion_ids
        or creation.deadline_at != job.deadline_at
    ):
        raise ValueError("creation job is not bound to the Captain job")


def _require_preparation(
    job: AgentFactoryJobV2,
    creation: CreationJobV1,
    observation: CapabilityCreationPreparation,
) -> None:
    if (
        observation.factory_job_id != job.job_id
        or observation.creation_job_id != creation.creation_job_id
        or observation.correlation_id != job.correlation_id
        or observation.subject_version != job.subject_version
        or observation.attempt != creation.attempt
        or not job.occurred_at <= observation.occurred_at < job.deadline_at
    ):
        raise ValueError("Hermes creation preparation identity changed")


def _require_creation_result(
    creation: CreationJobV1,
    result: CreationResultV1,
) -> None:
    if (
        result.creation_job_id != creation.creation_job_id
        or result.correlation_id != creation.correlation_id
        or result.subject_version != creation.subject_version
        or result.attempt != creation.attempt
    ):
        raise ValueError("creation result identity changed")


def _require_release_inputs(
    job: AgentFactoryJobV2,
    result: CreationResultV1,
    candidate: ForgeCapabilityPackageCandidateV1,
    run_number: int,
) -> None:
    if run_number not in {1, 2, 3, 4}:
        raise ValueError("release evidence run number must be one through four")
    if (
        result.status != "succeeded"
        or result.package_manifest_ref is None
        or result.correlation_id != job.correlation_id
        or result.subject_version != job.subject_version
        or candidate.factory_job_id != job.job_id
        or candidate.creation_job_id != result.creation_job_id
        or candidate.correlation_id != job.correlation_id
        or candidate.subject_version != job.subject_version
        or candidate.attempt != result.attempt
        or candidate.capability_id != job.required_capability
    ):
        raise ValueError("release evidence inputs are not bound")


def _require_release_observation(
    job: AgentFactoryJobV2,
    candidate: ForgeCapabilityPackageCandidateV1,
    observation: CapabilityReleaseObservation,
    run_number: int,
) -> None:
    expected_kind = "recovery" if run_number == 1 else "normal"
    expected_outcome = "expected_failure_recovered" if run_number == 1 else "succeeded"
    assertion_ids = tuple(item.assertion_id for item in observation.assertion_results)
    if (
        observation.kind != expected_kind
        or observation.outcome != expected_outcome
        or observation.capability_version != candidate.capability_version
        or assertion_ids != job.acceptance_assertion_ids
        or any(item.status != "passed" for item in observation.assertion_results)
        or not job.occurred_at <= observation.occurred_at < job.deadline_at
    ):
        raise ValueError("release observation does not satisfy the Captain run contract")
    if run_number == 1:
        holdout_ids = tuple(item.holdout_id for item in observation.private_holdout_evidence)
        expected_holdouts = tuple(item.holdout_id for item in job.private_holdout_refs)
        if (
            observation.recovery_assertion_id not in job.acceptance_assertion_ids
            or holdout_ids != expected_holdouts
            or any(item.status != "passed" for item in observation.private_holdout_evidence)
        ):
            raise ValueError("controlled recovery evidence is incomplete")
    elif observation.private_holdout_evidence:
        raise ValueError("normal release evidence cannot expose private holdout results")


def _require_release_record(
    job: AgentFactoryJobV2,
    result: CreationResultV1,
    candidate: ForgeCapabilityPackageCandidateV1,
    record: CapabilityReleaseEvidenceV1,
    run_number: int,
) -> None:
    _require_release_inputs(job, result, candidate, run_number)
    if (
        record.run_number != run_number
        or record.factory_job_id != job.job_id
        or record.creation_job_id != result.creation_job_id
        or record.correlation_id != job.correlation_id
        or record.subject_version != job.subject_version
        or record.attempt != result.attempt
        or record.capability_id != candidate.capability_id
        or record.capability_version != candidate.capability_version
        or record.candidate_manifest_sha256 != result.package_manifest_ref.sha256
        or record.package_archive_sha256 != candidate.source_ref.sha256
    ):
        raise ValueError("release evidence record identity changed")


def _require_lifecycle_record(
    job: AgentFactoryJobV2,
    record: CapabilityReleaseEvidenceV1,
) -> None:
    expected_kind = "recovery" if record.run_number == 1 else "normal"
    expected_outcome = (
        "expected_failure_recovered" if record.run_number == 1 else "succeeded"
    )
    if (
        record.run_number not in {1, 2, 3, 4}
        or record.factory_job_id != job.job_id
        or record.correlation_id != job.correlation_id
        or record.subject_version != job.subject_version
        or record.capability_id != job.required_capability
        or record.kind != expected_kind
        or record.outcome != expected_outcome
        or record.producer != "captain"
        or tuple(item.assertion_id for item in record.assertion_results)
        != job.acceptance_assertion_ids
        or any(item.status != "passed" for item in record.assertion_results)
    ):
        raise ValueError("release lifecycle record identity changed")
    if record.run_number == 1:
        if (
            tuple(item.holdout_id for item in record.private_holdout_evidence)
            != tuple(item.holdout_id for item in job.private_holdout_refs)
            or any(item.status != "passed" for item in record.private_holdout_evidence)
        ):
            raise ValueError("release lifecycle recovery evidence changed")
    elif record.private_holdout_evidence:
        raise ValueError("normal lifecycle evidence contains private holdouts")


def _hermes_block(
    job: AgentFactoryJobV2,
    *,
    phase: FactoryPhase,
    role: FactoryRole,
    occurred_at: datetime,
    evidence_refs: tuple[ArtifactRef, ...],
    lease_id: str,
    assertion_ids: tuple[str, ...] = (),
) -> FactoryEvidenceBlock:
    return FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id=uuid5(job.event_id, f"capability-live-adapter:{phase.value}:1"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        causation_id=job.event_id,
        occurred_at=occurred_at,
        producer="hermes",
        subject_version=job.subject_version,
        attempt=1,
        phase=phase,
        role=role,
        status=FactoryBlockStatus.SUCCEEDED,
        evidence_refs=evidence_refs,
        assertion_ids=assertion_ids,
        lease_id=lease_id,
    )
