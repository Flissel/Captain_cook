"""Fail-closed isolated evaluation for a sealed generated agent candidate."""

from __future__ import annotations

import compileall
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import NAMESPACE_URL, uuid5
import zipfile

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.contracts import (
    AgentFactoryJob,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.evidence_store import FactoryEvidenceStore
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.state_machine import FactoryActionKind
from agenten.agent_runtime.contracts import ArtifactRef


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FactoryCandidateArtifact(_FrozenModel):
    """One content-addressed file that must be present in the source archive."""

    reference: ArtifactRef
    relative_path: str = Field(min_length=1)

    @field_validator("relative_path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or "." in path.parts:
            raise ValueError("candidate artifact path must be a safe relative path")
        return path.as_posix()


class FactoryCandidateManifest(_FrozenModel):
    """The only executable input accepted by the factory evaluator."""

    schema_name: Literal["captain.factory-candidate.v1"] = "captain.factory-candidate.v1"
    candidate_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    source_archive_ref: ArtifactRef
    team_manifest: FactoryCandidateArtifact
    workflow_artifacts: tuple[FactoryCandidateArtifact, ...] = Field(min_length=1)
    tool_schema_artifacts: tuple[FactoryCandidateArtifact, ...] = Field(min_length=2)
    n8n_tools: tuple[TypedN8nTool, ...] = Field(min_length=1)
    build_command: tuple[str, ...] = Field(min_length=1)
    real_case_command: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(ge=1, le=300)

    @field_validator("build_command", "real_case_command")
    @classmethod
    def require_safe_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value[0] != "python":
            raise ValueError("candidate commands must use the isolated python executable")
        if any(not part or "\x00" in part for part in value):
            raise ValueError("candidate command parts must be non-empty and NUL-free")
        return value

    @model_validator(mode="after")
    def require_sealed_tool_schemas(self) -> "FactoryCandidateManifest":
        references = {item.reference.uri for item in self.tool_schema_artifacts}
        expected = {
            reference
            for tool in self.n8n_tools
            for reference in (tool.input_schema_ref, tool.output_schema_ref)
        }
        if references != expected:
            raise ValueError("each typed n8n input/output schema must be sealed in the candidate archive")
        if len(references) != len(self.tool_schema_artifacts):
            raise ValueError("candidate tool schema artifact references must be unique")
        return self


class FactoryEvaluationCheck(_FrozenModel):
    name: str
    status: Literal["passed", "failed", "infrastructure_failed"]
    detail: str


class FactoryCandidateEvaluationResult(_FrozenModel):
    status: Literal["succeeded", "failed", "infrastructure_failed"]
    trace_id: str
    assertion_ids: tuple[str, ...] = ()
    tool_names: tuple[str, ...]
    workspace_was_temporary: Literal[True] = True
    checks: tuple[FactoryEvaluationCheck, ...]


class FactoryCandidateEvaluator:
    """Evaluate only a digest-verified archive in a newly created temp directory."""

    def evaluate(
        self,
        *,
        job: AgentFactoryJob,
        candidate: FactoryCandidateManifest,
        source_archive: Path,
    ) -> FactoryCandidateEvaluationResult:
        trace_id = str(job.correlation_id)
        tool_names = tuple(tool.name for tool in candidate.n8n_tools)
        checks: list[FactoryEvaluationCheck] = []
        try:
            self._verify_source_archive(candidate.source_archive_ref, source_archive)
            checks.append(FactoryEvaluationCheck(name="source_archive", status="passed", detail="sha256 verified"))
            with TemporaryDirectory(prefix="captain-factory-evaluation-") as temporary:
                workspace = Path(temporary) / "candidate"
                self._extract_archive(source_archive, workspace)
                self._verify_artifact(candidate.team_manifest, workspace, "team_manifest")
                checks.append(FactoryEvaluationCheck(name="team_manifest", status="passed", detail="sha256 verified"))
                for index, workflow in enumerate(candidate.workflow_artifacts, start=1):
                    self._verify_artifact(workflow, workspace, f"workflow_{index}")
                    self._require_json(workspace / workflow.relative_path)
                    checks.append(
                        FactoryEvaluationCheck(name=f"workflow_{index}", status="passed", detail="sha256 and JSON verified")
                    )
                for index, schema in enumerate(candidate.tool_schema_artifacts, start=1):
                    self._verify_artifact(schema, workspace, f"tool_schema_{index}")
                    self._require_json(workspace / schema.relative_path)
                    checks.append(
                        FactoryEvaluationCheck(name=f"tool_schema_{index}", status="passed", detail="sha256 and JSON verified")
                    )
                if not compileall.compile_dir(str(workspace), quiet=1):
                    return self._failed(trace_id, tool_names, checks, "static_compile", "Python compilation failed")
                checks.append(FactoryEvaluationCheck(name="static_compile", status="passed", detail="compileall succeeded"))
                build = self._run(candidate.build_command, workspace, trace_id, candidate.timeout_seconds)
                if build.returncode != 0:
                    return self._failed(trace_id, tool_names, checks, "build", self._command_failure(build))
                checks.append(FactoryEvaluationCheck(name="build", status="passed", detail="command exited 0"))
                real_case = self._run(candidate.real_case_command, workspace, trace_id, candidate.timeout_seconds)
                if real_case.returncode != 0:
                    return self._failed(trace_id, tool_names, checks, "real_case", self._command_failure(real_case))
                assertion_ids = self._read_real_case_output(real_case.stdout, trace_id, job)
                checks.append(FactoryEvaluationCheck(name="real_case", status="passed", detail="trace and assertions verified"))
                return FactoryCandidateEvaluationResult(
                    status="succeeded",
                    trace_id=trace_id,
                    assertion_ids=assertion_ids,
                    tool_names=tool_names,
                    checks=tuple(checks),
                )
        except (FileNotFoundError, OSError, zipfile.BadZipFile) as exc:
            checks.append(FactoryEvaluationCheck(name="infrastructure", status="infrastructure_failed", detail=str(exc)))
            return FactoryCandidateEvaluationResult(
                status="infrastructure_failed",
                trace_id=trace_id,
                tool_names=tool_names,
                checks=tuple(checks),
            )
        except ValueError as exc:
            checks.append(FactoryEvaluationCheck(name="validation", status="failed", detail=str(exc)))
            return FactoryCandidateEvaluationResult(
                status="failed",
                trace_id=trace_id,
                tool_names=tool_names,
                checks=tuple(checks),
            )

    @staticmethod
    def _verify_source_archive(reference: ArtifactRef, source_archive: Path) -> None:
        if reference.media_type != "application/zip":
            raise ValueError("candidate source must be an application/zip artifact")
        content = source_archive.read_bytes()
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ValueError("candidate source archive digest does not match its artifact reference")

    @staticmethod
    def _extract_archive(source_archive: Path, workspace: Path) -> None:
        with zipfile.ZipFile(source_archive) as archive:
            for entry in archive.infolist():
                name = PurePosixPath(entry.filename)
                mode = entry.external_attr >> 16
                if name.is_absolute() or ".." in name.parts or stat.S_ISLNK(mode):
                    raise ValueError("candidate archive contains an unsafe path")
            archive.extractall(workspace)

    @staticmethod
    def _verify_artifact(artifact: FactoryCandidateArtifact, workspace: Path, name: str) -> None:
        path = workspace / artifact.relative_path
        if not path.is_file():
            raise ValueError(f"{name} is missing from the candidate archive")
        if hashlib.sha256(path.read_bytes()).hexdigest() != artifact.reference.sha256:
            raise ValueError(f"{name} digest does not match its artifact reference")

    @staticmethod
    def _require_json(path: Path) -> None:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("candidate workflow artifact is not valid JSON") from exc

    @staticmethod
    def _run(command: tuple[str, ...], workspace: Path, trace_id: str, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        resolved = (sys.executable, *command[1:])
        try:
            return subprocess.run(
                resolved,
                cwd=workspace,
                env=_isolated_environment(trace_id),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"candidate command timed out after {timeout_seconds} seconds") from exc

    @staticmethod
    def _read_real_case_output(stdout: str, trace_id: str, job: AgentFactoryJob) -> tuple[str, ...]:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("real-case command must emit exactly one JSON object") from exc
        if not isinstance(payload, dict) or payload.get("trace_id") != trace_id:
            raise ValueError("real-case result does not carry the Captain trace ID")
        assertions = payload.get("assertion_ids")
        if not isinstance(assertions, list) or any(not isinstance(item, str) or not item for item in assertions):
            raise ValueError("real-case result must contain non-empty assertion_ids")
        if len(assertions) != len(set(assertions)):
            raise ValueError("real-case result assertion_ids must be unique")
        if set(assertions) != set(job.acceptance_assertion_ids):
            raise ValueError("real-case result does not prove exactly the Captain acceptance assertions")
        return tuple(job.acceptance_assertion_ids)

    @staticmethod
    def _command_failure(result: subprocess.CompletedProcess[str]) -> str:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        return f"candidate command failed: {detail[:300]}"

    @staticmethod
    def _failed(
        trace_id: str,
        tool_names: tuple[str, ...],
        checks: list[FactoryEvaluationCheck],
        name: str,
        detail: str,
    ) -> FactoryCandidateEvaluationResult:
        checks.append(FactoryEvaluationCheck(name=name, status="failed", detail=detail))
        return FactoryCandidateEvaluationResult(
            status="failed",
            trace_id=trace_id,
            tool_names=tool_names,
            checks=tuple(checks),
        )


def _isolated_environment(trace_id: str) -> dict[str, str]:
    """Do not inherit provider, database, n8n, or user secrets into generated code."""

    allowed = ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATHEXT")
    environment = {name: value for name in allowed if (value := os.environ.get(name)) is not None}
    environment["CAPTAIN_TRACE_ID"] = trace_id
    environment["CAPTAIN_FACTORY_EVALUATION"] = "1"
    environment["PYTHONUTF8"] = "1"
    return environment


class ResolvedFactoryCandidate(_FrozenModel):
    """Local execution material for one sealed candidate, never ledger data."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    candidate: FactoryCandidateManifest
    source_archive: Path


class FactoryCandidateProvider:
    """Explicit candidate lookup; production wiring may resolve it from Minibook artifacts."""

    def candidate_for(self, job: AgentFactoryJob) -> ResolvedFactoryCandidate:
        raise NotImplementedError


class StaticFactoryCandidateProvider(FactoryCandidateProvider):
    """Small deterministic provider for the local CLI and integration tests."""

    def __init__(self, candidates: dict[object, ResolvedFactoryCandidate]) -> None:
        self._candidates = dict(candidates)

    def candidate_for(self, job: AgentFactoryJob) -> ResolvedFactoryCandidate:
        try:
            return self._candidates[job.job_id]
        except KeyError as exc:
            raise FileNotFoundError("no sealed candidate is registered for the factory job") from exc


class CandidateEvaluationFactory:
    """Emit leased Hermes lifecycle blocks from independently persisted evaluation evidence."""

    def __init__(
        self,
        *,
        provider: FactoryCandidateProvider,
        evidence_store: FactoryEvidenceStore,
        evaluator: FactoryCandidateEvaluator | None = None,
    ) -> None:
        self._provider = provider
        self._evidence_store = evidence_store
        self._evaluator = evaluator or FactoryCandidateEvaluator()

    async def dispatch(self, request: FactoryDispatch) -> FactoryEvidenceBlock:
        phase, role = _validation_phase(request.action.kind)
        if request.role is not role or request.lease is None or request.lease.role is not role:
            raise FactoryDispatchError("candidate validation requires the matching active factory lease")
        try:
            resolved = self._provider.candidate_for(request.job)
            result = self._evaluator.evaluate(
                job=request.job,
                candidate=resolved.candidate,
                source_archive=resolved.source_archive,
            )
        except (FileNotFoundError, OSError) as exc:
            result = FactoryCandidateEvaluationResult(
                status="infrastructure_failed",
                trace_id=str(request.job.correlation_id),
                tool_names=(),
                checks=(FactoryEvaluationCheck(name="candidate_lookup", status="infrastructure_failed", detail=str(exc)),),
            )
        evidence = await self._evidence_store.persist(
            request.job,
            result.model_dump_json(exclude_none=True).encode("utf-8"),
        )
        block_phase = _result_phase(phase, result.status)
        assertions = result.assertion_ids if result.status == "succeeded" and phase is not FactoryPhase.BUILD_PASSED else ()
        event_id = uuid5(
            NAMESPACE_URL,
            f"factory-evaluation|{request.job.job_id}|{request.action.attempt}|{block_phase.value}|{evidence.sha256}",
        )
        return FactoryEvidenceBlock(
            schema_name="captain.agent-factory-block.v1",
            event_id=event_id,
            job_id=request.job.job_id,
            correlation_id=request.job.correlation_id,
            causation_id=request.job.event_id,
            occurred_at=request.lease.issued_at,
            producer="hermes",
            subject_version=request.job.subject_version,
            attempt=request.action.attempt,
            phase=block_phase,
            role=role,
            status=FactoryBlockStatus(result.status),
            artifact_refs=(
                resolved.candidate.source_archive_ref,
                resolved.candidate.team_manifest.reference,
                *(item.reference for item in resolved.candidate.workflow_artifacts),
                *(item.reference for item in resolved.candidate.tool_schema_artifacts),
            ),
            evidence_refs=(evidence,),
            assertion_ids=assertions,
            lease_id=request.lease.lease_id,
        )


def _validation_phase(action: FactoryActionKind) -> tuple[FactoryPhase, FactoryRole]:
    phases = {
        FactoryActionKind.DISPATCH_BUILD_VALIDATOR: (FactoryPhase.BUILD_PASSED, FactoryRole.TOOL_INTEGRATOR),
        FactoryActionKind.DISPATCH_REAL_CASE_TESTER: (FactoryPhase.REAL_CASE_EVIDENCE, FactoryRole.REAL_CASE_TESTER),
        FactoryActionKind.DISPATCH_QUALITY_WARDEN: (FactoryPhase.QUALITY_REVIEWED, FactoryRole.QUALITY_WARDEN),
    }
    try:
        return phases[action]
    except KeyError as exc:
        raise FactoryDispatchError("action is not a candidate validation action") from exc


def _result_phase(phase: FactoryPhase, status: str) -> FactoryPhase:
    if phase is FactoryPhase.BUILD_PASSED and status != "succeeded":
        return FactoryPhase.BUILD_FAILED
    return phase
