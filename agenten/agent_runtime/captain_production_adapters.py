"""Current-architecture Hermes, Codex, and content-addressed runtime ports."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid5

from agenten.agent_runtime.runtime_codex import (
    RuntimeCodexExecution,
    RuntimeCodexTerminalEvidenceV1,
    PowerShellRuntimeCodexRunner,
)
from agenten.agent_factory.codex_build_recovery import canonical_factory_codex_model
from agenten.agent_factory.hermes_cli import HermesCliFactory, HermesCliSettings
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    HermesPlanResult,
    RuntimeStatus,
)
from agenten.agent_runtime.confined_files import ConfinedFileError, ConfinedFileStore
from agenten.agent_runtime.production_bootstrap import (
    RuntimeAdapterBinding,
    RuntimeAdapterContext,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_ENVIRONMENT_NAME = re.compile(
    r"(?i)(?:^|_)(?:api[_-]?key|authorization|credential|password|private[_-]?key|secret|token)(?:$|_)"
)


class RuntimeEventObserver(Protocol):
    def __call__(
        self,
        event: dict[str, object],
    ) -> object | Awaitable[object]: ...


class ContentAddressedArtifactAdapter:
    """Exact-byte SHA-256 storage shared by all three production adapters."""

    def __init__(self, root: Path) -> None:
        self._store = ConfinedFileStore(root)

    def put(self, content: bytes, media_type: str) -> ArtifactRef:
        if not isinstance(content, bytes):
            raise TypeError("runtime artifact content must be bytes")
        digest = hashlib.sha256(content).hexdigest()
        try:
            self._store.write_once(
                f"{digest[:2]}/{digest}",
                content,
                conflict="runtime artifact digest collision or corruption",
            )
        except ConfinedFileError as exc:
            raise ValueError(str(exc)) from None
        return ArtifactRef(
            uri=f"artifact://sha256/{digest}",
            sha256=digest,
            media_type=media_type,
        )

    async def write(self, content: bytes, media_type: str) -> ArtifactRef:
        return self.put(content, media_type)

    async def require(self, reference: ArtifactRef) -> None:
        self.read_bytes(reference)

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        digest = reference.sha256
        if (
            _SHA256.fullmatch(digest) is None
            or reference.uri != f"artifact://sha256/{digest}"
        ):
            raise ValueError("runtime artifact reference is not content-addressed")
        try:
            content = self._store.read(f"{digest[:2]}/{digest}")
        except ConfinedFileError:
            raise ValueError("runtime artifact content is unavailable") from None
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("runtime artifact digest changed")
        return content

class CaptainHermesPlannerAdapter:
    """Runtime planning port over the current released-skill Hermes factory."""

    def __init__(
        self,
        *,
        factory: HermesCliFactory,
        artifacts: ContentAddressedArtifactAdapter,
        skill_name: str,
        released_skill_sha256: str,
        environ: Mapping[str, str],
    ) -> None:
        self._factory = factory
        self._artifacts = artifacts
        self._skill_name = skill_name
        self._released_skill_sha256 = released_skill_sha256
        self._environ = dict(environ)

    @classmethod
    def from_context(
        cls,
        context: RuntimeAdapterContext,
        *,
        artifacts: ContentAddressedArtifactAdapter | None = None,
    ) -> "CaptainHermesPlannerAdapter":
        skill_name = context.environ.get(
            "CAPTAIN_HERMES_RUNTIME_SKILL",
            "captain-agent-factory-loop",
        ).strip()
        released_digest = context.environ.get(
            "CAPTAIN_HERMES_RUNTIME_SKILL_SHA256",
            "",
        ).strip()
        if _SHA256.fullmatch(released_digest) is None:
            raise ValueError("Captain Hermes runtime skill release is not configured")
        skill_root = context.repository_root / "agenten" / "agent_factory" / "skills"
        hermes_workspace = ConfinedFileStore(
            context.repository_root
            / ".captain-cook"
            / "workspaces"
            / "hermes-runtime"
        ).root
        factory = HermesCliFactory(
            settings=HermesCliSettings(
                executable=(
                    context.environ.get("HERMES_EXECUTABLE", "hermes").strip()
                    or "hermes"
                ),
                skill_root=skill_root,
                evidence_root=(
                    context.repository_root
                    / ".captain-cook"
                    / "evidence"
                    / "hermes-runtime"
                ),
                released_skill_root=(
                    context.repository_root
                    / "agenten"
                    / "agent_factory"
                    / "released-skills"
                ),
                working_directory=hermes_workspace,
            )
        )
        factory.validate_runtime_skill(
            skill_name=skill_name,
            released_skill_sha256=released_digest,
        )
        return cls(
            factory=factory,
            artifacts=artifacts or ContentAddressedArtifactAdapter(context.artifact_root),
            skill_name=skill_name,
            released_skill_sha256=released_digest,
            environ=context.environ,
        )

    async def plan(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> HermesPlanResult:
        return await self._invoke(command, grant)

    async def design_agent(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> HermesPlanResult:
        return await self._invoke(command, grant)

    async def _invoke(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> HermesPlanResult:
        _require_grant(command, grant)
        prompt_bytes = self._artifacts.read_bytes(command.payload.prompt_ref)
        try:
            prompt = prompt_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FactoryDispatchError("Hermes runtime prompt is not UTF-8") from exc
        result = await self._factory.runtime_plan(
            command,
            grant,
            prompt,
            skill_name=self._skill_name,
            released_skill_sha256=self._released_skill_sha256,
        )
        _reject_private_evidence(
            result.model_dump(mode="json", by_alias=True),
            prompt=prompt,
            environ=self._environ,
        )
        return result


class CaptainCodexExecutionAdapter:
    """Five-method runtime port over the current Codex execution seam."""

    def __init__(
        self,
        *,
        execution: RuntimeCodexExecution,
        artifacts: ContentAddressedArtifactAdapter,
        repository_root: Path,
        observer: RuntimeEventObserver | None = None,
        environ: Mapping[str, str],
    ) -> None:
        self._execution = execution
        self._artifacts = artifacts
        self._repository_root = repository_root.resolve()
        self._observer = observer
        self._environ = dict(environ)

    @classmethod
    def from_context(
        cls,
        context: RuntimeAdapterContext,
        *,
        artifacts: ContentAddressedArtifactAdapter | None = None,
    ) -> "CaptainCodexExecutionAdapter":
        executable = (
            context.environ.get("CAPTAIN_CODEX_EXECUTABLE", "codex").strip()
            or "codex"
        )
        runner = PowerShellRuntimeCodexRunner(
            repository_root=context.repository_root,
            executable=executable,
            environ=context.environ,
            evidence_root=(
                context.repository_root / ".captain-cook" / "runtime-codex"
            ),
        )
        execution = RuntimeCodexExecution(
            runner=runner,
            checkpoint_root=(
                context.repository_root
                / ".captain-cook"
                / "runtime-codex"
                / "checkpoints"
            ),
        )
        return cls(
            execution=execution,
            artifacts=artifacts or ContentAddressedArtifactAdapter(context.artifact_root),
            repository_root=context.repository_root,
            environ=context.environ,
        )

    async def start(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        _require_grant(command, grant)
        prompt = self._prompt(command)
        workspace = self._workspace(grant.workspace_ref)
        evidence = await self._execution.start(
            command_id=command.event_id,
            correlation_id=command.correlation_id,
            subject_id=command.subject_id,
            prompt_sha256=command.payload.prompt_ref.sha256,
            workspace_ref=grant.workspace_ref,
            workspace=workspace,
            prompt=prompt,
            timeout_seconds=command.payload.limits.wall_seconds,
            observer=self._observe,
        )
        return self._result(command, grant, evidence)

    async def resume(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        _require_grant(command, grant)
        prompt = self._prompt(command)
        workspace = self._workspace(grant.workspace_ref)
        evidence = await self._execution.resume(
            correlation_id=command.correlation_id,
            causation_id=command.causation_id,
            subject_id=command.subject_id,
            prompt_sha256=command.payload.prompt_ref.sha256,
            workspace_ref=grant.workspace_ref,
            workspace=workspace,
            prompt=prompt,
            timeout_seconds=command.payload.limits.wall_seconds,
            observer=self._observe,
        )
        return self._result(command, grant, evidence)

    async def status(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        _require_grant(command, grant)
        workspace = self._workspace(grant.workspace_ref)
        evidence = self._execution.find_terminal(
            correlation_id=command.correlation_id,
            causation_id=command.causation_id,
            subject_id=command.subject_id,
            prompt_sha256=command.payload.prompt_ref.sha256,
            workspace_ref=grant.workspace_ref,
            workspace=workspace,
        )
        return (
            self._result(command, grant, evidence)
            if evidence is not None
            else self._running_result(command, grant)
        )

    async def cancel(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        _require_grant(command, grant)
        workspace = self._workspace(grant.workspace_ref)
        evidence = await self._execution.cancel(
            correlation_id=command.correlation_id,
            causation_id=command.causation_id,
            subject_id=command.subject_id,
            prompt_sha256=command.payload.prompt_ref.sha256,
            workspace_ref=grant.workspace_ref,
            workspace=workspace,
        )
        return self._result(command, grant, evidence)

    async def heartbeat(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        return await self.status(command, grant)

    def _prompt(self, command: AgentRuntimeCommand) -> str:
        try:
            return self._artifacts.read_bytes(command.payload.prompt_ref).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FactoryDispatchError("Codex runtime prompt is not UTF-8") from exc

    def _workspace(self, workspace_ref: str) -> Path:
        parsed = urlsplit(workspace_ref)
        if parsed.scheme != "workspace" or parsed.query or parsed.fragment:
            raise FactoryDispatchError("Codex workspace reference is invalid")
        components = (parsed.netloc, *parsed.path.strip("/").split("/"))
        if any(
            re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", component) is None
            for component in components
        ):
            raise FactoryDispatchError("Codex workspace reference is invalid")
        try:
            return ConfinedFileStore(
                self._repository_root / ".captain-cook" / "workspaces"
            ).require_directory(Path(*components))
        except ConfinedFileError:
            raise FactoryDispatchError(
                "Codex workspace is unavailable or outside its authorized root"
            ) from None

    async def _observe(self, event: dict[str, object]) -> None:
        if self._observer is None:
            return
        observed = self._observer(dict(event))
        if inspect.isawaitable(observed):
            await observed

    def _result(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
        evidence: RuntimeCodexTerminalEvidenceV1,
    ) -> AgentRuntimeResult:
        _reject_private_evidence(
            evidence.model_dump(mode="json", by_alias=True),
            prompt=self._prompt(command),
            environ=self._environ,
        )
        evidence_bytes = canonical_factory_codex_model(evidence)
        evidence_ref = self._artifacts.put(evidence_bytes, "application/json")
        evidence_refs = _unique_references(
            (
                evidence_ref,
                *(
                    (evidence.resumable_checkpoint,)
                    if evidence.resumable_checkpoint is not None
                    else ()
                ),
            )
        )
        status = {
            "succeeded": RuntimeStatus.SUCCEEDED,
            "failed": RuntimeStatus.FAILED,
            "timed_out": RuntimeStatus.INFRASTRUCTURE_FAILED,
            "cancelled": RuntimeStatus.CANCELLED,
        }[evidence.status]
        error = (
            "codex execution timed out (exit 124)"
            if evidence.status == "timed_out"
            else "codex execution failed"
            if evidence.status == "failed"
            else None
        )
        return AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=uuid5(command.event_id, "captain.runtime-result"),
            command_id=command.event_id,
            correlation_id=command.correlation_id,
            occurred_at=command.occurred_at,
            producer="agent-runtime",
            subject_id=command.subject_id,
            subject_version=command.subject_version,
            grant_id=grant.grant_id,
            operation=command.payload.operation,
            status=status,
            session_id=evidence.session_id,
            artifact_refs=(),
            evidence_refs=evidence_refs,
            error=error,
        )

    @staticmethod
    def _running_result(
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        return AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=uuid5(command.event_id, "captain.runtime-running"),
            command_id=command.event_id,
            correlation_id=command.correlation_id,
            occurred_at=command.occurred_at,
            producer="agent-runtime",
            subject_id=command.subject_id,
            subject_version=command.subject_version,
            grant_id=grant.grant_id,
            operation=command.payload.operation,
            status=RuntimeStatus.RUNNING,
        )


def create_runtime_adapters(context: RuntimeAdapterContext) -> RuntimeAdapterBinding:
    """Create exactly the immutable binding consumed by the verified loader."""

    artifacts = ContentAddressedArtifactAdapter(context.artifact_root)
    return RuntimeAdapterBinding(
        hermes=CaptainHermesPlannerAdapter.from_context(
            context,
            artifacts=artifacts,
        ),
        codex=CaptainCodexExecutionAdapter.from_context(
            context,
            artifacts=artifacts,
        ),
        artifacts=artifacts,
    )


def _require_grant(command: AgentRuntimeCommand, grant: CapabilityGrant) -> None:
    if (
        grant.command_id != command.event_id
        or grant.profile != command.payload.capability_profile
        or (
            command.payload.workspace_ref is not None
            and grant.workspace_ref != command.payload.workspace_ref
        )
    ):
        raise FactoryDispatchError("runtime grant does not match command")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _reject_private_evidence(
    value: object,
    *,
    prompt: str,
    environ: Mapping[str, str],
) -> None:
    encoded = _canonical_json(value).decode("utf-8")
    forbidden = [prompt] if prompt else []
    forbidden.extend(
        secret
        for name, secret in environ.items()
        if _SENSITIVE_ENVIRONMENT_NAME.search(name)
        and isinstance(secret, str)
        and len(secret) >= 8
    )
    if any(secret and secret in encoded for secret in forbidden):
        raise FactoryDispatchError("runtime evidence contains private content")


def _unique_references(values: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
    unique: dict[tuple[str, str, str], ArtifactRef] = {}
    for value in values:
        unique[(value.uri, value.sha256, value.media_type)] = value
    return tuple(unique.values())


__all__ = [
    "CaptainCodexExecutionAdapter",
    "CaptainHermesPlannerAdapter",
    "ContentAddressedArtifactAdapter",
    "create_runtime_adapters",
]
