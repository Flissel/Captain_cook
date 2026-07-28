"""Fail-closed isolated evaluation for a sealed generated agent candidate."""

from __future__ import annotations

import hashlib
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
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from agenten.agent_factory.contracts import (
    AgentFactoryJob,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryJob,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.evidence_store import FactoryEvidenceStore
from agenten.agent_factory.forge_contracts import (
    CreationPackageManifestV1,
    CreationResultV1,
)
from agenten.agent_factory.leases import validate_factory_lease
from agenten.agent_factory.n8n_tools import OpaqueN8nToolReference, TypedN8nTool
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.skill_evaluation import HermesSkillEvaluationRequest
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
        if any(agent.handoffs for agent in self.agents) and self.max_handoffs == 0:
            raise ValueError("handoff topology requires a positive max_handoffs ceiling")
        return self


class FactoryCandidateManifest(_FrozenModel):
    """The only executable input accepted by the factory evaluator."""

    schema_name: Literal["captain.factory-candidate.v1"] = "captain.factory-candidate.v1"
    candidate_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    source_archive_ref: ArtifactRef
    team_manifest: FactoryCandidateArtifact
    workflow_artifacts: tuple[FactoryCandidateArtifact, ...] = ()
    tool_schema_artifacts: tuple[FactoryCandidateArtifact, ...] = ()
    n8n_tools: tuple[TypedN8nTool, ...] = ()
    n8n_tool_references: tuple[OpaqueN8nToolReference, ...] = ()
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
        tool_names = tuple(tool.name for tool in manifest.n8n_tools)
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
            context={"allowed_tools": {tool.name for tool in candidate.n8n_tools}},
        )

    @staticmethod
    def _verify_system_prompts(
        manifest: FactoryAutoGenTeamManifestV1,
        workspace: Path,
    ) -> None:
        digests = {
            hashlib.sha256(path.read_bytes()).hexdigest()
            for path in workspace.rglob("*")
            if path.is_file()
        }
        if any(
            agent.system_prompt_ref.sha256 not in digests
            for agent in manifest.agents
        ):
            raise ValueError("system prompt ref is not sealed in the candidate archive")

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
        result: CreationResultV1,
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


class FactoryBlockSource(Protocol):
    def blocks(self, job_id: object) -> tuple[FactoryEvidenceBlock, ...]: ...


class GatewayForgeCandidateProvider(FactoryCandidateProvider):
    """Resolve candidates only from SHA-verified Forge bytes and Gateway blocks."""

    def __init__(
        self,
        *,
        repository: FactoryBlockSource,
        artifacts: ForgeCandidateArtifactStore,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts

    def accept_creation_result(
        self,
        job: FactoryJob,
        result: CreationResultV1,
    ) -> ResolvedFactoryCandidate:
        if result.package_manifest_ref is None:
            raise FactoryDispatchError("Forge package manifest reference is missing")
        package_ref = _runtime_ref(result.package_manifest_ref)
        artifact_refs = tuple(_runtime_ref(reference) for reference in result.artifact_refs)
        resolved, package = self._resolve(
            job=job,
            attempt=result.attempt,
            package_ref=package_ref,
            artifact_refs=artifact_refs,
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

    def candidate_for(self, job: FactoryJob) -> ResolvedFactoryCandidate:
        block = self._authoritative_block(job)
        resolved, _ = self._resolve_block(job, block)
        return resolved

    def current_candidate_ref(
        self,
        job: FactoryJob,
        attempt: int,
    ) -> ArtifactRef | None:
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
    ) -> tuple[ResolvedFactoryCandidate, CreationPackageManifestV1]:
        return self._resolve(
            job=job,
            attempt=block.attempt,
            package_ref=block.artifact_refs[0],
            artifact_refs=block.artifact_refs[1:],
        )

    def _resolve(
        self,
        *,
        job: FactoryJob,
        attempt: int,
        package_ref: ArtifactRef,
        artifact_refs: tuple[ArtifactRef, ...],
    ) -> tuple[ResolvedFactoryCandidate, CreationPackageManifestV1]:
        if package_ref.media_type != "application/json":
            raise FactoryDispatchError("Forge package manifest media type is invalid")
        try:
            package = CreationPackageManifestV1.model_validate_json(
                self._artifacts.read_bytes(package_ref)
            )
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
        return (
            ResolvedFactoryCandidate(candidate=candidate, source_archive=source_path),
            package,
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


class CandidateEvaluationFactory:
    """Emit leased Hermes lifecycle blocks from independently persisted evaluation evidence."""

    def __init__(
        self,
        *,
        provider: FactoryCandidateProvider,
        evidence_store: FactoryEvidenceStore,
        evaluator: FactoryCandidateEvaluator | None = None,
        team_execution: FactoryTeamExecutionPort | None = None,
    ) -> None:
        self._provider = provider
        self._evidence_store = evidence_store
        self._evaluator = evaluator or FactoryCandidateEvaluator()
        self._team_execution = team_execution

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
        if request.action.kind is FactoryActionKind.DISPATCH_REAL_CASE_TESTER:
            if self._team_execution is None:
                raise FactoryDispatchError(
                    "real-case dispatch requires a configured TeamExecutionService"
                )
        try:
            resolved = self._provider.candidate_for(request.job)
            if request.action.kind is FactoryActionKind.DISPATCH_REAL_CASE_TESTER:
                assert self._team_execution is not None
                expected_invocation = self._team_execution.invocation_for(request)
                team_evidence = await self._team_execution.execute(request, resolved)
                sealed = await self._evidence_store.persist(
                    request.job,
                    team_evidence.model_dump_json(
                        by_alias=True, exclude_none=True
                    ).encode("utf-8"),
                )
                return _team_execution_block(
                    request,
                    resolved,
                    team_evidence,
                    sealed,
                    expected_invocation=expected_invocation,
                )
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
        result: CreationResultV1,
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
        if not result.artifact_refs:
            raise FactoryDispatchError(
                "successful Minibook CreationResultV1 has no generated code artifacts"
            )
        self._provider.accept_creation_result(request.job, result)
        assert result.package_manifest_ref is not None
        assert result.skill_usage_receipt_ref is not None
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
        artifact_refs = tuple(
            ArtifactRef.model_validate(reference.model_dump(mode="json"))
            for reference in result.artifact_refs
        )
        forge_evidence_refs = tuple(
            ArtifactRef.model_validate(reference.model_dump(mode="json"))
            for reference in result.evidence_refs
        )
        skill_usage_receipt_ref = ArtifactRef.model_validate(
            result.skill_usage_receipt_ref.model_dump(mode="json")
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
            evidence_refs=(sealed, *forge_evidence_refs, skill_usage_receipt_ref),
            assertion_ids=(),
            lease_id=request.lease.lease_id,
        )


def _validation_phase(action: FactoryActionKind) -> tuple[FactoryPhase, FactoryRole]:
    phases = {
        FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE: (FactoryPhase.AGENT_CODE_CREATED, FactoryRole.TOOL_INTEGRATOR),
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
        phase=FactoryPhase.REAL_CASE_EVIDENCE,
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
