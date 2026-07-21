"""Named, resumable step adapter around the legacy SwarmPipeline."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from enum import Enum
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .contracts import (
    ArtifactRef,
    CreationFailure,
    CreationJobV1,
    CreationResultV1,
    ToolGapMarkerV1,
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
    pipeline_state_ref: ArtifactRef | None = None
    tool_gaps: tuple[ToolGapMarkerV1, ...] = ()


class ContentAddressedCreationArtifacts:
    """Small immutable artifact store owned by the opt-in creation runtime."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes, media_type: str) -> ArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        path = self.root / digest
        if path.exists():
            if path.read_bytes() != content:
                raise ValueError("artifact digest collision")
        else:
            path.write_bytes(content)
        return ArtifactRef(
            uri=f"artifact://sha256/{digest}",
            sha256=digest,
            media_type=media_type,
        )

    def read(self, reference: ArtifactRef) -> bytes:
        content = (self.root / reference.sha256).read_bytes()
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ValueError("artifact content digest changed")
        return content


class Snapshotter(Protocol):
    def capture(
        self,
        job: CreationJobV1,
        step: SwarmStep,
        pipeline: Any,
        output: Any,
        prior: SwarmSnapshot,
    ) -> dict[str, Any]: ...


class ExportArtifactSnapshotter:
    """Checkpoint safe legacy state and attest exported bytes without inventing evidence."""

    _STATE_FIELDS = (
        "start_time",
        "completed_steps",
        "revision_count",
        "code_post_id",
        "generated_files",
        "yaml_files",
        "architect_output",
        "output_path",
        "mcp_catalog",
        "mcp_selection",
        "mcp_enabled",
        "mcp_server_tools",
        "mcp_tools_prompt",
        "build_dir",
        "build_result",
        "run_result",
        "output_eval",
        "export_result",
        "pre_todo_eval",
        "todo_implemented",
    )

    def __init__(self, artifacts: ContentAddressedCreationArtifacts) -> None:
        self.artifacts = artifacts

    @staticmethod
    def _safe_json(value: Any) -> Any:
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, set):
            return sorted(value)
        if isinstance(value, tuple):
            return [ExportArtifactSnapshotter._safe_json(item) for item in value]
        if isinstance(value, list):
            return [ExportArtifactSnapshotter._safe_json(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): ExportArtifactSnapshotter._safe_json(item)
                for key, item in value.items()
            }
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise ValueError(f"unsupported legacy pipeline state: {type(value).__name__}")

    def _pipeline_state(self, pipeline: Any) -> ArtifactRef:
        state = {
            name: self._safe_json(getattr(pipeline, name))
            for name in self._STATE_FIELDS
            if hasattr(pipeline, name)
        }
        encoded = json.dumps(
            state, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return self.artifacts.put(encoded, "application/json")

    @staticmethod
    def _files(root: Path) -> list[tuple[str, bytes]]:
        files: list[tuple[str, bytes]] = []
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if ".git" in relative.parts:
                continue
            if path.is_symlink():
                raise ValueError("export contains a symbolic link")
            if path.is_file():
                files.append((relative.as_posix(), path.read_bytes()))
        if not files:
            raise ValueError("export contains no files")
        return files

    def _capture_export(
        self, job: CreationJobV1, pipeline: Any
    ) -> dict[str, Any]:
        export = getattr(pipeline, "export_result", None)
        if not isinstance(export, dict) or export.get("status") != "SUCCESS":
            raise ValueError("legacy pipeline did not produce a successful export")
        export_path = Path(str(export.get("path", ""))).resolve()
        if not export_path.is_dir():
            raise ValueError("legacy pipeline export path is unavailable")
        files = self._files(export_path)
        manifest = {
            "schema": "minibook.creation-export-manifest.v1",
            "creation_job_id": str(job.creation_job_id),
            "files": [
                {
                    "path": name,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
                for name, content in files
            ],
        }
        manifest_bytes = json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        package_buffer = io.BytesIO()
        with zipfile.ZipFile(
            package_buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for name, content in files:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
            info = zipfile.ZipInfo(
                "creation-export-manifest.json", date_time=(1980, 1, 1, 0, 0, 0)
            )
            info.create_system = 3
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, manifest_bytes)
        package_ref = self.artifacts.put(package_buffer.getvalue(), "application/zip")
        manifest_ref = self.artifacts.put(manifest_bytes, "application/json")

        receipt_path = export_path / "evidence" / "hermes-skill-usage-receipt.json"
        if not receipt_path.is_file():
            gap = {
                "schema": "TODO_TOOL.v1",
                "gap_id": "hermes-skill-usage-receipt",
                "severity": "required",
                "status": "unresolved",
                "required_output": "evidence/hermes-skill-usage-receipt.json",
            }
            gap_ref = self.artifacts.put(
                json.dumps(gap, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                "application/json",
            )
            marker = ToolGapMarkerV1(
                gap_id="hermes-skill-usage-receipt",
                severity="required",
                evidence_ref=gap_ref,
                status="unresolved",
            )
            return {
                "package_manifest_ref": manifest_ref,
                "artifact_bindings": {"export_package": package_ref},
                "tool_gaps": (marker,),
            }

        receipt_bytes = receipt_path.read_bytes()
        self._validate_receipt(job, receipt_bytes)
        receipt_ref = self.artifacts.put(receipt_bytes, "application/json")
        return {
            "package_manifest_ref": manifest_ref,
            "artifact_bindings": {"export_package": package_ref},
            "skill_usage_receipt_ref": receipt_ref,
            "tool_gaps": (),
        }

    @staticmethod
    def _validate_receipt(job: CreationJobV1, content: bytes) -> None:
        try:
            receipt = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Hermes skill usage receipt is not valid JSON") from exc
        if not isinstance(receipt, dict):
            raise ValueError("Hermes skill usage receipt must be an object")
        required = {
            "schema": "hermes.skill-usage-receipt.v1",
            "producer": "hermes",
            "job_id": str(job.factory_job_id),
            "correlation_id": str(job.correlation_id),
            "used_skill_id": job.released_skill.skill_id,
            "used_skill_version": job.released_skill.version,
            "used_skill_sha256": job.released_skill.content_sha256,
            "outcome": "passed",
        }
        labels = {
            "job_id": "factory job",
            "correlation_id": "correlation",
            "used_skill_id": "released skill id",
            "used_skill_version": "released skill version",
            "used_skill_sha256": "released skill digest",
        }
        for field, expected in required.items():
            if receipt.get(field) != expected:
                raise ValueError(
                    f"Hermes skill usage receipt does not match {labels.get(field, field)}"
                )
        assertions = receipt.get("assertion_ids")
        if not isinstance(assertions, list) or not set(job.public_assertion_ids).issubset(
            set(assertions)
        ):
            raise ValueError("Hermes skill usage receipt does not cover public assertions")

    def capture(
        self,
        job: CreationJobV1,
        step: SwarmStep,
        pipeline: Any,
        output: Any,
        prior: SwarmSnapshot,
    ) -> dict[str, Any]:
        del output, prior
        updates: dict[str, Any] = {"pipeline_state_ref": self._pipeline_state(pipeline)}
        if step is SwarmStep.EXPORT:
            updates.update(self._capture_export(job, pipeline))
        return updates

    def restore(self, pipeline: Any, snapshot: SwarmSnapshot) -> Any:
        if snapshot.pipeline_state_ref is None:
            return pipeline
        state = json.loads(self.artifacts.read(snapshot.pipeline_state_ref))
        for name, value in state.items():
            if name in {"output_path", "build_dir"} and value is not None:
                value = Path(value)
            elif name == "completed_steps":
                value = set(value)
            setattr(pipeline, name, value)
        return pipeline


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
        snapshotter: Snapshotter | None = None,
    ) -> None:
        self._pipeline_factory = pipeline_factory
        self._session = session
        self._snapshotter = snapshotter

    async def run_step(
        self,
        job: CreationJobV1,
        step: str,
        prior_snapshot: dict[str, Any],
        effect_key: str,
        accepted_effect: dict[str, Any] | None,
    ) -> StepOutcome:
        named_step = SwarmStep(step)
        prior = SwarmSnapshot.model_validate(prior_snapshot)
        pipeline = self._pipeline_factory(prior)
        output: Any = accepted_effect
        snapshot_updates: dict[str, Any] = {}
        if accepted_effect is None:
            output = await self._dispatch(pipeline, named_step, job)
            if self._snapshotter is not None:
                snapshot_updates = self._snapshotter.capture(
                    job, named_step, pipeline, output, prior
                )
        else:
            candidate = accepted_effect.get("snapshot_updates", {})
            if isinstance(candidate, dict):
                snapshot_updates = candidate
        if snapshot_updates:
            candidate_state = SwarmSnapshot.model_validate(
                prior.model_dump(mode="json") | snapshot_updates
            ).model_dump(mode="json")
            snapshot_updates = {
                field: candidate_state[field] for field in snapshot_updates
            }
        digest = hashlib.sha256(
            json.dumps(output, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        completed = tuple(dict.fromkeys((*prior.completed_steps, named_step.value)))
        receipts = dict(prior.external_receipt_ids)
        receipt = None
        if named_step.value in self.effectful_steps:
            receipt = accepted_effect or {
                "receipt_id": effect_key,
                "output_sha256": digest,
                "snapshot_updates": snapshot_updates,
            }
            receipts[named_step.value] = str(receipt["receipt_id"])
        snapshot = SwarmSnapshot.model_validate(
            prior.model_dump(mode="json")
            | {
                "completed_steps": completed,
                "output_digests": prior.output_digests | {named_step.value: digest},
                "external_receipt_ids": receipts,
                **snapshot_updates,
            }
        )
        return StepOutcome(snapshot=snapshot.model_dump(mode="json"), effect_receipt=receipt)

    async def _dispatch(self, pipeline: Any, step: SwarmStep, job: CreationJobV1) -> Any:
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
            SwarmStep.EXPORT: "step_export",
        }
        method = getattr(pipeline, method_names[step])
        return await method(self._session)

    def assemble_result(
        self, job: CreationJobV1, snapshot: dict[str, Any]
    ) -> CreationResultV1:
        state = SwarmSnapshot.model_validate(snapshot)
        if state.package_manifest_ref is None:
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
        if state.skill_usage_receipt_ref is None:
            failure_refs = tuple(gap.evidence_ref for gap in state.tool_gaps)
            return CreationResultV1(
                creation_job_id=job.creation_job_id,
                correlation_id=job.correlation_id,
                subject_version=job.subject_version,
                attempt=job.attempt,
                status="blocked",
                package_manifest_ref=state.package_manifest_ref,
                artifact_refs=tuple(state.artifact_bindings.values()),
                evidence_refs=failure_refs,
                tool_gaps=state.tool_gaps,
                failure=CreationFailure(
                    code="tool_unresolved",
                    summary="Hermes skill usage evidence is unavailable",
                    evidence_refs=failure_refs,
                ),
            )
        return CreationResultV1(
            creation_job_id=job.creation_job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=job.attempt,
            status="succeeded",
            package_manifest_ref=state.package_manifest_ref,
            artifact_refs=tuple(state.artifact_bindings.values()),
            tool_gaps=state.tool_gaps,
            skill_usage_receipt_ref=state.skill_usage_receipt_ref,
        )
