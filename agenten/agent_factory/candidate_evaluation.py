"""Fail-closed isolated evaluation for a sealed generated agent candidate."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import signal
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5
import zipfile
from contextlib import contextmanager
from collections.abc import Iterator

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from agenten.agent_factory.contracts import (
    AgentFactoryJob,
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryJob,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.evidence_store import FactoryEvidenceStore
from agenten.agent_factory.forge_contracts import (
    CodexBuildReceiptV1,
    CreationPackageManifestV1,
    CreationPackageManifestV2,
    CreationJobV3,
    CreationResultV1,
    CreationResultV2,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
    ForgeBuildSkillUsageReceiptV1,
    PackageBuildEvidenceV1,
    PackageRealCaseEvidenceV1,
    codex_build_receipt_sha256,
    validate_build_effect_binding,
)
from agenten.agent_factory.leases import validate_factory_lease
from agenten.agent_factory.n8n_tools import OpaqueN8nToolReference, TypedN8nTool
from agenten.agent_factory.business_decision_tool import TOOL_NAME as BUSINESS_DECISION_TOOL
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.skill_evaluation import (
    HermesSkillEvaluationRequest,
    ReleasedHermesSkill,
)
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamExecutionEvidenceV1,
)
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


class FactoryAutoGenAgentV1(_FrozenModel):
    """One named specialist in the sealed AutoGen topology."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    tools: tuple[str, ...] = ()
    system_prompt_ref: ArtifactRef
    handoffs: tuple[str, ...] = ()

    @field_validator("tools", "handoffs")
    @classmethod
    def require_unique_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("agent tools and handoffs must be unique named entries")
        return value


class FactoryAutoGenTeamManifestV1(_FrozenModel):
    """Strict executable topology read from the candidate's sealed team manifest."""

    schema_name: Literal["autogen-team.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    conversation_pattern: Literal[
        "swarm",
        "selector_group_chat",
        "round_robin_group_chat",
        "single_agent",
    ]
    agents: tuple[FactoryAutoGenAgentV1, ...] = Field(min_length=1)
    memory_policy: str = Field(min_length=1, max_length=64)
    max_messages: int = Field(ge=1, le=100, strict=True)
    max_handoffs: int = Field(ge=0, le=50, strict=True)
    max_tool_calls: int = Field(ge=0, le=100, strict=True)
    termination_conditions: tuple[str, ...] = Field(min_length=1)
    entrypoint_command: tuple[str, ...] = Field(
        min_length=2,
        description=(
            "Legacy candidate metadata only; the host runner never executes this command."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def default_conversation_pattern(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "conversation_pattern" in value:
            return value
        agents = value.get("agents")
        has_handoffs = isinstance(agents, (list, tuple)) and any(
            isinstance(agent, dict) and bool(agent.get("handoffs")) for agent in agents
        )
        copied = dict(value)
        copied["conversation_pattern"] = (
            "swarm" if isinstance(agents, (list, tuple)) and len(agents) > 1 and has_handoffs else "single_agent"
        )
        return copied

    @field_validator("memory_policy")
    @classmethod
    def require_named_memory_policy(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("memory policy must be named")
        return normalized

    @field_validator("termination_conditions")
    @classmethod
    def require_unique_termination_conditions(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        allowed = {
            "task_completed",
            "max_messages",
            "max_handoffs",
            "max_tool_calls",
            "provider_cost_unresolved",
        }
        if (
            len(value) != len(set(value))
            or any(item not in allowed for item in value)
            or not set(value).intersection(
                {"task_completed", "max_messages", "max_handoffs", "max_tool_calls"}
            )
        ):
            raise ValueError("termination conditions must be unique named entries")
        return value

    @field_validator("entrypoint_command")
    @classmethod
    def require_isolated_entrypoint(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if value[0] != "python" or any(not item or "\x00" in item for item in value):
            raise ValueError("team entrypoint must use the isolated python executable")
        return value

    @model_validator(mode="after")
    def require_closed_topology(self, info: ValidationInfo) -> "FactoryAutoGenTeamManifestV1":
        names = tuple(agent.name for agent in self.agents)
        if len(names) != len(set(names)):
            raise ValueError("AutoGen agent names must be unique")
        known_agents = set(names)
        allowed_tools = set((info.context or {}).get("allowed_tools", ()))
        for agent in self.agents:
            unknown_handoffs = set(agent.handoffs) - known_agents
            if unknown_handoffs:
                raise ValueError(f"unknown handoff: {sorted(unknown_handoffs)[0]}")
            unknown_tools = set(agent.tools) - allowed_tools
            if unknown_tools:
                raise ValueError(f"unknown tool: {sorted(unknown_tools)[0]}")
        if self.conversation_pattern == "single_agent" and len(self.agents) != 1:
            raise ValueError("single_agent conversation requires exactly one agent")
        if (
            self.conversation_pattern != "swarm"
            and any(agent.handoffs for agent in self.agents)
        ):
            raise ValueError("handoff topology requires swarm conversation pattern")
        if any(agent.handoffs for agent in self.agents) and self.max_handoffs == 0:
            raise ValueError("handoff topology requires a positive max_handoffs ceiling")
        return self


class FactoryCandidateManifest(_FrozenModel):
    """The only executable input accepted by the factory evaluator."""

    schema_name: Literal["captain.factory-candidate.v1"] = Field(
        default="captain.factory-candidate.v1",
        validation_alias=AliasChoices("schema", "schema_name"),
        serialization_alias="schema",
    )
    candidate_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    source_archive_ref: ArtifactRef
    team_manifest: FactoryCandidateArtifact
    workflow_artifacts: tuple[FactoryCandidateArtifact, ...] = ()
    tool_schema_artifacts: tuple[FactoryCandidateArtifact, ...] = ()
    n8n_tools: tuple[TypedN8nTool, ...] = ()
    n8n_tool_references: tuple[OpaqueN8nToolReference, ...] = ()
    host_tools: tuple[str, ...] = ()
    build_command: tuple[str, ...] = Field(min_length=1)
    real_case_command: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(ge=1, le=300)

    @field_validator("host_tools")
    @classmethod
    def require_reserved_host_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or set(value) - {BUSINESS_DECISION_TOOL}:
            raise ValueError("candidate host tool is not Captain-reserved")
        return value

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
        if not self.n8n_tools:
            if (
                self.workflow_artifacts
                or self.tool_schema_artifacts
                or self.n8n_tool_references
            ):
                raise ValueError(
                    "candidate cannot contain n8n artifacts without n8n tools"
                )
            return self
        if not self.workflow_artifacts:
            raise ValueError("candidate with n8n tools requires at least one workflow")
        tool_names = tuple(tool.name for tool in self.n8n_tools)
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("candidate n8n tool names must be unique")
        references = {item.reference.uri for item in self.tool_schema_artifacts}
        expected_sequence = tuple(
            reference
            for tool in self.n8n_tools
            for reference in (tool.input_schema_ref, tool.output_schema_ref)
        )
        expected = set(expected_sequence)
        if len(expected) != len(expected_sequence):
            raise ValueError("candidate n8n tools require unique input/output schemas")
        if references != expected:
            raise ValueError("each typed n8n input/output schema must be sealed in the candidate archive")
        if len(references) != len(self.tool_schema_artifacts):
            raise ValueError("candidate tool schema artifact references must be unique")
        if self.n8n_tool_references:
            expected_references = tuple(
                tool.opaque_reference() for tool in self.n8n_tools
            )
            if self.n8n_tool_references != expected_references:
                raise ValueError(
                    "candidate n8n tool references must exactly match the typed tool schemas"
                )
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
    candidate_manifest: FactoryCandidateManifest | None = None
    team_execution_manifest: FactoryAutoGenTeamManifestV1 | None = None


class FactoryCandidateEvaluator:
    """Evaluate only a digest-verified archive in a newly created temp directory."""

    def validate(
        self,
        candidate: "ResolvedFactoryCandidate",
        max_seconds: float,
    ) -> FactoryCandidateEvaluationResult:
        """Preflight the sealed executable topology without running a paid case."""

        if max_seconds <= 0:
            raise ValueError("candidate evaluation requires positive remaining lease time")
        deadline = time.monotonic() + max_seconds
        manifest = candidate.candidate
        tool_names = (
            *(tool.name for tool in manifest.n8n_tools),
            *manifest.host_tools,
        )
        checks: list[FactoryEvaluationCheck] = []
        topology: FactoryAutoGenTeamManifestV1 | None = None
        try:
            self._verify_source_archive(manifest.source_archive_ref, candidate.source_archive)
            checks.append(
                FactoryEvaluationCheck(
                    name="source_archive", status="passed", detail="sha256 verified"
                )
            )
            with TemporaryDirectory(prefix="captain-factory-preflight-") as temporary:
                workspace = Path(temporary) / "candidate"
                self._extract_archive(candidate.source_archive, workspace)
                self._verify_artifact(manifest.team_manifest, workspace, "team_manifest")
                topology = self._read_team_execution_manifest(manifest, workspace)
                self._verify_system_prompts(topology, workspace)
                checks.append(
                    FactoryEvaluationCheck(
                        name="team_manifest",
                        status="passed",
                        detail="digest and AutoGen topology verified",
                    )
                )
                for index, workflow in enumerate(manifest.workflow_artifacts, start=1):
                    self._verify_artifact(workflow, workspace, f"workflow_{index}")
                    self._require_json(workspace / workflow.relative_path)
                for index, schema in enumerate(manifest.tool_schema_artifacts, start=1):
                    self._verify_artifact(schema, workspace, f"tool_schema_{index}")
                    self._require_json(workspace / schema.relative_path)
                static_compile = self._run(
                    ("python", "-m", "compileall", "-q", "."),
                    workspace,
                    manifest.candidate_id,
                    self._command_timeout(manifest.timeout_seconds, deadline),
                )
                if static_compile.returncode != 0:
                    raise ValueError(self._command_failure(static_compile))
                build = self._run(
                    manifest.build_command,
                    workspace,
                    manifest.candidate_id,
                    self._command_timeout(manifest.timeout_seconds, deadline),
                )
                if build.returncode != 0:
                    raise ValueError(self._command_failure(build))
                checks.append(
                    FactoryEvaluationCheck(
                        name="build", status="passed", detail="compile and build succeeded"
                    )
                )
            return self._preflight_result(
                status="succeeded",
                manifest=manifest,
                tool_names=tool_names,
                checks=checks,
                topology=topology,
            )
        except (FileNotFoundError, OSError, zipfile.BadZipFile) as exc:
            checks.append(
                FactoryEvaluationCheck(
                    name="infrastructure", status="infrastructure_failed", detail=str(exc)
                )
            )
            status: Literal["failed", "infrastructure_failed"] = "infrastructure_failed"
        except ValueError as exc:
            checks.append(
                FactoryEvaluationCheck(name="validation", status="failed", detail=str(exc))
            )
            status = "failed"
        return self._preflight_result(
            status=status,
            manifest=manifest,
            tool_names=tool_names,
            checks=checks,
            topology=topology,
        )

    @staticmethod
    def _preflight_result(
        *,
        status: Literal["succeeded", "failed", "infrastructure_failed"],
        manifest: FactoryCandidateManifest,
        tool_names: tuple[str, ...],
        checks: list[FactoryEvaluationCheck],
        topology: FactoryAutoGenTeamManifestV1 | None,
    ) -> FactoryCandidateEvaluationResult:
        """Preserve the sealed candidate tool authority during nested validation."""

        return FactoryCandidateEvaluationResult.model_validate(
            {
                "status": status,
                "trace_id": manifest.candidate_id,
                "tool_names": tool_names,
                "checks": tuple(checks),
                "candidate_manifest": manifest,
                "team_execution_manifest": topology,
            },
            context={"allowed_tools": set(tool_names)},
        )

    @contextmanager
    def verified_team_workspace(
        self,
        candidate: "ResolvedFactoryCandidate",
    ) -> Iterator[tuple[Path, FactoryAutoGenTeamManifestV1]]:
        """Yield a fresh digest-verified workspace; generated code stays out of Captain."""

        manifest = candidate.candidate
        self._verify_source_archive(manifest.source_archive_ref, candidate.source_archive)
        with TemporaryDirectory(prefix="captain-factory-team-") as temporary:
            workspace = Path(temporary) / "candidate"
            self._extract_archive(candidate.source_archive, workspace)
            self._verify_artifact(manifest.team_manifest, workspace, "team_manifest")
            topology = self._read_team_execution_manifest(manifest, workspace)
            self._verify_system_prompts(topology, workspace)
            for index, workflow in enumerate(manifest.workflow_artifacts, start=1):
                self._verify_artifact(workflow, workspace, f"workflow_{index}")
                self._require_json(workspace / workflow.relative_path)
            for index, schema in enumerate(manifest.tool_schema_artifacts, start=1):
                self._verify_artifact(schema, workspace, f"tool_schema_{index}")
                self._require_json(workspace / schema.relative_path)
            yield workspace, topology

    def evaluate(
        self,
        *,
        job: AgentFactoryJob,
        candidate: FactoryCandidateManifest,
        source_archive: Path,
    ) -> FactoryCandidateEvaluationResult:
        return self._evaluate(
            trace_id=str(job.correlation_id),
            acceptance_assertion_ids=job.acceptance_assertion_ids,
            candidate=candidate,
            source_archive=source_archive,
            max_seconds=None,
        )

    def evaluate_skill(
        self,
        *,
        request: HermesSkillEvaluationRequest,
        candidate: FactoryCandidateManifest,
        source_archive: Path,
        max_seconds: float | None = None,
    ) -> FactoryCandidateEvaluationResult:
        """Reuse the sealed evaluator without requiring a lifecycle job projection."""

        if candidate.source_archive_ref != request.candidate_source_ref:
            raise ValueError("candidate source does not match the skill evaluation request")
        return self._evaluate(
            trace_id=str(request.correlation_id),
            acceptance_assertion_ids=request.acceptance_assertion_ids,
            candidate=candidate,
            source_archive=source_archive,
            max_seconds=max_seconds,
        )

    def _evaluate(
        self,
        *,
        trace_id: str,
        acceptance_assertion_ids: tuple[str, ...],
        candidate: FactoryCandidateManifest,
        source_archive: Path,
        max_seconds: float | None,
    ) -> FactoryCandidateEvaluationResult:
        if max_seconds is not None and max_seconds <= 0:
            raise ValueError("candidate evaluation requires positive remaining lease time")
        deadline = None if max_seconds is None else time.monotonic() + max_seconds
        tool_names = (
            *(tool.name for tool in candidate.n8n_tools),
            *candidate.host_tools,
        )
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
                static_compile = self._run(
                    ("python", "-m", "compileall", "-q", "."),
                    workspace,
                    trace_id,
                    self._command_timeout(candidate.timeout_seconds, deadline),
                )
                if static_compile.returncode != 0:
                    return self._failed(
                        trace_id,
                        tool_names,
                        checks,
                        "static_compile",
                        self._command_failure(static_compile),
                        candidate,
                    )
                checks.append(FactoryEvaluationCheck(name="static_compile", status="passed", detail="compileall succeeded"))
                build = self._run(
                    candidate.build_command,
                    workspace,
                    trace_id,
                    self._command_timeout(candidate.timeout_seconds, deadline),
                )
                if build.returncode != 0:
                    return self._failed(
                        trace_id,
                        tool_names,
                        checks,
                        "build",
                        self._command_failure(build),
                        candidate,
                    )
                checks.append(FactoryEvaluationCheck(name="build", status="passed", detail="command exited 0"))
                real_case = self._run(
                    candidate.real_case_command,
                    workspace,
                    trace_id,
                    self._command_timeout(candidate.timeout_seconds, deadline),
                )
                if real_case.returncode != 0:
                    return self._failed(
                        trace_id,
                        tool_names,
                        checks,
                        "real_case",
                        self._command_failure(real_case),
                        candidate,
                    )
                try:
                    assertion_ids = self._read_real_case_output(
                        real_case.stdout,
                        trace_id,
                        acceptance_assertion_ids,
                    )
                    self._command_timeout(candidate.timeout_seconds, deadline)
                except ValueError as exc:
                    return self._failed(
                        trace_id,
                        tool_names,
                        checks,
                        "real_case",
                        str(exc),
                        candidate,
                    )
                checks.append(FactoryEvaluationCheck(name="real_case", status="passed", detail="trace and assertions verified"))
                return FactoryCandidateEvaluationResult(
                    status="succeeded",
                    trace_id=trace_id,
                    assertion_ids=assertion_ids,
                    tool_names=tool_names,
                    checks=tuple(checks),
                    candidate_manifest=candidate,
                )
        except (FileNotFoundError, OSError, zipfile.BadZipFile) as exc:
            checks.append(FactoryEvaluationCheck(name="infrastructure", status="infrastructure_failed", detail=str(exc)))
            return FactoryCandidateEvaluationResult(
                status="infrastructure_failed",
                trace_id=trace_id,
                tool_names=tool_names,
                checks=tuple(checks),
                candidate_manifest=candidate,
            )
        except ValueError as exc:
            checks.append(FactoryEvaluationCheck(name="validation", status="failed", detail=str(exc)))
            return FactoryCandidateEvaluationResult(
                status="failed",
                trace_id=trace_id,
                tool_names=tool_names,
                checks=tuple(checks),
                candidate_manifest=candidate,
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
    def _read_team_execution_manifest(
        candidate: FactoryCandidateManifest,
        workspace: Path,
    ) -> FactoryAutoGenTeamManifestV1:
        path = workspace / candidate.team_manifest.relative_path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("team manifest must be valid UTF-8 JSON") from exc
        return FactoryAutoGenTeamManifestV1.model_validate(
            payload,
            context={
                "allowed_tools": {
                    *(tool.name for tool in candidate.n8n_tools),
                    *candidate.host_tools,
                }
            },
        )

    @staticmethod
    def _verify_system_prompts(
        manifest: FactoryAutoGenTeamManifestV1,
        workspace: Path,
    ) -> None:
        content_by_digest = {
            hashlib.sha256(content).hexdigest(): content
            for path in workspace.rglob("*")
            if path.is_file()
            for content in (path.read_bytes(),)
        }
        if any(
            agent.system_prompt_ref.sha256 not in content_by_digest
            for agent in manifest.agents
        ):
            raise ValueError("system prompt ref is not sealed in the candidate archive")
        for agent in manifest.agents:
            try:
                prompt = content_by_digest[agent.system_prompt_ref.sha256].decode(
                    "utf-8"
                )
            except UnicodeDecodeError as exc:
                raise ValueError("system prompt must be valid UTF-8") from exc
            for tool_name in agent.tools:
                if tool_name == BUSINESS_DECISION_TOOL and tool_name not in prompt:
                    raise ValueError(
                        f"system prompt must explicitly name declared tool {tool_name}"
                    )
            for target in agent.handoffs:
                transfer_tool = f"transfer_to_{target}"
                if transfer_tool not in prompt:
                    raise ValueError(
                        f"handoff prompt must explicitly call {transfer_tool}"
                    )

    @staticmethod
    def _require_json(path: Path) -> None:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("candidate workflow artifact is not valid JSON") from exc

    @staticmethod
    def _run(command: tuple[str, ...], workspace: Path, trace_id: str, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        resolved = (sys.executable, *command[1:])
        process = subprocess.Popen(
            resolved,
            cwd=workspace,
            env=_isolated_environment(trace_id),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_sync_process_group_options(),
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            return subprocess.CompletedProcess(
                resolved,
                process.returncode,
                stdout,
                stderr,
            )
        except subprocess.TimeoutExpired as exc:
            _terminate_sync_process_tree(process, executable=sys.executable)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired as cleanup_exc:
                    raise ValueError(
                        "candidate process tree did not terminate within the cleanup window"
                    ) from cleanup_exc
            raise ValueError(f"candidate command timed out after {timeout_seconds} seconds") from exc

    @staticmethod
    def _read_real_case_output(
        stdout: str,
        trace_id: str,
        acceptance_assertion_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
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
        if set(assertions) != set(acceptance_assertion_ids):
            raise ValueError("real-case result does not prove exactly the Captain acceptance assertions")
        return tuple(acceptance_assertion_ids)

    @staticmethod
    def _command_failure(result: subprocess.CompletedProcess[str]) -> str:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        return f"candidate command failed: {detail[:300]}"

    @staticmethod
    def _command_timeout(configured_seconds: int, deadline: float | None) -> float:
        if deadline is None:
            return float(configured_seconds)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("candidate evaluation timed out within the active lease")
        return min(float(configured_seconds), remaining)

    @staticmethod
    def _failed(
        trace_id: str,
        tool_names: tuple[str, ...],
        checks: list[FactoryEvaluationCheck],
        name: str,
        detail: str,
        candidate: FactoryCandidateManifest,
    ) -> FactoryCandidateEvaluationResult:
        checks.append(FactoryEvaluationCheck(name=name, status="failed", detail=detail))
        return FactoryCandidateEvaluationResult(
            status="failed",
            trace_id=trace_id,
            tool_names=tool_names,
            checks=tuple(checks),
            candidate_manifest=candidate,
        )


def _isolated_environment(trace_id: str) -> dict[str, str]:
    """Do not inherit provider, database, n8n, or user secrets into generated code."""

    allowed = ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATHEXT")
    environment = {name: value for name in allowed if (value := os.environ.get(name)) is not None}
    environment["CAPTAIN_TRACE_ID"] = trace_id
    environment["CAPTAIN_FACTORY_EVALUATION"] = "1"
    environment["PYTHONUTF8"] = "1"
    return environment


def _sync_process_group_options() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate_sync_process_tree(
    process: subprocess.Popen[str],
    *,
    executable: str,
) -> None:
    """Terminate only the tree rooted at the verified process just spawned."""

    pid = process.pid
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError("candidate process identity is invalid")
    args = process.args
    actual_executable = args[0] if isinstance(args, (tuple, list)) and args else args
    if not isinstance(actual_executable, str) or (
        os.path.normcase(os.path.abspath(actual_executable))
        != os.path.normcase(os.path.abspath(executable))
    ):
        raise ValueError("candidate process identity does not match the evaluator executable")
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if completed.returncode not in {0, 128} and process.poll() is None:
            process.kill()
    else:
        group_found = True
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            group_found = False
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        finally:
            if group_found:
                try:
                    os.killpg(pid, 9)
                except ProcessLookupError:
                    pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                "candidate process tree did not terminate within the cleanup window"
            ) from exc


class ResolvedFactoryCandidate(_FrozenModel):
    """Local execution material for one sealed candidate, never ledger data."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    candidate: FactoryCandidateManifest
    source_archive: Path


class FactoryCandidateProvider:
    """Explicit candidate lookup; production wiring may resolve it from Minibook artifacts."""

    def accept_creation_result(
        self,
        job: FactoryJob,
        result: CreationResultV1 | CreationResultV2,
    ) -> ResolvedFactoryCandidate:
        raise NotImplementedError

    def candidate_for(self, job: FactoryJob) -> ResolvedFactoryCandidate:
        raise NotImplementedError


class StaticFactoryCandidateProvider(FactoryCandidateProvider):
    """Small deterministic provider for the local CLI and integration tests."""

    def __init__(self, candidates: dict[object, ResolvedFactoryCandidate]) -> None:
        self._candidates = dict(candidates)

    def accept_creation_result(
        self,
        job: FactoryJob,
        result: CreationResultV1,
    ) -> ResolvedFactoryCandidate:
        resolved = self.candidate_for(job)
        source_refs = tuple(
            ArtifactRef.model_validate(reference.model_dump(mode="json"))
            for reference in result.artifact_refs
        )
        if resolved.candidate.source_archive_ref not in source_refs:
            raise FactoryDispatchError(
                "static candidate source is not bound by CreationResultV1"
            )
        return resolved

    def candidate_for(self, job: FactoryJob) -> ResolvedFactoryCandidate:
        try:
            return self._candidates[job.job_id]
        except KeyError as exc:
            raise FileNotFoundError("no sealed candidate is registered for the factory job") from exc


class ForgeCandidateArtifactStore(Protocol):
    """Read verified immutable Forge bytes without granting write authority."""

    def read_bytes(self, reference: ArtifactRef) -> bytes: ...

    def local_path(self, reference: ArtifactRef) -> Path: ...


class CaptainCreationJobV3Source(Protocol):
    """Resolve the exact Captain-mapped V3 authority, never Minibook status."""

    def creation_job_for(self, job: FactoryJob, attempt: int) -> CreationJobV3: ...

    def record_accepted_result(self, result: CreationResultV2) -> CreationResultV2: ...

    def accepted_result_for(self, job: FactoryJob, attempt: int) -> CreationResultV2: ...

    def assignment_for(
        self, job: FactoryJob, attempt: int
    ) -> FactoryBuildAssignmentV2: ...

    def effect_for(self, job: FactoryJob, attempt: int) -> FactoryBuildEffectV2: ...


class FactoryBlockSource(Protocol):
    def blocks(self, job_id: object) -> tuple[FactoryEvidenceBlock, ...]: ...

    def released_for(
        self,
        job: FactoryJob,
        step: FactorySkillStep,
    ) -> ReleasedHermesSkill: ...


class GatewayForgeCandidateProvider(FactoryCandidateProvider):
    """Resolve candidates only from SHA-verified Forge bytes and Gateway blocks."""

    def __init__(
        self,
        *,
        repository: FactoryBlockSource,
        artifacts: ForgeCandidateArtifactStore,
        v3_jobs: CaptainCreationJobV3Source | None = None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._v3_jobs = v3_jobs

    @property
    def v3_readback_configured(self) -> bool:
        return self._v3_jobs is not None

    def accept_creation_result(
        self,
        job: FactoryJob,
        result: CreationResultV1 | CreationResultV2,
    ) -> ResolvedFactoryCandidate:
        if isinstance(result, CreationResultV2):
            return self._accept_v3(job, result)
        if isinstance(job, AgentFactoryJobV3) and self._v3_jobs is not None:
            raise FactoryDispatchError("Captain V3 job rejects V1/V2 creation evidence")
        if result.package_manifest_ref is None:
            raise FactoryDispatchError("Forge package manifest reference is missing")
        if result.skill_usage_receipt_ref is None:
            raise FactoryDispatchError("Forge skill usage receipt reference is missing")
        package_ref = _runtime_ref(result.package_manifest_ref)
        skill_usage_receipt_ref = _runtime_ref(result.skill_usage_receipt_ref)
        artifact_refs = tuple(_runtime_ref(reference) for reference in result.artifact_refs)
        resolved, package = self._resolve(
            job=job,
            attempt=result.attempt,
            package_ref=package_ref,
            artifact_refs=artifact_refs,
            skill_usage_receipt_ref=skill_usage_receipt_ref,
        )
        if (
            package.creation_job_id != result.creation_job_id
            or package.correlation_id != result.correlation_id
            or package.subject_version != result.subject_version
        ):
            raise FactoryDispatchError(
                "Forge package manifest does not match CreationResultV1"
            )
        return resolved

    def _accept_v3(
        self,
        job: FactoryJob,
        result: CreationResultV2,
    ) -> ResolvedFactoryCandidate:
        if not isinstance(job, AgentFactoryJobV3) or self._v3_jobs is None:
            raise FactoryDispatchError("Captain V3 creation authority is unavailable")
        try:
            authority = self._v3_jobs.creation_job_for(job, result.attempt)
            assignment = self._v3_jobs.assignment_for(job, result.attempt)
            effect = self._v3_jobs.effect_for(job, result.attempt)
        except (AttributeError, FileNotFoundError) as exc:
            raise FactoryDispatchError(
                "Captain V3 assignment or effect authority is unavailable"
            ) from exc
        if not isinstance(authority, CreationJobV3):
            raise FactoryDispatchError("Captain V3 creation authority is invalid")
        if (
            result.status != "succeeded"
            or result.factory_job_id != job.job_id
            or result.creation_job_id != authority.creation_job_id
            or result.correlation_id != job.correlation_id
            or result.subject_version != job.subject_version
            or result.attempt != authority.attempt
            or authority.factory_job_id != job.job_id
            or authority.correlation_id != job.correlation_id
            or authority.subject_version != job.subject_version
            or authority.compiled_spec_ref.sha256 != job.compiled_spec_ref.sha256
            or authority.public_assertion_ids != job.acceptance_assertion_ids
        ):
            raise FactoryDispatchError("Captain V3 result identity is stale or mixed")
        try:
            validate_build_effect_binding(assignment, effect)
        except ValueError as exc:
            raise FactoryDispatchError(
                "Captain V3 assignment or effect authority is invalid"
            ) from exc
        if (
            assignment.assignment_id != authority.command_id
            or assignment.creation_job_id != authority.creation_job_id
            or assignment.factory_job_id != authority.factory_job_id
            or assignment.correlation_id != authority.correlation_id
            or assignment.subject_version != authority.subject_version
            or assignment.attempt != authority.attempt
            or assignment.required_agent_ids != authority.required_agent_ids
            or assignment.ordered_handoffs != authority.ordered_handoffs
            or assignment.tool_requirements != authority.tool_requirements
            or assignment.workspace_policy_sha256 != authority.workspace_policy_ref.sha256
            or assignment.lease_ref != authority.lease_ref
            or assignment.deadline_at != authority.deadline_at
            or effect.hermes_usage_receipt_ref != result.hermes_usage_receipt_ref
            or effect.codex_build_receipt_ref != result.codex_build_receipt_ref
            or effect.build_evidence_refs != result.build_evidence_refs
            or effect.test_evidence_refs != result.real_case_evidence_refs
            or effect.tool_gaps != result.tool_gaps
        ):
            raise FactoryDispatchError(
                "Captain V3 assignment or effect authority differs from the result"
            )

        required_refs = (
            result.source_archive_ref,
            result.package_manifest_ref,
            result.team_manifest_ref,
            result.candidate_manifest_ref,
            result.hermes_usage_receipt_ref,
            result.codex_build_receipt_ref,
            *result.build_evidence_refs,
            *result.real_case_evidence_refs,
            result.cas_readback_receipt_ref,
        )
        if any(reference is None for reference in required_refs):
            raise FactoryDispatchError("Captain V3 result is missing independent readback inputs")
        typed_refs = tuple(_runtime_ref(reference) for reference in required_refs if reference is not None)
        contents = { _artifact_identity(ref): self._read_v3_exact(ref) for ref in typed_refs }
        source_ref = _runtime_ref(result.source_archive_ref)
        source_bytes = contents[_artifact_identity(source_ref)]
        try:
            source_path = self._artifacts.local_path(source_ref)
            if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_ref.sha256:
                raise ValueError
            with zipfile.ZipFile(source_path) as archive:
                infos = archive.infolist()
                if not infos or len({item.filename for item in infos}) != len(infos):
                    raise ValueError
                files = {
                    PurePosixPath(item.filename): archive.read(item)
                    for item in infos
                    if not item.is_dir()
                    and not PurePosixPath(item.filename).is_absolute()
                    and ".." not in PurePosixPath(item.filename).parts
                }
                if len(files) != len(infos):
                    raise ValueError
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise FactoryDispatchError("Captain V3 sealed ZIP is unavailable or invalid") from exc

        package_ref = _runtime_ref(result.package_manifest_ref)
        package_bytes = contents[_artifact_identity(package_ref)]
        try:
            package = json.loads(package_bytes)
            if (
                not isinstance(package, dict)
                or package.get("schema") != "minibook.team-package-manifest.v1"
                or package_bytes
                != json.dumps(
                    package,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
                or files.get(PurePosixPath("package-manifest.json")) != package_bytes
            ):
                raise ValueError
            entries = package["files"]
            if [entry.get("path") for entry in entries if isinstance(entry, dict)] != sorted(
                entry.get("path") for entry in entries if isinstance(entry, dict)
            ):
                raise ValueError
            children: dict[PurePosixPath, ArtifactRef] = {}
            for entry in entries:
                path = PurePosixPath(entry["path"])
                ref = ArtifactRef.model_validate(
                    {key: entry[key] for key in ("uri", "sha256", "media_type")}
                )
                child_content = self._read_v3_exact(ref)
                if path in children or files.get(path) != child_content:
                    raise ValueError
                children[path] = ref
                contents[_artifact_identity(ref)] = child_content
            if set(files) != {*children, PurePosixPath("package-manifest.json")}:
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FactoryDispatchError("Captain V3 package manifest or ZIP binding is invalid") from exc

        top_level = {
            PurePosixPath("team_manifest.json"): _runtime_ref(result.team_manifest_ref),
            PurePosixPath("factory-candidate.json"): _runtime_ref(result.candidate_manifest_ref),
            PurePosixPath("evidence/hermes-factory-skill-usage-receipt.json"): _runtime_ref(result.hermes_usage_receipt_ref),
            PurePosixPath("evidence/codex-build-receipt.json"): _runtime_ref(result.codex_build_receipt_ref),
        }
        if any(children.get(path) != reference for path, reference in top_level.items()):
            raise FactoryDispatchError("Captain V3 result refs differ from sealed package")
        packaged_build_refs = tuple(
            reference
            for path, reference in children.items()
            if str(path).startswith("evidence/build")
        )
        packaged_case_refs = tuple(
            reference
            for path, reference in children.items()
            if str(path).startswith("evidence/real-case")
        )
        if (
            packaged_build_refs
            != tuple(_runtime_ref(ref) for ref in result.build_evidence_refs)
            or packaged_case_refs
            != tuple(_runtime_ref(ref) for ref in result.real_case_evidence_refs)
        ):
            raise FactoryDispatchError(
                "Captain V3 result evidence refs differ from the canonical package"
            )

        try:
            team = json.loads(contents[_artifact_identity(top_level[PurePosixPath("team_manifest.json")])])
            agents = tuple(item["agent_id"] if isinstance(item, dict) else item for item in team["agents"])
            handoffs = tuple(
                (item["from_agent_id"], item["to_agent_id"])
                for item in team["handoffs"]
            )
            expected_handoffs = tuple(
                (item.from_agent_id, item.to_agent_id) for item in authority.ordered_handoffs
            )
            tools = {item["tool_id"]: item for item in team["tools"]}
            if agents != authority.required_agent_ids or handoffs != expected_handoffs:
                raise ValueError
            for requirement in authority.tool_requirements:
                if requirement.required and (
                    requirement.tool_id not in tools
                    or not isinstance(tools[requirement.tool_id].get("input_schema"), dict)
                    or not isinstance(tools[requirement.tool_id].get("output_schema"), dict)
                ):
                    raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FactoryDispatchError("Captain V3 team topology or required tools are invalid") from exc

        codex = self._validate_v3_receipts(
            job,
            authority,
            assignment,
            effect,
            result,
            contents,
            tuple(team["test_files"]),
        )
        candidate_source_ref = _runtime_ref(codex.source_archive_ref)
        candidate_source_bytes = self._read_v3_exact(candidate_source_ref)

        candidate_ref = _runtime_ref(result.candidate_manifest_ref)
        try:
            candidate_payload = json.loads(contents[_artifact_identity(candidate_ref)])
            if contents[_artifact_identity(candidate_ref)] != json.dumps(
                candidate_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8") + b"\n":
                raise ValueError
            expected_authority = {
                "compiled_spec_ref": authority.compiled_spec_ref.model_dump(mode="json"),
                "required_agent_ids": list(authority.required_agent_ids),
                "ordered_handoffs": [item.model_dump(mode="json") for item in authority.ordered_handoffs],
                "tool_requirements": [item.model_dump(mode="json") for item in authority.tool_requirements],
                "public_assertion_ids": list(authority.public_assertion_ids),
                "allowed_optional_tool_gaps": [item.tool_id for item in authority.tool_requirements if not item.required],
            }
            if any(candidate_payload.get(key) != value for key, value in expected_authority.items()):
                raise ValueError
            if candidate_payload.get("team_manifest_sha256") != top_level[PurePosixPath("team_manifest.json")].sha256:
                raise ValueError
            declared_artifacts = candidate_payload.get("artifacts")
            if not isinstance(declared_artifacts, list):
                raise ValueError
            actual_artifacts = {
                (item.get("path"), item.get("sha256"))
                for item in declared_artifacts
                if isinstance(item, dict) and set(item) == {"path", "sha256"}
            }
            expected_artifacts = {
                (str(path), reference.sha256)
                for path, reference in children.items()
                if path != PurePosixPath("factory-candidate.json")
            }
            if len(actual_artifacts) != len(declared_artifacts) or actual_artifacts != expected_artifacts:
                raise ValueError
            runtime_payload = dict(candidate_payload)
            for key in (
                "team_manifest_sha256",
                "compiled_spec_ref",
                "required_agent_ids",
                "ordered_handoffs",
                "tool_requirements",
                "public_assertion_ids",
                "allowed_optional_tool_gaps",
                "artifacts",
            ):
                runtime_payload.pop(key)
            if runtime_payload.get("source_archive_ref") != candidate_source_ref.model_dump(
                mode="json"
            ):
                raise ValueError
            candidate = FactoryCandidateManifest.model_validate(runtime_payload)
            declared = (
                candidate.team_manifest,
                *candidate.workflow_artifacts,
                *candidate.tool_schema_artifacts,
            )
            for artifact in declared:
                path = PurePosixPath(artifact.relative_path)
                if children.get(path) != artifact.reference:
                    raise ValueError
                if files.get(path) != contents.get(_artifact_identity(artifact.reference)):
                    raise ValueError
            source_files = self._safe_v3_zip_files(candidate_source_bytes)
            declared_source_paths = tuple(team["source_files"])
            declared_test_paths = tuple(team["test_files"])
            declared_paths = (*declared_source_paths, *declared_test_paths)
            if (
                any(not isinstance(path, str) for path in declared_paths)
                or len(declared_paths) != len(set(declared_paths))
                or set(source_files)
                != {PurePosixPath(path) for path in declared_paths}
                | {PurePosixPath("factory-candidate.json")}
            ):
                raise ValueError
            declared_digests = {
                item["path"]: item["sha256"] for item in declared_artifacts
            }
            for declared_path in declared_paths:
                path = PurePosixPath(declared_path)
                content = source_files[path]
                reference = children.get(path)
                if (
                    reference is None
                    or files.get(path) != content
                    or hashlib.sha256(content).hexdigest() != reference.sha256
                    or declared_digests.get(declared_path) != reference.sha256
                ):
                    raise ValueError
            preseal_payload = dict(runtime_payload)
            preseal_payload.pop("source_archive_ref")
            expected_preseal = json.dumps(
                preseal_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8") + b"\n"
            if source_files[PurePosixPath("factory-candidate.json")] != expected_preseal:
                raise ValueError
        except (OSError, TypeError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise FactoryDispatchError("Captain V3 candidate binding is invalid") from exc
        try:
            candidate_source_path = self._artifacts.local_path(candidate_source_ref)
            local_source_bytes = self._read_bound_v3_source_path(
                candidate_source_path,
                maximum_bytes=len(candidate_source_bytes),
            )
            if (
                local_source_bytes != candidate_source_bytes
                or hashlib.sha256(local_source_bytes).hexdigest()
                != candidate_source_ref.sha256
            ):
                raise ValueError
        except (OSError, ValueError) as exc:
            raise FactoryDispatchError(
                "Captain V3 candidate source archive binding is invalid"
            ) from exc

        receipt_ref = _runtime_ref(result.cas_readback_receipt_ref)
        try:
            receipt = json.loads(contents[_artifact_identity(receipt_ref)])
            if contents[_artifact_identity(receipt_ref)] != json.dumps(
                receipt,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8") + b"\n":
                raise ValueError
            actual = tuple(receipt["artifacts"])
            expected = tuple(
                {
                    "uri": reference.uri,
                    "sha256": reference.sha256,
                    "media_type": reference.media_type,
                    "verified": True,
                }
                for reference in (*children.values(), package_ref, source_ref)
            )
            if (
                receipt.get("schema") != "minibook.cas-readback-receipt.v1"
                or any(
                    not isinstance(item, dict)
                    or set(item) != {"uri", "sha256", "media_type", "verified"}
                    for item in actual
                )
                or actual != expected
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FactoryDispatchError("Captain V3 Minibook readback receipt is invalid") from exc
        resolved = ResolvedFactoryCandidate(
            candidate=candidate,
            source_archive=candidate_source_path,
        )
        self._v3_jobs.record_accepted_result(result)
        return resolved

    def _read_v3_exact(self, reference: ArtifactRef) -> bytes:
        try:
            content = self._artifacts.read_bytes(reference)
        except (OSError, RuntimeError, ValueError) as exc:
            raise FactoryDispatchError("Captain CAS readback digest mismatch") from exc
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise FactoryDispatchError("Captain CAS readback digest mismatch")
        return content

    @staticmethod
    def _read_bound_v3_source_path(path: Path, *, maximum_bytes: int) -> bytes:
        descriptor: int | None = None
        try:
            before = os.stat(path, follow_symlinks=False)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if (
                not stat.S_ISREG(before.st_mode)
                or path.is_symlink()
                or (
                    reparse_flag
                    and getattr(before, "st_file_attributes", 0) & reparse_flag
                )
            ):
                raise OSError
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode))
                != (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
                or opened.st_size != before.st_size
            ):
                raise OSError
            content = bytearray()
            while len(content) <= maximum_bytes:
                chunk = os.read(
                    descriptor,
                    min(64 * 1024, maximum_bytes + 1 - len(content)),
                )
                if not chunk:
                    break
                content.extend(chunk)
            after_open = os.fstat(descriptor)
            after_path = os.stat(path, follow_symlinks=False)
            if (
                len(content) != maximum_bytes
                or (
                    after_open.st_dev,
                    after_open.st_ino,
                    after_open.st_size,
                    after_open.st_mtime_ns,
                )
                != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                )
                or (
                    after_path.st_dev,
                    after_path.st_ino,
                    after_path.st_size,
                    after_path.st_mtime_ns,
                )
                != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                )
            ):
                raise OSError
            return bytes(content)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _safe_v3_zip_files(content: bytes) -> dict[PurePosixPath, bytes]:
        if not content or len(content) > 16 * 1024 * 1024:
            raise ValueError("candidate source ZIP size is invalid")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > 4096:
                raise ValueError("candidate source ZIP member count is invalid")
            files: dict[PurePosixPath, bytes] = {}
            total_size = 0
            for info in infos:
                path = PurePosixPath(info.filename)
                mode = info.external_attr >> 16
                if (
                    info.is_dir()
                    or info.flag_bits & 1
                    or "\\" in info.filename
                    or "\x00" in info.filename
                    or path.is_absolute()
                    or not path.parts
                    or ".." in path.parts
                    or ":" in path.parts[0]
                    or stat.S_ISLNK(mode)
                    or path in files
                    or info.file_size > 2 * 1024 * 1024
                    or (
                        info.compress_size > 0
                        and info.file_size / info.compress_size > 200
                    )
                ):
                    raise ValueError("candidate source ZIP member is unsafe")
                total_size += info.file_size
                if total_size > 8 * 1024 * 1024:
                    raise ValueError("candidate source ZIP expands beyond its limit")
                files[path] = archive.read(info)
        return files

    def _validate_v3_receipts(
        self,
        job: AgentFactoryJobV3,
        authority: CreationJobV3,
        assignment: FactoryBuildAssignmentV2,
        effect: FactoryBuildEffectV2,
        result: CreationResultV2,
        contents: dict[tuple[str, str, str], bytes],
        declared_test_paths: tuple[str, ...],
    ) -> CodexBuildReceiptV1:
        try:
            hermes_ref = _runtime_ref(result.hermes_usage_receipt_ref)
            hermes = ForgeBuildSkillUsageReceiptV1.model_validate_json(contents[_artifact_identity(hermes_ref)])
            codex_ref = _runtime_ref(result.codex_build_receipt_ref)
            codex = CodexBuildReceiptV1.model_validate_json(contents[_artifact_identity(codex_ref)])
            builds = tuple(
                PackageBuildEvidenceV1.model_validate_json(contents[_artifact_identity(_runtime_ref(ref))])
                for ref in result.build_evidence_refs
            )
            cases = tuple(
                PackageRealCaseEvidenceV1.model_validate_json(contents[_artifact_identity(_runtime_ref(ref))])
                for ref in result.real_case_evidence_refs
            )
            preliminary_team = json.loads(
                self._read_v3_exact(_runtime_ref(codex.candidate_manifest_ref))
            )
            final_team = json.loads(
                contents[_artifact_identity(_runtime_ref(result.team_manifest_ref))]
            )
        except (KeyError, ValueError) as exc:
            raise FactoryDispatchError("Captain V3 build or test evidence is invalid") from exc
        common = (authority.creation_job_id, job.job_id, job.correlation_id, job.subject_version, result.attempt)
        assignment_identity = (
            assignment.assignment_id,
            assignment.checkpoint_version,
            assignment.effect_key,
            effect.source_archive_ref,
            effect.codex_session_ref,
        )
        if (
            (hermes.creation_job_id, hermes.factory_job_id, hermes.correlation_id, hermes.subject_version, hermes.attempt) != common
            or (codex.creation_job_id, codex.factory_job_id, codex.correlation_id, codex.subject_version, codex.attempt) != common
            or hermes.public_assertion_ids != job.acceptance_assertion_ids
            or hermes.idempotency_key != authority.idempotency_key
            or hermes.released_skill != authority.released_skill
            or codex.acceptance_assertion_ids != job.acceptance_assertion_ids
            or any((item.creation_job_id, item.factory_job_id, item.correlation_id, item.subject_version, item.attempt) != common for item in (*builds, *cases))
            or any(item.outcome != "passed" for item in (*builds, *cases))
            or any(item.assertion_ids != job.acceptance_assertion_ids for item in cases)
            or any(
                (
                    item.assignment_id,
                    item.checkpoint_version,
                    item.effect_key,
                    item.source_archive_ref,
                    item.codex_session_ref,
                )
                != assignment_identity
                for item in (*builds, *cases)
            )
            or any(item.workspace_snapshot_ref != effect.workspace_snapshot_ref for item in builds)
            or any(item.evidence_refs != result.build_evidence_refs for item in cases)
            or any(item.declared_test_paths != declared_test_paths for item in cases)
            or hermes.evidence_refs
            != (
                authority.compiled_spec_ref,
                authority.blueprint_ref,
                authority.tool_plan_ref,
            )
            or codex.assignment_id != assignment.assignment_id
            or codex.idempotency_key != assignment.effect_key
            or codex.build_brief_ref != authority.blueprint_ref
            or codex.workspace_ref != assignment.workspace_ref
            or codex.codex_session_ref != effect.codex_session_ref
            or codex.workspace_snapshot_ref != effect.workspace_snapshot_ref
            or codex.source_archive_ref != effect.source_archive_ref
            or codex.candidate_manifest_ref != effect.preliminary_team_manifest_ref
            or preliminary_team != final_team
            or codex.test_evidence_refs != effect.test_evidence_refs
        ):
            raise FactoryDispatchError("Captain V3 build or test evidence is stale or failed")
        return codex

    def candidate_for(self, job: FactoryJob) -> ResolvedFactoryCandidate:
        if self._v3_jobs is not None and isinstance(job, AgentFactoryJobV3):
            block = self._authoritative_block(job)
            result = self._v3_jobs.accepted_result_for(job, block.attempt)
            return self._accept_v3(job, result)
        block = self._authoritative_block(job)
        resolved, _ = self._resolve_block(job, block)
        return resolved

    def current_candidate_ref(
        self,
        job: FactoryJob,
        attempt: int,
    ) -> ArtifactRef | None:
        if self._v3_jobs is not None and isinstance(job, AgentFactoryJobV3):
            try:
                self._authoritative_block(job, attempt=attempt)
                result = self._v3_jobs.accepted_result_for(job, attempt)
            except (FactoryDispatchError, FileNotFoundError):
                return None
            return self._accept_v3(job, result).candidate.source_archive_ref
        try:
            block = self._authoritative_block(job, attempt=attempt)
        except FileNotFoundError:
            return None
        resolved, _ = self._resolve_block(job, block)
        return resolved.candidate.source_archive_ref

    def _authoritative_block(
        self,
        job: FactoryJob,
        *,
        attempt: int | None = None,
    ) -> FactoryEvidenceBlock:
        candidates = tuple(
            block
            for block in self._repository.blocks(job.job_id)
            if block.phase is FactoryPhase.AGENT_CODE_CREATED
            and block.status is FactoryBlockStatus.SUCCEEDED
            and block.job_id == job.job_id
            and block.correlation_id == job.correlation_id
            and block.subject_version == job.subject_version
            and (attempt is None or block.attempt == attempt)
        )
        if not candidates:
            raise FileNotFoundError("authoritative Gateway candidate reference is unavailable")
        highest_attempt = max(block.attempt for block in candidates)
        latest = tuple(block for block in candidates if block.attempt == highest_attempt)
        package_refs = {
            _artifact_identity(block.artifact_refs[0])
            for block in latest
            if block.artifact_refs
        }
        if len(package_refs) != 1 or any(not block.artifact_refs for block in latest):
            raise FactoryDispatchError(
                "authoritative Gateway candidate reference is conflicting"
            )
        return max(latest, key=lambda block: (block.occurred_at, str(block.event_id)))

    def _resolve_block(
        self,
        job: FactoryJob,
        block: FactoryEvidenceBlock,
    ) -> tuple[
        ResolvedFactoryCandidate,
        CreationPackageManifestV1 | CreationPackageManifestV2,
    ]:
        return self._resolve(
            job=job,
            attempt=block.attempt,
            package_ref=block.artifact_refs[0],
            artifact_refs=block.artifact_refs[1:],
            skill_usage_receipt_ref=(
                block.evidence_refs[-1] if block.evidence_refs else None
            ),
        )

    def _resolve(
        self,
        *,
        job: FactoryJob,
        attempt: int,
        package_ref: ArtifactRef,
        artifact_refs: tuple[ArtifactRef, ...],
        skill_usage_receipt_ref: ArtifactRef | None,
    ) -> tuple[
        ResolvedFactoryCandidate,
        CreationPackageManifestV1 | CreationPackageManifestV2,
    ]:
        if package_ref.media_type != "application/json":
            raise FactoryDispatchError("Forge package manifest media type is invalid")
        try:
            package_payload = json.loads(self._artifacts.read_bytes(package_ref))
            if not isinstance(package_payload, dict):
                raise ValueError("package manifest must be an object")
            package_type = (
                CreationPackageManifestV2
                if package_payload.get("schema")
                == "minibook.creation-package-manifest.v2"
                else CreationPackageManifestV1
            )
            package = package_type.model_validate(package_payload)
        except (OSError, ValueError) as exc:
            raise FactoryDispatchError(
                "Forge package manifest bytes are unavailable or invalid"
            ) from exc
        if (
            package.factory_job_id != job.job_id
            or package.correlation_id != job.correlation_id
            or package.subject_version != job.subject_version
            or package.attempt != attempt
        ):
            raise FactoryDispatchError("Forge package manifest does not match factory job")
        package_skill_ref = _runtime_ref(package.skill_usage_receipt_ref)
        if (
            skill_usage_receipt_ref is None
            or _artifact_identity(package_skill_ref)
            != _artifact_identity(skill_usage_receipt_ref)
        ):
            raise FactoryDispatchError(
                "Forge skill usage receipt reference is not bound by agent-code evidence"
            )
        try:
            skill_usage_receipt = ForgeBuildSkillUsageReceiptV1.model_validate_json(
                self._artifacts.read_bytes(package_skill_ref)
            )
        except (OSError, ValueError) as exc:
            raise FactoryDispatchError(
                "Forge skill usage receipt bytes are unavailable or invalid"
            ) from exc
        self._require_skill_usage_receipt(
            job=job,
            attempt=attempt,
            package=package,
            receipt=skill_usage_receipt,
        )

        candidate_ref = _runtime_ref(package.candidate_manifest_ref)
        source_ref = _runtime_ref(package.source_archive_ref)
        bound = {_artifact_identity(reference) for reference in artifact_refs}
        if {
            _artifact_identity(candidate_ref),
            _artifact_identity(source_ref),
        } - bound:
            raise FactoryDispatchError(
                "Forge package manifest references are not bound by agent-code evidence"
            )
        codex_build_receipt: CodexBuildReceiptV1 | None = None
        if isinstance(package, CreationPackageManifestV2):
            codex_receipt_ref = _runtime_ref(package.codex_build_receipt_ref)
            if _artifact_identity(codex_receipt_ref) not in bound:
                raise FactoryDispatchError(
                    "Forge Codex build receipt is not bound by agent-code evidence"
                )
            try:
                codex_build_receipt = CodexBuildReceiptV1.model_validate_json(
                    self._artifacts.read_bytes(codex_receipt_ref)
                )
            except (OSError, ValueError) as exc:
                raise FactoryDispatchError(
                    "Forge Codex build receipt bytes are unavailable or invalid"
                ) from exc
            if codex_build_receipt_sha256(codex_build_receipt) != (
                codex_receipt_ref.sha256
            ):
                raise FactoryDispatchError(
                    "Forge Codex build receipt canonical digest changed"
                )
        try:
            candidate = FactoryCandidateManifest.model_validate_json(
                self._artifacts.read_bytes(candidate_ref)
            )
            source_bytes = self._artifacts.read_bytes(source_ref)
            source_path = self._artifacts.local_path(source_ref)
            if (
                not source_path.is_file()
                or hashlib.sha256(source_path.read_bytes()).hexdigest()
                != source_ref.sha256
                or hashlib.sha256(source_bytes).hexdigest() != source_ref.sha256
            ):
                raise FactoryDispatchError(
                    "Forge local source archive differs from verified CAS bytes"
                )
        except (OSError, ValueError) as exc:
            raise FactoryDispatchError(
                "Forge candidate bytes are unavailable or invalid"
            ) from exc
        if _artifact_identity(candidate.source_archive_ref) != _artifact_identity(source_ref):
            raise FactoryDispatchError(
                "Forge candidate manifest does not match source archive"
            )
        if codex_build_receipt is not None:
            self._require_codex_build_receipt(
                job=job,
                attempt=attempt,
                package=package,
                receipt=codex_build_receipt,
                source_archive=source_path,
            )
        return (
            ResolvedFactoryCandidate(candidate=candidate, source_archive=source_path),
            package,
        )

    @staticmethod
    def _require_codex_build_receipt(
        *,
        job: FactoryJob,
        attempt: int,
        package: CreationPackageManifestV2,
        receipt: CodexBuildReceiptV1,
        source_archive: Path,
    ) -> None:
        if (
            receipt.factory_job_id != job.job_id
            or receipt.creation_job_id != package.creation_job_id
            or receipt.correlation_id != job.correlation_id
            or receipt.subject_version != job.subject_version
            or receipt.attempt != attempt
            or receipt.acceptance_assertion_ids != job.acceptance_assertion_ids
            or receipt.source_archive_ref.sha256
            != package.source_archive_ref.sha256
            or receipt.source_archive_ref.media_type
            != package.source_archive_ref.media_type
        ):
            raise FactoryDispatchError(
                "Forge Codex build receipt does not match factory job or source"
            )
        try:
            with zipfile.ZipFile(source_archive) as archive:
                candidates = tuple(
                    item
                    for item in archive.infolist()
                    if PurePosixPath(item.filename).as_posix()
                    == "factory-candidate.json"
                )
                if len(candidates) != 1:
                    raise ValueError
                candidate_bytes = archive.read(candidates[0])
        except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
            raise FactoryDispatchError(
                "Forge source archive lacks the unique Captain candidate manifest"
            ) from exc
        if (
            receipt.candidate_manifest_ref.media_type != "application/json"
            or hashlib.sha256(candidate_bytes).hexdigest()
            != receipt.candidate_manifest_ref.sha256
        ):
            raise FactoryDispatchError(
                "Forge source archive does not match the Captain candidate manifest"
            )

    def _require_skill_usage_receipt(
        self,
        *,
        job: FactoryJob,
        attempt: int,
        package: CreationPackageManifestV1,
        receipt: ForgeBuildSkillUsageReceiptV1,
    ) -> None:
        if (
            receipt.creation_job_id != package.creation_job_id
            or receipt.factory_job_id != job.job_id
            or receipt.correlation_id != job.correlation_id
            or receipt.subject_version != job.subject_version
            or receipt.attempt != attempt
            or receipt.public_assertion_ids != job.acceptance_assertion_ids
        ):
            raise FactoryDispatchError(
                "Forge skill usage receipt does not match factory job"
            )
        released = self._repository.released_for(job, FactorySkillStep.BRIEF_CODEX)
        if (
            receipt.released_skill.skill_id != released.skill_id
            or receipt.released_skill.version != released.version
            or receipt.released_skill.content_sha256 != released.content_sha256
            or _artifact_identity(_runtime_ref(receipt.released_skill.content_ref))
            != _artifact_identity(released.content_ref)
        ):
            raise FactoryDispatchError(
                "Forge skill usage receipt does not match Captain released skill"
            )


def _runtime_ref(reference: object) -> ArtifactRef:
    dump = getattr(reference, "model_dump", None)
    if not callable(dump):
        raise FactoryDispatchError("Forge artifact reference is invalid")
    return ArtifactRef.model_validate(dump(mode="json"))


def _artifact_identity(reference: ArtifactRef) -> tuple[str, str, str]:
    return reference.uri, reference.sha256, reference.media_type


class FactoryTeamExecutionPort(Protocol):
    """Production real-case boundary, implemented with TeamExecutionService."""

    def invocation_for(
        self,
        request: FactoryDispatch,
    ) -> FactorySkillInvocationV1: ...

    async def execute(
        self,
        request: FactoryDispatch,
        candidate: ResolvedFactoryCandidate,
    ) -> TeamExecutionEvidenceV1: ...


class FactoryTeamExecutionArtifactSink(Protocol):
    """Captain-owned append-only sink for validated team execution evidence."""

    def record_workflow_artifact(
        self,
        artifact: TeamExecutionEvidenceV1,
    ) -> bool: ...


class CandidateEvaluationFactory:
    """Emit leased Hermes lifecycle blocks from independently persisted evaluation evidence."""

    def __init__(
        self,
        *,
        provider: FactoryCandidateProvider,
        evidence_store: FactoryEvidenceStore,
        evaluator: FactoryCandidateEvaluator | None = None,
        team_execution: FactoryTeamExecutionPort | None = None,
        workflow_artifacts: FactoryTeamExecutionArtifactSink | None = None,
    ) -> None:
        self._provider = provider
        self._evidence_store = evidence_store
        self._evaluator = evaluator or FactoryCandidateEvaluator()
        self._team_execution = team_execution
        self._workflow_artifacts = workflow_artifacts

    async def dispatch(self, request: FactoryDispatch) -> FactoryEvidenceBlock:
        if request.action.kind is FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE:
            raise FactoryDispatchError(
                "agent code evidence requires the exact Forge CreationResultV1"
            )
        phase, role = _validation_phase(request.action.kind)
        if request.role is not role or request.lease is None or request.lease.role is not role:
            raise FactoryDispatchError("candidate validation requires the matching active factory lease")
        validate_factory_lease(
            request.lease,
            job=request.job,
            role=role,
            attempt=request.action.attempt,
            now=request.lease.issued_at,
        )
        if request.action.kind in {
            FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
            FactoryActionKind.DISPATCH_TECHNICAL_REVALIDATION,
        }:
            if self._team_execution is None:
                raise FactoryDispatchError(
                    "real-case dispatch requires a configured TeamExecutionService"
                )
        try:
            resolved = self._provider.candidate_for(request.job)
            if request.action.kind in {
                FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
                FactoryActionKind.DISPATCH_TECHNICAL_REVALIDATION,
            }:
                assert self._team_execution is not None
                required_runs = (
                    request.job.execution_policy.required_live_runs
                    if isinstance(request.job, AgentFactoryJobV3)
                    and request.action.kind
                    is FactoryActionKind.DISPATCH_REAL_CASE_TESTER
                    else 1
                )
                if required_runs > 1:
                    invocations_for = getattr(
                        self._team_execution,
                        "invocations_for",
                        None,
                    )
                    execute_required = getattr(
                        self._team_execution,
                        "execute_required",
                        None,
                    )
                    if not callable(invocations_for) or not callable(execute_required):
                        raise FactoryDispatchError(
                            "release real-case dispatch requires the multi-run execution port"
                        )
                    expected_invocations = tuple(invocations_for(request))
                    team_evidences = tuple(
                        await execute_required(request, resolved)
                    )
                    if (
                        len(expected_invocations) != required_runs
                        or len(team_evidences) != required_runs
                        or tuple(item.run_number for item in team_evidences)
                        != tuple(range(1, required_runs + 1))
                    ):
                        raise FactoryDispatchError(
                            "release real-case dispatch returned incomplete run evidence"
                        )
                    blocks: list[FactoryEvidenceBlock] = []
                    for expected_invocation, team_evidence in zip(
                        expected_invocations,
                        team_evidences,
                        strict=True,
                    ):
                        sealed = await self._evidence_store.persist(
                            request.job,
                            team_evidence.model_dump_json(
                                by_alias=True, exclude_none=True
                            ).encode("utf-8"),
                        )
                        blocks.append(
                            _team_execution_block(
                                request,
                                resolved,
                                team_evidence,
                                sealed,
                                expected_invocation=expected_invocation,
                            )
                        )
                        if self._workflow_artifacts is not None:
                            self._workflow_artifacts.record_workflow_artifact(
                                team_evidence
                            )
                    return _team_execution_bundle_block(request, tuple(blocks))
                expected_invocation = self._team_execution.invocation_for(request)
                team_evidence = await self._team_execution.execute(request, resolved)
                if (
                    request.action.kind
                    is FactoryActionKind.DISPATCH_TECHNICAL_REVALIDATION
                ):
                    assert request.action.authorization_ref is not None
                    assert request.action.supersedes_ref is not None
                    team_evidence = team_evidence.model_copy(
                        update={
                            "evidence_refs": tuple(
                                dict.fromkeys(
                                    (
                                        *team_evidence.evidence_refs,
                                        request.action.authorization_ref,
                                        request.action.supersedes_ref,
                                    )
                                )
                            )
                        }
                    )
                sealed = await self._evidence_store.persist(
                    request.job,
                    team_evidence.model_dump_json(
                        by_alias=True, exclude_none=True
                    ).encode("utf-8"),
                )
                block = _team_execution_block(
                    request,
                    resolved,
                    team_evidence,
                    sealed,
                    expected_invocation=expected_invocation,
                )
                if self._workflow_artifacts is not None:
                    self._workflow_artifacts.record_workflow_artifact(team_evidence)
                return block
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
        assertions = (
            result.assertion_ids
            if result.status == "succeeded"
            and phase in {FactoryPhase.REAL_CASE_EVIDENCE, FactoryPhase.QUALITY_REVIEWED}
            else ()
        )
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

    async def record_creation_result(
        self,
        request: FactoryDispatch,
        result: CreationResultV1 | CreationResultV2,
    ) -> FactoryEvidenceBlock:
        if request.action.kind is not FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE:
            raise FactoryDispatchError(
                "CreationResultV1 may only produce agent code evidence"
            )
        role = FactoryRole.TOOL_INTEGRATOR
        if request.role is not role or request.lease is None or request.lease.role is not role:
            raise FactoryDispatchError(
                "agent code evidence requires the matching active factory lease"
            )
        validate_factory_lease(
            request.lease,
            job=request.job,
            role=role,
            attempt=request.action.attempt,
            now=request.lease.issued_at,
        )
        if (
            result.correlation_id != request.job.correlation_id
            or result.subject_version != request.job.subject_version
            or result.attempt != request.action.attempt
        ):
            raise FactoryDispatchError(
                "Minibook CreationResultV1 does not match the factory dispatch"
            )
        if result.status != "succeeded":
            raise FactoryDispatchError(
                "Minibook CreationResultV1 did not produce successful agent code"
            )
        if isinstance(result, CreationResultV1) and not result.artifact_refs:
            raise FactoryDispatchError(
                "successful Minibook CreationResultV1 has no generated code artifacts"
            )
        self._provider.accept_creation_result(request.job, result)
        assert result.package_manifest_ref is not None
        content = json.dumps(
            result.model_dump(mode="json", by_alias=True, exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        sealed = await self._evidence_store.persist(request.job, content)
        package_manifest_ref = ArtifactRef.model_validate(
            result.package_manifest_ref.model_dump(mode="json")
        )
        if isinstance(result, CreationResultV2):
            artifact_refs = tuple(
                ArtifactRef.model_validate(reference.model_dump(mode="json"))
                for reference in (
                    result.team_manifest_ref,
                    result.candidate_manifest_ref,
                    result.source_archive_ref,
                )
                if reference is not None
            )
            forge_evidence_refs = tuple(
                ArtifactRef.model_validate(reference.model_dump(mode="json"))
                for reference in (
                    result.hermes_usage_receipt_ref,
                    result.codex_build_receipt_ref,
                    *result.build_evidence_refs,
                    *result.real_case_evidence_refs,
                    result.cas_readback_receipt_ref,
                )
                if reference is not None
            )
        else:
            assert result.skill_usage_receipt_ref is not None
            artifact_refs = tuple(
                ArtifactRef.model_validate(reference.model_dump(mode="json"))
                for reference in result.artifact_refs
            )
            forge_evidence_refs = tuple(
                ArtifactRef.model_validate(reference.model_dump(mode="json"))
                for reference in result.evidence_refs
            ) + (
                ArtifactRef.model_validate(
                    result.skill_usage_receipt_ref.model_dump(mode="json")
                ),
            )
        return FactoryEvidenceBlock(
            schema_name="captain.agent-factory-block.v1",
            event_id=uuid5(
                NAMESPACE_URL,
                f"factory-creation-result|{request.job.job_id}|{request.action.attempt}|{sealed.sha256}",
            ),
            job_id=request.job.job_id,
            correlation_id=request.job.correlation_id,
            causation_id=request.job.event_id,
            occurred_at=request.lease.issued_at,
            producer="hermes",
            subject_version=request.job.subject_version,
            attempt=request.action.attempt,
            phase=FactoryPhase.AGENT_CODE_CREATED,
            role=role,
            status=FactoryBlockStatus.SUCCEEDED,
            artifact_refs=(package_manifest_ref, *artifact_refs),
            evidence_refs=(sealed, *forge_evidence_refs),
            assertion_ids=(),
            lease_id=request.lease.lease_id,
        )


def _validation_phase(action: FactoryActionKind) -> tuple[FactoryPhase, FactoryRole]:
    phases = {
        FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE: (FactoryPhase.AGENT_CODE_CREATED, FactoryRole.TOOL_INTEGRATOR),
        FactoryActionKind.DISPATCH_BUILD_VALIDATOR: (FactoryPhase.BUILD_PASSED, FactoryRole.TOOL_INTEGRATOR),
        FactoryActionKind.DISPATCH_REAL_CASE_TESTER: (FactoryPhase.REAL_CASE_EVIDENCE, FactoryRole.REAL_CASE_TESTER),
        FactoryActionKind.DISPATCH_TECHNICAL_REVALIDATION: (FactoryPhase.REAL_CASE_REVALIDATED, FactoryRole.REAL_CASE_TESTER),
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


def _team_execution_bundle_block(
    request: FactoryDispatch,
    blocks: tuple[FactoryEvidenceBlock, ...],
) -> FactoryEvidenceBlock:
    """Collapse complete release-run evidence into one lifecycle transition."""

    if (
        not isinstance(request.job, AgentFactoryJobV3)
        or len(blocks) != request.job.execution_policy.required_live_runs
        or not blocks
    ):
        raise FactoryDispatchError("release team execution bundle is incomplete")
    first = blocks[0]
    if any(
        block.job_id != first.job_id
        or block.attempt != first.attempt
        or block.phase is not first.phase
        or block.lease_id != first.lease_id
        for block in blocks
    ):
        raise FactoryDispatchError("release team execution bundle is stale or mixed")
    succeeded = all(
        block.status is FactoryBlockStatus.SUCCEEDED for block in blocks
    )
    artifact_refs = tuple(
        dict.fromkeys(
            reference for block in blocks for reference in block.artifact_refs
        )
    )
    evidence_refs = tuple(
        dict.fromkeys(
            reference for block in blocks for reference in block.evidence_refs
        )
    )
    identity = "|".join(str(block.event_id) for block in blocks)
    return first.model_copy(
        update={
            "event_id": uuid5(
                NAMESPACE_URL,
                f"factory-team-execution-bundle|{identity}",
            ),
            "occurred_at": max(block.occurred_at for block in blocks),
            "status": (
                FactoryBlockStatus.SUCCEEDED
                if succeeded
                else FactoryBlockStatus.FAILED
            ),
            "artifact_refs": artifact_refs,
            "evidence_refs": evidence_refs,
            "assertion_ids": (
                request.job.acceptance_assertion_ids if succeeded else ()
            ),
        }
    )


def _team_execution_block(
    request: FactoryDispatch,
    resolved: ResolvedFactoryCandidate,
    evidence: TeamExecutionEvidenceV1,
    sealed: ArtifactRef,
    *,
    expected_invocation: FactorySkillInvocationV1,
) -> FactoryEvidenceBlock:
    assert request.lease is not None
    if (
        evidence.job_id != request.job.job_id
        or evidence.correlation_id != request.job.correlation_id
        or evidence.subject_version != request.job.subject_version
        or evidence.attempt != request.action.attempt
        or evidence.invocation.lease != request.lease
        or evidence.invocation != expected_invocation
        or evidence.invocation.step is not FactorySkillStep.EXECUTE_TEAM
        or evidence.invocation.acceptance_assertion_ids
        != request.job.acceptance_assertion_ids
        or evidence.acceptance_assertion_ids
        != request.job.acceptance_assertion_ids
        or evidence.candidate_ref != resolved.candidate.source_archive_ref
        or evidence.holdout_ref not in request.job.private_holdout_refs
    ):
        raise FactoryDispatchError(
            "TeamExecutionService evidence does not match the factory dispatch"
        )
    status = (
        FactoryBlockStatus.SUCCEEDED
        if evidence.status == "succeeded"
        else FactoryBlockStatus.FAILED
    )
    return FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id=uuid5(
            NAMESPACE_URL,
            f"factory-team-execution|{request.job.job_id}|{request.action.attempt}|{sealed.sha256}",
        ),
        job_id=request.job.job_id,
        correlation_id=request.job.correlation_id,
        causation_id=request.job.event_id,
        occurred_at=evidence.occurred_at,
        producer="hermes",
        subject_version=request.job.subject_version,
        attempt=request.action.attempt,
        phase=(
            FactoryPhase.REAL_CASE_REVALIDATED
            if request.action.kind
            is FactoryActionKind.DISPATCH_TECHNICAL_REVALIDATION
            else FactoryPhase.REAL_CASE_EVIDENCE
        ),
        role=FactoryRole.REAL_CASE_TESTER,
        status=status,
        artifact_refs=(
            resolved.candidate.source_archive_ref,
            resolved.candidate.team_manifest.reference,
            evidence.artifact_ref,
        ),
        evidence_refs=(sealed, *evidence.evidence_refs),
        assertion_ids=(
            evidence.acceptance_assertion_ids
            if evidence.status == "succeeded"
            else ()
        ),
        lease_id=request.lease.lease_id,
    )
