"""Named, resumable step adapter around the legacy SwarmPipeline."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from io import BytesIO
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Callable, Protocol, get_type_hints
import zipfile

from pydantic import BaseModel, ConfigDict, Field

from .contracts import (
    ArtifactRef,
    CodexBuildReceiptV1,
    CreationFailure,
    CreationJobV1,
    CreationJobV2,
    CreationPackageManifestV1,
    CreationPackageManifestV2,
    CreationResultV1,
    ForgeBuildSkillUsageReceiptV1,
)
from .runner import StepOutcome


class SwarmStep(str, Enum):
    MANAGER = "manager"
    CATALOG = "catalog"
    ARCHITECT = "architect"
    CODER = "coder"
    REVIEWER = "reviewer"
    TESTER = "tester"
    VALIDATOR = "validator"
    BUILDER = "builder"
    EXECUTOR = "executor"
    OUTPUT_EVALUATION = "output_evaluation"
    TODO_IMPLEMENTATION = "todo_implementation"
    TOOLFORGE = "toolforge"
    FEEDBACK_LOOP = "feedback_loop"
    EVALUATION_REPORT = "evaluation_report"
    EXPORT = "export"


PIPELINE_STEP_ORDER = tuple(SwarmStep)


class SwarmSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    creation_job_id: object
    completed_steps: tuple[str, ...] = ()
    output_digests: dict[str, str] = Field(default_factory=dict)
    artifact_bindings: dict[str, ArtifactRef] = Field(default_factory=dict)
    decisions: dict[str, str] = Field(default_factory=dict)
    external_receipt_ids: dict[str, str] = Field(default_factory=dict)
    package_manifest_ref: ArtifactRef | None = None
    skill_usage_receipt_ref: ArtifactRef | None = None


@dataclass(frozen=True)
class CreationExportBundle:
    """Typed bytes produced by an upgraded Builder/Export implementation."""

    source_archive: bytes
    candidate_manifest: dict[str, Any]
    skill_usage_receipt: bytes
    captain_sealed_source: bool = False


class CreationExportReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    schema_name: str = Field(
        default="minibook.creation-export-receipt.v1",
        alias="schema",
        serialization_alias="schema",
        pattern=r"^minibook\.creation-export-receipt\.v1$",
    )
    receipt_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_manifest_ref: ArtifactRef
    candidate_manifest_ref: ArtifactRef
    source_archive_ref: ArtifactRef
    skill_usage_receipt_ref: ArtifactRef


class CreationExportReceiptV2(CreationExportReceiptV1):
    schema_name: str = Field(
        default="minibook.creation-export-receipt.v2",
        alias="schema",
        serialization_alias="schema",
        pattern=r"^minibook\.creation-export-receipt\.v2$",
    )
    codex_build_receipt_ref: ArtifactRef


class CreationArtifactSink(Protocol):
    def put(self, content: bytes, media_type: str, *, namespace: str) -> object: ...

    def read_bytes(self, reference: object) -> bytes: ...


class CreationArtifactPublisher(Protocol):
    def publish(
        self,
        job: CreationJobV1 | CreationJobV2,
        bundle: CreationExportBundle,
    ) -> CreationExportReceiptV1 | CreationExportReceiptV2: ...

    def accept_receipt(
        self,
        job: CreationJobV1 | CreationJobV2,
        receipt: CreationExportReceiptV1 | CreationExportReceiptV2,
    ) -> CreationExportReceiptV1 | CreationExportReceiptV2: ...


class ContentAddressedCreationArtifactPublisher:
    """Publish real export bytes and return only digest-bound immutable refs."""

    def __init__(self, sink: CreationArtifactSink) -> None:
        self._sink = sink

    def publish(
        self,
        job: CreationJobV1 | CreationJobV2,
        bundle: CreationExportBundle,
    ) -> CreationExportReceiptV1 | CreationExportReceiptV2:
        is_v2 = isinstance(job, CreationJobV2)
        if is_v2 != bundle.captain_sealed_source:
            raise ValueError("creation source provenance does not match the creation job")
        self._validate_archive(
            bundle.source_archive,
            forbid_external_skill_receipt=is_v2,
            require_candidate_manifest=is_v2,
        )
        if is_v2 and hashlib.sha256(bundle.source_archive).hexdigest() != (
            job.source_archive_ref.sha256
        ):
            raise ValueError("Captain source archive digest does not match creation job")
        if is_v2:
            self._require_exact_captain_source(
                job,
                bundle.source_archive,
                candidate_manifest=bundle.candidate_manifest,
            )
        source_ref = self._put_checked(
            bundle.source_archive,
            "application/zip",
            namespace="forge-source",
        )
        manifest = dict(bundle.candidate_manifest)
        existing_source = manifest.get("source_archive_ref")
        serialized_source = source_ref.model_dump(mode="json")
        if existing_source is not None and existing_source != serialized_source:
            raise ValueError("candidate manifest source archive binding changed")
        manifest["source_archive_ref"] = serialized_source
        if manifest.get("schema", manifest.get("schema_name")) != "captain.factory-candidate.v1":
            raise ValueError("candidate manifest schema is invalid")
        candidate_bytes = _canonical_json(manifest)
        candidate_ref = self._put_checked(
            candidate_bytes,
            "application/json",
            namespace="forge-candidate-manifest",
        )
        skill_usage_receipt = self._parse_skill_usage_receipt(
            bundle.skill_usage_receipt
        )
        self._require_exact_skill_usage_receipt(job, skill_usage_receipt)
        skill_ref = self._put_checked(
            bundle.skill_usage_receipt,
            "application/json",
            namespace="forge-skill-receipt",
        )
        codex_receipt_ref: ArtifactRef | None = None
        package: CreationPackageManifestV1 | CreationPackageManifestV2
        if is_v2:
            codex_receipt_bytes = _canonical_json(
                job.codex_build_receipt.model_dump(mode="json", by_alias=True)
            )
            if hashlib.sha256(codex_receipt_bytes).hexdigest() != (
                job.codex_build_receipt_ref.sha256
            ):
                raise ValueError("Codex build receipt canonical digest changed")
            codex_receipt_ref = self._put_checked(
                codex_receipt_bytes,
                "application/json",
                namespace="captain-codex-build-receipt",
            )
            if (
                codex_receipt_ref.sha256 != job.codex_build_receipt_ref.sha256
                or codex_receipt_ref.media_type
                != job.codex_build_receipt_ref.media_type
            ):
                raise ValueError("Codex build receipt CAS binding changed")
            package = CreationPackageManifestV2(
                creation_job_id=job.creation_job_id,
                factory_job_id=job.factory_job_id,
                correlation_id=job.correlation_id,
                subject_version=job.subject_version,
                attempt=job.attempt,
                candidate_manifest_ref=candidate_ref,
                source_archive_ref=source_ref,
                skill_usage_receipt_ref=skill_ref,
                codex_build_receipt_ref=codex_receipt_ref,
            )
        else:
            package = CreationPackageManifestV1(
                creation_job_id=job.creation_job_id,
                factory_job_id=job.factory_job_id,
                correlation_id=job.correlation_id,
                subject_version=job.subject_version,
                attempt=job.attempt,
                candidate_manifest_ref=candidate_ref,
                source_archive_ref=source_ref,
                skill_usage_receipt_ref=skill_ref,
            )
        package_ref = self._put_checked(
            _canonical_json(package.model_dump(mode="json", by_alias=True)),
            "application/json",
            namespace="forge-package-manifest",
        )
        receipt_values = {
            "receipt_id": self._receipt_id(
                job,
                package_ref=package_ref,
                candidate_ref=candidate_ref,
                source_ref=source_ref,
                skill_ref=skill_ref,
                codex_receipt_ref=codex_receipt_ref,
            ),
            "package_manifest_ref": package_ref,
            "candidate_manifest_ref": candidate_ref,
            "source_archive_ref": source_ref,
            "skill_usage_receipt_ref": skill_ref,
        }
        receipt: CreationExportReceiptV1 | CreationExportReceiptV2
        if codex_receipt_ref is None:
            receipt = CreationExportReceiptV1(**receipt_values)
        else:
            receipt = CreationExportReceiptV2(
                **receipt_values,
                codex_build_receipt_ref=codex_receipt_ref,
            )
        return self.accept_receipt(job, receipt)

    def accept_receipt(
        self,
        job: CreationJobV1 | CreationJobV2,
        receipt: CreationExportReceiptV1 | CreationExportReceiptV2,
    ) -> CreationExportReceiptV1 | CreationExportReceiptV2:
        receipt_type = (
            CreationExportReceiptV2
            if isinstance(job, CreationJobV2)
            else CreationExportReceiptV1
        )
        canonical = receipt_type.model_validate(
            receipt.model_dump(mode="json", by_alias=True)
        )
        package_bytes = self._read_checked(
            canonical.package_manifest_ref,
            "application/json",
            label="creation package manifest",
        )
        candidate_bytes = self._read_checked(
            canonical.candidate_manifest_ref,
            "application/json",
            label="candidate manifest",
        )
        source_bytes = self._read_checked(
            canonical.source_archive_ref,
            "application/zip",
            label="candidate source archive",
        )
        skill_bytes = self._read_checked(
            canonical.skill_usage_receipt_ref,
            "application/json",
            label="skill usage receipt",
        )
        codex_receipt_bytes: bytes | None = None
        if isinstance(canonical, CreationExportReceiptV2):
            codex_receipt_bytes = self._read_checked(
                canonical.codex_build_receipt_ref,
                "application/json",
                label="Codex build receipt",
            )
        try:
            package_type = (
                CreationPackageManifestV2
                if isinstance(job, CreationJobV2)
                else CreationPackageManifestV1
            )
            package = package_type.model_validate_json(package_bytes)
        except ValueError as exc:
            raise ValueError("creation package manifest is invalid") from exc
        if (
            package.creation_job_id != job.creation_job_id
            or package.factory_job_id != job.factory_job_id
            or package.correlation_id != job.correlation_id
            or package.subject_version != job.subject_version
            or package.attempt != job.attempt
        ):
            raise ValueError("creation package does not match replay job")
        if (
            package.candidate_manifest_ref != canonical.candidate_manifest_ref
            or package.source_archive_ref != canonical.source_archive_ref
        ):
            raise ValueError("creation package artifact bindings changed")
        if (
            package.skill_usage_receipt_ref
            != canonical.skill_usage_receipt_ref
        ):
            raise ValueError("creation package skill usage receipt binding changed")
        if isinstance(job, CreationJobV2):
            if (
                not isinstance(package, CreationPackageManifestV2)
                or not isinstance(canonical, CreationExportReceiptV2)
                or package.codex_build_receipt_ref
                != canonical.codex_build_receipt_ref
                or codex_receipt_bytes is None
            ):
                raise ValueError("Codex build receipt package binding changed")
            try:
                persisted_codex_receipt = CodexBuildReceiptV1.model_validate_json(
                    codex_receipt_bytes
                )
            except ValueError as exc:
                raise ValueError("Codex build receipt is invalid") from exc
            if (
                persisted_codex_receipt != job.codex_build_receipt
                or canonical.codex_build_receipt_ref.sha256
                != job.codex_build_receipt_ref.sha256
                or canonical.codex_build_receipt_ref.media_type
                != job.codex_build_receipt_ref.media_type
            ):
                raise ValueError("Codex build receipt does not match creation job")
        self._validate_json_object(candidate_bytes, "candidate manifest")
        is_v2 = isinstance(job, CreationJobV2)
        self._validate_archive(
            source_bytes,
            forbid_external_skill_receipt=is_v2,
            require_candidate_manifest=is_v2,
        )
        if is_v2:
            self._require_exact_captain_source(job, source_bytes)
        skill_usage_receipt = self._parse_skill_usage_receipt(skill_bytes)
        self._require_exact_skill_usage_receipt(job, skill_usage_receipt)
        expected_receipt_id = self._receipt_id(
            job,
            package_ref=canonical.package_manifest_ref,
            candidate_ref=canonical.candidate_manifest_ref,
            source_ref=canonical.source_archive_ref,
            skill_ref=canonical.skill_usage_receipt_ref,
            codex_receipt_ref=(
                canonical.codex_build_receipt_ref
                if isinstance(canonical, CreationExportReceiptV2)
                else None
            ),
        )
        if canonical.receipt_id != expected_receipt_id:
            raise ValueError("creation export receipt ID does not match CAS bindings")
        return canonical

    def _read_checked(
        self,
        reference: ArtifactRef,
        media_type: str,
        *,
        label: str,
    ) -> bytes:
        reader = getattr(self._sink, "read_bytes", None)
        if not callable(reader):
            raise ValueError("creation artifact CAS read authority is unavailable")
        try:
            content = reader(self._native_read_reference(reader, reference))
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError(f"{label} is unavailable from CAS") from exc
        if (
            not isinstance(content, bytes)
            or reference.media_type != media_type
            or reference.sha256 != hashlib.sha256(content).hexdigest()
        ):
            raise ValueError(f"{label} is not bound to its CAS reference")
        return content

    @staticmethod
    def _native_read_reference(
        reader: Callable[[object], bytes],
        reference: ArtifactRef,
    ) -> object:
        """Rehydrate a sink-owned reference without importing its contract package."""

        try:
            reference_type = get_type_hints(reader).get("reference")
        except (NameError, TypeError):
            reference_type = None
        validator = getattr(reference_type, "model_validate", None)
        if not callable(validator):
            return reference
        return validator(reference.model_dump(mode="json"))

    @staticmethod
    def _receipt_id(
        job: CreationJobV1,
        *,
        package_ref: ArtifactRef,
        candidate_ref: ArtifactRef,
        source_ref: ArtifactRef,
        skill_ref: ArtifactRef,
        codex_receipt_ref: ArtifactRef | None = None,
    ) -> str:
        digest_parts = [
            str(job.creation_job_id),
            package_ref.sha256,
            candidate_ref.sha256,
            source_ref.sha256,
            skill_ref.sha256,
        ]
        if codex_receipt_ref is not None:
            digest_parts.append(codex_receipt_ref.sha256)
        return hashlib.sha256(
            "|".join(digest_parts).encode("utf-8")
        ).hexdigest()

    def _put_checked(
        self,
        content: bytes,
        media_type: str,
        *,
        namespace: str,
    ) -> ArtifactRef:
        reference = self._sink.put(content, media_type, namespace=namespace)
        dump = getattr(reference, "model_dump", None)
        if not callable(dump):
            raise ValueError("artifact sink returned an invalid reference")
        parsed = ArtifactRef.model_validate(dump(mode="json"))
        if (
            parsed.sha256 != hashlib.sha256(content).hexdigest()
            or parsed.media_type != media_type
        ):
            raise ValueError("artifact sink returned an unbound reference")
        return parsed

    @staticmethod
    def _validate_archive(
        content: bytes,
        *,
        forbid_external_skill_receipt: bool = False,
        require_candidate_manifest: bool = False,
    ) -> None:
        if not content:
            raise ValueError("candidate source archive is empty")
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                names = archive.namelist()
                if not names:
                    raise ValueError("candidate source archive is empty")
                for name in names:
                    if (
                        "\\" in name
                        or (len(name) >= 2 and name[0].isalpha() and name[1] == ":")
                    ):
                        raise ValueError("candidate source archive path is unsafe")
                    path = PurePosixPath(name.replace("\\", "/"))
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError("candidate source archive path is unsafe")
                folded = {name.rstrip("/").casefold() for name in names}
                if len(folded) != len(names):
                    raise ValueError("candidate source archive paths are not unique")
                if require_candidate_manifest and "factory-candidate.json" not in names:
                    raise ValueError("candidate source archive has no candidate manifest")
                if (
                    forbid_external_skill_receipt
                    and "evidence/hermes-factory-skill-usage-receipt.json" in folded
                ):
                    raise ValueError("candidate source archive contains external skill receipt")
        except zipfile.BadZipFile as exc:
            raise ValueError("candidate source archive is not a ZIP") from exc

    @staticmethod
    def _require_exact_captain_source(
        job: CreationJobV2,
        source_archive: bytes,
        *,
        candidate_manifest: dict[str, Any] | None = None,
    ) -> None:
        if (
            job.source_archive_ref.media_type != "application/zip"
            or hashlib.sha256(source_archive).hexdigest()
            != job.source_archive_ref.sha256
        ):
            raise ValueError("Captain source archive digest does not match creation job")
        try:
            with zipfile.ZipFile(BytesIO(source_archive)) as archive:
                manifest_bytes = archive.read("factory-candidate.json")
        except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ValueError("Captain source archive candidate manifest is unavailable") from exc
        if hashlib.sha256(manifest_bytes).hexdigest() != (
            job.codex_build_receipt.candidate_manifest_ref.sha256
        ):
            raise ValueError("Captain candidate manifest digest does not match build receipt")
        if candidate_manifest is not None:
            try:
                archived_manifest = json.loads(manifest_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Captain candidate manifest is invalid JSON") from exc
            if not isinstance(archived_manifest, dict) or archived_manifest != candidate_manifest:
                raise ValueError("Captain candidate manifest changed outside the source archive")

    @staticmethod
    def _validate_json_object(content: bytes, label: str) -> None:
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be a JSON object")

    @staticmethod
    def _parse_skill_usage_receipt(
        content: bytes,
    ) -> ForgeBuildSkillUsageReceiptV1:
        try:
            return ForgeBuildSkillUsageReceiptV1.model_validate_json(content)
        except ValueError as exc:
            raise ValueError("skill usage receipt is invalid") from exc

    @staticmethod
    def _require_exact_skill_usage_receipt(
        job: CreationJobV1,
        receipt: ForgeBuildSkillUsageReceiptV1,
    ) -> None:
        if (
            receipt.creation_job_id != job.creation_job_id
            or receipt.factory_job_id != job.factory_job_id
            or receipt.correlation_id != job.correlation_id
            or receipt.subject_version != job.subject_version
            or receipt.attempt != job.attempt
            or receipt.idempotency_key != job.idempotency_key
            or receipt.released_skill != job.released_skill
            or receipt.public_assertion_ids != job.public_assertion_ids
        ):
            raise ValueError("skill usage receipt does not match creation job")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


_KNOWN_FAILURES = {
    "DocumentationUnavailable": "documentation_unavailable",
    "ToolResolutionError": "tool_unresolved",
    "CodexExecutionError": "codex_failed",
    "N8nExecutionError": "n8n_failed",
    "BuildError": "build_failed",
    "ValidationError": "validation_failed",
}


def translate_creation_failure(exc: Exception) -> CreationFailure:
    code = _KNOWN_FAILURES.get(type(exc).__name__, "internal_error")
    return CreationFailure(
        code=code,
        summary="creation step failed",
        exception_type=type(exc).__name__,
    )


class SwarmPipelineAdapter:
    steps = tuple(step.value for step in PIPELINE_STEP_ORDER)
    effectful_steps = frozenset(
        {
            SwarmStep.CATALOG.value,
            SwarmStep.CODER.value,
            SwarmStep.BUILDER.value,
            SwarmStep.EXECUTOR.value,
            SwarmStep.TODO_IMPLEMENTATION.value,
            SwarmStep.TOOLFORGE.value,
            SwarmStep.EXPORT.value,
        }
    )

    def __init__(
        self,
        pipeline_factory: Callable[[SwarmSnapshot], Any],
        *,
        session: object,
        artifact_publisher: CreationArtifactPublisher | None = None,
    ) -> None:
        self._pipeline_factory = pipeline_factory
        self._session = session
        self._artifact_publisher = artifact_publisher

    async def run_step(
        self,
        job: CreationJobV1 | CreationJobV2,
        step: str,
        prior_snapshot: dict[str, Any],
        effect_key: str,
        accepted_effect: dict[str, Any] | None,
    ) -> StepOutcome:
        named_step = SwarmStep(step)
        prior = SwarmSnapshot.model_validate(prior_snapshot)
        pipeline = self._pipeline_factory(prior)
        output: Any = accepted_effect
        if accepted_effect is None:
            output = await self._dispatch(pipeline, named_step, job)
        export_receipt = self._export_receipt(job, named_step, output, accepted_effect)
        digest_output = (
            export_receipt.model_dump(mode="json", by_alias=True)
            if export_receipt is not None
            else output
        )
        digest = hashlib.sha256(
            json.dumps(digest_output, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        completed = tuple(dict.fromkeys((*prior.completed_steps, named_step.value)))
        receipts = dict(prior.external_receipt_ids)
        receipt = None
        if named_step.value in self.effectful_steps:
            receipt = (
                export_receipt.model_dump(mode="json", by_alias=True)
                if export_receipt is not None
                else accepted_effect
            ) or {
                "receipt_id": effect_key,
                "output_sha256": digest,
            }
            receipts[named_step.value] = str(receipt["receipt_id"])
        artifact_bindings = dict(prior.artifact_bindings)
        package_manifest_ref = prior.package_manifest_ref
        skill_usage_receipt_ref = prior.skill_usage_receipt_ref
        if export_receipt is not None:
            artifact_bindings.update(
                {
                    "candidate_manifest": export_receipt.candidate_manifest_ref,
                    "source_archive": export_receipt.source_archive_ref,
                }
            )
            if isinstance(export_receipt, CreationExportReceiptV2):
                artifact_bindings["codex_build_receipt"] = (
                    export_receipt.codex_build_receipt_ref
                )
            package_manifest_ref = export_receipt.package_manifest_ref
            skill_usage_receipt_ref = export_receipt.skill_usage_receipt_ref
        snapshot = prior.model_copy(
            update={
                "completed_steps": completed,
                "output_digests": prior.output_digests | {named_step.value: digest},
                "external_receipt_ids": receipts,
                "artifact_bindings": artifact_bindings,
                "package_manifest_ref": package_manifest_ref,
                "skill_usage_receipt_ref": skill_usage_receipt_ref,
            }
        )
        return StepOutcome(snapshot=snapshot.model_dump(mode="json"), effect_receipt=receipt)

    def _export_receipt(
        self,
        job: CreationJobV1 | CreationJobV2,
        step: SwarmStep,
        output: Any,
        accepted_effect: dict[str, Any] | None,
    ) -> CreationExportReceiptV1 | CreationExportReceiptV2 | None:
        if step is not SwarmStep.EXPORT:
            return None
        if accepted_effect is not None:
            if self._artifact_publisher is None:
                raise ValueError("creation artifact CAS read authority is unavailable")
            receipt_type = (
                CreationExportReceiptV2
                if isinstance(job, CreationJobV2)
                else CreationExportReceiptV1
            )
            receipt = receipt_type.model_validate(accepted_effect)
            return self._artifact_publisher.accept_receipt(job, receipt)
        if not isinstance(output, CreationExportBundle):
            # A creation export must expose verified local bytes. Any legacy
            # exporter result remains deliberately non-promotable.
            return None
        if self._artifact_publisher is None:
            return None
        return self._artifact_publisher.publish(job, output)

    async def _dispatch(
        self,
        pipeline: Any,
        step: SwarmStep,
        job: CreationJobV1 | CreationJobV2,
    ) -> Any:
        if step is SwarmStep.MANAGER:
            return await pipeline.step_swarm_manager(self._session, str(job.input_ref.uri))
        method_names = {
            SwarmStep.CATALOG: "step_catalog",
            SwarmStep.ARCHITECT: "step_architect",
            SwarmStep.CODER: "step_coder",
            SwarmStep.REVIEWER: "step_reviewer",
            SwarmStep.TESTER: "step_tester",
            SwarmStep.VALIDATOR: "step_validator",
            SwarmStep.BUILDER: "step_builder",
            SwarmStep.EXECUTOR: "step_executor",
            SwarmStep.OUTPUT_EVALUATION: "step_output_eval",
            SwarmStep.TODO_IMPLEMENTATION: "step_todo_implement",
            SwarmStep.TOOLFORGE: "step_toolforge",
            SwarmStep.FEEDBACK_LOOP: "step_feedback_loop",
            SwarmStep.EVALUATION_REPORT: "step_eval_reporter",
            SwarmStep.EXPORT: "step_creation_export",
        }
        method = getattr(pipeline, method_names[step])
        return await method(self._session)

    def assemble_result(
        self, job: CreationJobV1 | CreationJobV2, snapshot: dict[str, Any]
    ) -> CreationResultV1:
        state = SwarmSnapshot.model_validate(snapshot)
        required_bindings = (
            ("candidate_manifest", "source_archive", "codex_build_receipt")
            if isinstance(job, CreationJobV2)
            else ("candidate_manifest", "source_archive")
        )
        if (
            state.package_manifest_ref is None
            or state.skill_usage_receipt_ref is None
            or set(state.artifact_bindings) != set(required_bindings)
        ):
            return CreationResultV1(
                creation_job_id=job.creation_job_id,
                correlation_id=job.correlation_id,
                subject_version=job.subject_version,
                attempt=job.attempt,
                status="blocked",
                failure=CreationFailure(
                    code="validation_failed",
                    summary="creation package evidence incomplete",
                ),
            )
        return CreationResultV1(
            creation_job_id=job.creation_job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=job.attempt,
            status="succeeded",
            package_manifest_ref=state.package_manifest_ref,
            artifact_refs=tuple(
                state.artifact_bindings[name] for name in required_bindings
            ),
            skill_usage_receipt_ref=state.skill_usage_receipt_ref,
        )
