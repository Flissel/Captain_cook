"""Named, resumable step adapter around the legacy SwarmPipeline."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from enum import Enum
from typing import Any, Callable, Protocol
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from .contracts import (
    ArtifactRef,
    CreationCompletionEvidenceV1,
    CreationFailure,
    CreationJobV1,
    CreationPreparationEvidenceV1,
    CreationResultV1,
    FactoryEvidenceBlockV1,
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
    creation_job_id: UUID
    completed_steps: tuple[str, ...] = ()
    output_digests: dict[str, str] = Field(default_factory=dict)
    artifact_bindings: dict[str, ArtifactRef] = Field(default_factory=dict)
    decisions: dict[str, str] = Field(default_factory=dict)
    external_receipt_ids: dict[str, str] = Field(default_factory=dict)
    package_manifest_ref: ArtifactRef | None = None
    skill_usage_receipt_ref: ArtifactRef | None = None
    pipeline_state_ref: ArtifactRef | None = None
    tool_gaps: tuple[ToolGapMarkerV1, ...] = ()
    evidence_step_refs: dict[str, ArtifactRef] = Field(default_factory=dict)
    evidence_step_times: dict[str, datetime] = Field(default_factory=dict)


class ContentAddressedCreationArtifacts:
    """Small immutable artifact store owned by the opt-in creation runtime."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._content_root = self.root / "content" / "sha256"
        self._content_root.mkdir(parents=True, exist_ok=True)

    def put(
        self,
        content: bytes,
        media_type: str,
        *,
        namespace: str = "minibook-creation",
    ) -> ArtifactRef:
        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes")
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", namespace) is None:
            raise ValueError("artifact namespace is invalid")
        digest = hashlib.sha256(content).hexdigest()
        path = self._content_root / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != content:
                raise ValueError("artifact digest collision")
        else:
            try:
                with path.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                if path.read_bytes() != content:
                    raise ValueError("immutable artifact content changed")
        return ArtifactRef(
            uri=f"artifact://capability-factory/{namespace}/{digest}",
            sha256=digest,
            media_type=media_type,
        )

    def read(self, reference: ArtifactRef) -> bytes:
        if (
            not reference.uri.startswith("artifact://capability-factory/")
            or reference.uri.rsplit("/", 1)[-1] != reference.sha256
        ):
            raise ValueError("artifact reference is outside the capability store")
        try:
            content = (
                self._content_root / reference.sha256[:2] / reference.sha256
            ).read_bytes()
        except OSError as exc:
            raise ValueError("artifact content is unavailable") from exc
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
        return self.artifacts.put(
            encoded, "application/json", namespace="creation-pipeline-state"
        )

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

    @staticmethod
    def _artifact_kind(path: str) -> str | None:
        if path == "team-manifest.json":
            return "team_manifest"
        if path == "RUNBOOK.md":
            return "runbook"
        if path.startswith("autogen/") and path.endswith(".py"):
            return "autogen_source"
        if path.startswith("skills/"):
            return "skill"
        if path.startswith("tests/test_") and path.endswith(".py"):
            return "test"
        if path.startswith("evidence/"):
            return "evidence"
        if path.startswith("n8n/") and path.endswith(".json"):
            return "n8n_workflow"
        if path.startswith("adapters/") and path.endswith(".py"):
            return "local_adapter"
        return None

    @staticmethod
    def _media_type(path: str) -> str:
        suffix = Path(path).suffix.lower()
        return {
            ".json": "application/json",
            ".md": "text/markdown",
            ".py": "text/x-python",
            ".yaml": "application/yaml",
            ".yml": "application/yaml",
        }.get(suffix, "application/octet-stream")

    def _required_gap(self, gap_id: str, detail: str) -> dict[str, Any]:
        gap = {
            "schema": "TODO_TOOL.v1",
            "gap_id": gap_id,
            "severity": "required",
            "status": "unresolved",
            "required_output": detail,
        }
        gap_ref = self.artifacts.put(
            json.dumps(gap, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            "application/json",
            namespace="creation-tool-gap",
        )
        marker = ToolGapMarkerV1(
            gap_id=gap_id,
            severity="required",
            evidence_ref=gap_ref,
            status="unresolved",
        )
        return {"tool_gaps": (marker,)}

    @staticmethod
    def _team_manifest(content: bytes) -> dict[str, Any]:
        try:
            manifest = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("team manifest is not valid JSON") from exc
        expected = {
            "schema",
            "capability_id",
            "capability_version",
            "autogen_modules",
            "test_paths",
        }
        if not isinstance(manifest, dict) or set(manifest) != expected:
            raise ValueError("team manifest fields are incomplete")
        if manifest["schema"] != "autogen-team.v1":
            raise ValueError("team manifest schema is unsupported")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", manifest["capability_id"] or "") is None:
            raise ValueError("team manifest capability identity is invalid")
        if not isinstance(manifest["capability_version"], int) or isinstance(
            manifest["capability_version"], bool
        ) or manifest["capability_version"] < 1:
            raise ValueError("team manifest capability version is invalid")
        for field in ("autogen_modules", "test_paths"):
            values = manifest[field]
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(item, str) or not item for item in values)
                or len(values) != len(set(values))
            ):
                raise ValueError(f"team manifest {field} is invalid")
        return manifest

    def _tool_gaps(
        self, job: CreationJobV1, content: bytes
    ) -> tuple[list[dict[str, Any]], tuple[ToolGapMarkerV1, ...]]:
        try:
            envelope = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("tool gap declaration is not valid JSON") from exc
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"schema", "tool_gaps"}
            or envelope["schema"] != "minibook.creation-tool-gaps.v1"
            or not isinstance(envelope["tool_gaps"], list)
        ):
            raise ValueError("tool gap declaration is invalid")
        rich: list[dict[str, Any]] = []
        creation: list[ToolGapMarkerV1] = []
        required_fields = {
            "schema",
            "gap_id",
            "severity",
            "input_contract_ref",
            "output_contract_ref",
            "least_privilege_capability",
            "implementation_options",
            "acceptance_assertion_ids",
            "evidence_ref",
            "status",
        }
        for item in envelope["tool_gaps"]:
            if not isinstance(item, dict) or set(item) != required_fields:
                raise ValueError("tool gap fields are incomplete")
            if item["schema"] != "TODO_TOOL.v1":
                raise ValueError("tool gap schema is unsupported")
            if item["severity"] not in {"required", "optional"} or item["status"] not in {
                "unresolved",
                "resolved",
            }:
                raise ValueError("tool gap status is invalid")
            assertions = item["acceptance_assertion_ids"]
            if (
                not isinstance(assertions, list)
                or not assertions
                or not set(assertions).issubset(set(job.public_assertion_ids))
            ):
                raise ValueError("tool gap assertions exceed released assertions")
            references = {
                field: ArtifactRef.model_validate(item[field])
                for field in (
                    "input_contract_ref",
                    "output_contract_ref",
                    "evidence_ref",
                )
            }
            for reference in references.values():
                self.artifacts.read(reference)
            normalized = dict(item)
            normalized.update(
                {field: reference.model_dump(mode="json") for field, reference in references.items()}
            )
            rich.append(normalized)
            creation.append(
                ToolGapMarkerV1(
                    gap_id=str(item["gap_id"]),
                    severity=item["severity"],
                    evidence_ref=references["evidence_ref"],
                    status=item["status"],
                )
            )
        return rich, tuple(creation)

    def _capture_export(
        self, job: CreationJobV1, pipeline: Any
    ) -> dict[str, Any]:
        package_gap = getattr(pipeline, "package_contract_gap", None)
        if isinstance(package_gap, dict):
            gap_id = package_gap.get("gap_id")
            required_outputs = package_gap.get("required_outputs")
            if (
                isinstance(gap_id, str)
                and gap_id
                and isinstance(required_outputs, (list, tuple))
                and required_outputs
                and all(isinstance(item, str) and item for item in required_outputs)
            ):
                return self._required_gap(
                    gap_id,
                    "; ".join(required_outputs),
                )
        export = getattr(pipeline, "export_result", None)
        if not isinstance(export, dict) or export.get("status") != "SUCCESS":
            return self._required_gap(
                "legacy-swarm-package-c-export",
                "successful legacy export with an observed output path",
            )
        export_path = Path(str(export.get("path", ""))).resolve()
        if not export_path.is_dir():
            raise ValueError("legacy pipeline export path is unavailable")
        all_files = self._files(export_path)
        files = [
            (name, content, self._artifact_kind(name))
            for name, content in all_files
            if self._artifact_kind(name) is not None
        ]
        by_path = {name: content for name, content, _kind in files}
        required = {
            "team-manifest.json",
            "RUNBOOK.md",
            "evidence/tool-gaps.json",
            "evidence/hermes-skill-usage-receipt.json",
        }
        missing = sorted(required - set(by_path))
        if "evidence/hermes-skill-usage-receipt.json" in missing:
            return self._required_gap(
                "hermes-skill-usage-receipt",
                "evidence/hermes-skill-usage-receipt.json",
            )
        if missing:
            return self._required_gap(
                "forge-capability-candidate-contract",
                "missing package paths: " + ", ".join(missing),
            )
        if not any(name.startswith("autogen/") for name in by_path) or not any(
            name.startswith("skills/") for name in by_path
        ) or not any(name.startswith("tests/") for name in by_path):
            return self._required_gap(
                "forge-capability-candidate-contract",
                "autogen, skills, and tests package roots",
            )
        try:
            team_manifest = self._team_manifest(by_path["team-manifest.json"])
            autogen_paths = {name for name, _content, kind in files if kind == "autogen_source"}
            test_paths = {name for name, _content, kind in files if kind == "test"}
            if set(team_manifest["autogen_modules"]) != autogen_paths:
                raise ValueError("team manifest modules do not match export bytes")
            if set(team_manifest["test_paths"]) != test_paths:
                raise ValueError("team manifest tests do not match export bytes")
            rich_gaps, creation_gaps = self._tool_gaps(
                job, by_path["evidence/tool-gaps.json"]
            )
            receipt_bytes = by_path["evidence/hermes-skill-usage-receipt.json"]
            self._validate_receipt(job, receipt_bytes)
        except ValueError as exc:
            return self._required_gap(
                "forge-capability-candidate-contract", str(exc)
            )

        package_artifacts: list[dict[str, Any]] = []
        references: dict[str, ArtifactRef] = {}
        for name, content, kind in files:
            assert kind is not None
            reference = self.artifacts.put(
                content,
                self._media_type(name),
                namespace="candidate-file",
            )
            references[name] = reference
            package_artifacts.append(
                {
                    "path": name,
                    "kind": kind,
                    "reference": reference.model_dump(mode="json"),
                }
            )
        if len({item["reference"]["sha256"] for item in package_artifacts}) != len(
            package_artifacts
        ):
            return self._required_gap(
                "forge-capability-candidate-contract",
                "package files must have unique content digests",
            )
        package_buffer = io.BytesIO()
        with zipfile.ZipFile(
            package_buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for name, content, _kind in files:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
        package_ref = self.artifacts.put(
            package_buffer.getvalue(),
            "application/zip",
            namespace="candidate-source",
        )
        receipt_ref = self.artifacts.put(
            receipt_bytes,
            "application/json",
            namespace="skill-usage",
        )
        candidate = {
            "schema": "forge.capability-package-candidate.v1",
            "capability_id": team_manifest["capability_id"],
            "capability_version": team_manifest["capability_version"],
            "factory_job_id": str(job.factory_job_id),
            "creation_job_id": str(job.creation_job_id),
            "correlation_id": str(job.correlation_id),
            "subject_version": job.subject_version,
            "attempt": job.attempt,
            "source_ref": package_ref.model_dump(mode="json"),
            "team_manifest_ref": references["team-manifest.json"].model_dump(mode="json"),
            "artifacts": package_artifacts,
            "skill_usage_receipt_ref": receipt_ref.model_dump(mode="json"),
            "tool_gaps": rich_gaps,
            "runbook_ref": references["RUNBOOK.md"].model_dump(mode="json"),
        }
        candidate_bytes = json.dumps(
            candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        candidate_ref = self.artifacts.put(
            candidate_bytes,
            "application/json",
            namespace="candidate",
        )
        return {
            "package_manifest_ref": candidate_ref,
            "artifact_bindings": {"source_archive": package_ref},
            "skill_usage_receipt_ref": receipt_ref,
            "tool_gaps": creation_gaps,
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
        prior = SwarmSnapshot.model_validate(
            prior_snapshot or {"creation_job_id": job.creation_job_id}
        )
        if prior.creation_job_id != job.creation_job_id:
            raise ValueError("creation snapshot identity changed")
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
        evidence_refs = dict(prior.evidence_step_refs)
        evidence_times = dict(prior.evidence_step_times)
        if named_step in {SwarmStep.ARCHITECT, SwarmStep.TOOLFORGE}:
            reference = snapshot_updates.get("pipeline_state_ref")
            if reference is not None:
                evidence_refs[named_step.value] = ArtifactRef.model_validate(reference)
                evidence_times[named_step.value] = datetime.now(timezone.utc)
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
                "evidence_step_refs": evidence_refs,
                "evidence_step_times": evidence_times,
                **snapshot_updates,
            }
        )
        return StepOutcome(snapshot=snapshot.model_dump(mode="json"), effect_receipt=receipt)

    def preparation_evidence(
        self,
        job: CreationJobV1,
        snapshot: dict[str, Any],
    ) -> CreationPreparationEvidenceV1:
        state = SwarmSnapshot.model_validate(snapshot)
        architect_ref = state.evidence_step_refs.get(SwarmStep.ARCHITECT.value)
        tool_ref = state.evidence_step_refs.get(SwarmStep.TOOLFORGE.value)
        architect_at = state.evidence_step_times.get(SwarmStep.ARCHITECT.value)
        tool_at = state.evidence_step_times.get(SwarmStep.TOOLFORGE.value)
        if (
            architect_ref is None
            or tool_ref is None
            or architect_at is None
            or tool_at is None
            or architect_at >= tool_at
        ):
            raise ValueError(
                "creation preparation requires completed architect and toolforge evidence"
            )
        return CreationPreparationEvidenceV1(
            creation_job=job,
            blocks=(
                self._evidence_block(
                    job,
                    phase="blueprint_created",
                    role="agent_architect",
                    occurred_at=architect_at,
                    evidence_ref=architect_ref,
                ),
                self._evidence_block(
                    job,
                    phase="tool_candidate_tested",
                    role="tool_integrator",
                    occurred_at=tool_at,
                    evidence_ref=tool_ref,
                ),
            ),
        )

    def completion_evidence(
        self,
        job: CreationJobV1,
        result: CreationResultV1,
        snapshot: dict[str, Any],
    ) -> CreationCompletionEvidenceV1:
        state = SwarmSnapshot.model_validate(snapshot)
        tool_at = state.evidence_step_times.get(SwarmStep.TOOLFORGE.value)
        occurred_at = datetime.now(timezone.utc)
        if tool_at is None or occurred_at <= tool_at:
            raise ValueError("creation completion does not follow tool evidence")
        if result.package_manifest_ref is None or result.skill_usage_receipt_ref is None:
            raise ValueError("creation completion requires package and skill evidence")
        return CreationCompletionEvidenceV1(
            result=result,
            block=self._evidence_block(
                job,
                phase="agent_code_created",
                role="tool_integrator",
                occurred_at=occurred_at,
                evidence_ref=result.package_manifest_ref,
                additional_evidence_ref=result.skill_usage_receipt_ref,
            ),
        )

    @staticmethod
    def _evidence_block(
        job: CreationJobV1,
        *,
        phase: str,
        role: str,
        occurred_at: datetime,
        evidence_ref: ArtifactRef,
        additional_evidence_ref: ArtifactRef | None = None,
    ) -> FactoryEvidenceBlockV1:
        evidence_refs = (
            (evidence_ref,)
            if additional_evidence_ref is None
            else (evidence_ref, additional_evidence_ref)
        )
        return FactoryEvidenceBlockV1(
            event_id=uuid5(job.creation_job_id, f"hermes:{phase}"),
            job_id=job.factory_job_id,
            correlation_id=job.correlation_id,
            causation_id=job.causation_id,
            occurred_at=occurred_at,
            producer="hermes",
            subject_version=job.subject_version,
            attempt=job.attempt,
            phase=phase,
            role=role,
            status="succeeded",
            evidence_refs=evidence_refs,
            assertion_ids=(),
            lease_id=f"minibook-swarm-{job.creation_job_id}",
        )

    async def _dispatch(self, pipeline: Any, step: SwarmStep, job: CreationJobV1) -> Any:
        if step is SwarmStep.MANAGER:
            task = getattr(pipeline, "task_name", None)
            if not isinstance(task, str) or not task.strip():
                task = str(job.input_ref.uri)
            return await pipeline.step_swarm_manager(self._session, task)
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
        if state.package_manifest_ref is None and state.tool_gaps:
            failure_refs = tuple(gap.evidence_ref for gap in state.tool_gaps)
            return CreationResultV1(
                creation_job_id=job.creation_job_id,
                correlation_id=job.correlation_id,
                subject_version=job.subject_version,
                attempt=job.attempt,
                status="blocked",
                evidence_refs=failure_refs,
                tool_gaps=state.tool_gaps,
                failure=CreationFailure(
                    code="tool_unresolved",
                    summary="creation package contract is unresolved",
                    evidence_refs=failure_refs,
                ),
            )
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
        if state.skill_usage_receipt_ref is None or any(
            gap.severity == "required" and gap.status == "unresolved"
            for gap in state.tool_gaps
        ):
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
