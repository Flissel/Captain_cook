"""Attested production composition for Package C and the paid Factory graph.

The module intentionally contains the two stable top-level symbols referenced by
the separated adapter manifests.  Local storage, Gateway authority, Minibook
projection and the digest-pinned sandbox are concrete.  Provider bridges which
cannot yet satisfy the repository's claim/evidence contracts fail with a
machine-readable ``TODO_TOOL`` marker before emitting evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from agenten.agent_factory.capability_factory_entrypoint import (
    CapabilityExecutionPlan,
    CapabilityFactoryEntrypoint,
    CapabilityFactoryRunSummary,
    CapabilityRuntimeExecution,
    DockerCapabilitySandboxRunner,
    FileCapabilityFactoryCheckpointStore,
)
from agenten.agent_factory.capability_factory_production import AdapterManifestKind
from agenten.agent_factory.capability_factory_production import (
    MinibookSwarmCreationHttpPort,
    RuntimeCaptainEvidenceHttpPort,
)
from agenten.agent_factory.capability_live_adapters import (
    ContentAddressedArtifactStore,
)
from agenten.agent_factory.factory_live_prepared_dispatch import (
    FactoryLivePreparedDispatch,
)
from agenten.agent_factory.factory_live_runner import (
    FactoryLiveEffectOutcomeV1,
    FactoryLiveEffectRequestV1,
)
from agenten.agent_factory.forge_contracts import ArtifactRef as ForgeArtifactRef
from agenten.agent_factory.forge_contracts import ReleasedSkillRefV1
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.input_document import load_factory_input
from agenten.agent_factory.outcome_contracts import (
    ExecutionOutcomeV1,
)
from agenten.agent_factory.state_machine import FactoryAction
import httpx
from fastapi import HTTPException, status
from pydantic import SecretStr

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    HermesPlanResult,
)
from agenten.agent_runtime.runtime_entrypoint import (
    RuntimeEntrypointSettings,
    compose_gateway_backed_runtime_executor,
)
from agenten.delivery.minibook_client import MinibookClient
from agenten.delivery.projector import MinibookProjector
from agenten.delivery.projection_cursor import ProjectionCursorStore
from gateway.capability_catalog import GatewayCapabilityCatalog
from gateway.factory_live_runtime import FactoryLiveExternalRuntimeGraph
from gateway.factory_repository import GatewayFactoryRepository
from gateway.registry_feed import factory_promotion_projection, runtime_result_projection
from gateway.production_store import LazyGatewayStore
from gateway.store import GatewayStore


class ProductionToolRequired(RuntimeError):
    """A required external effect has no truthful production implementation."""


def _todo(tool_id: str) -> ProductionToolRequired:
    return ProductionToolRequired(f"TODO_TOOL:{tool_id}")


class ContentAddressedRuntimeArtifactPort:
    """Read-only runtime artifact port over the Factory content store layout."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    async def require(self, reference: ArtifactRef | Any) -> None:
        self.read_bytes(reference)

    def read_bytes(self, reference: ArtifactRef | Any) -> bytes:
        digest = str(reference.sha256)
        if (
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or str(reference.uri).rsplit("/", 1)[-1] != digest
        ):
            raise ValueError("runtime artifact reference is not content-addressed")
        target = self._root / "content" / "sha256" / digest[:2] / digest
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise ValueError("runtime artifact content is unavailable") from exc
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("runtime artifact digest changed")
        return content


def _resolved_executable(environ: Mapping[str, str], name: str, default: str) -> str:
    raw = environ.get(name, default).strip()
    candidate = Path(raw)
    resolved = (
        str(candidate.resolve())
        if candidate.is_absolute() and candidate.is_file()
        else shutil.which(raw)
    )
    if not resolved:
        raise ProductionToolRequired(f"TODO_TOOL:runtime_executable:{name}")
    return resolved


async def _bounded_process(arguments: tuple[str, ...], *, timeout: int) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("runtime CLI exceeded its Captain wall-clock limit") from None
    if process.returncode != 0:
        raise RuntimeError("runtime CLI failed")
    if len(stdout) > 1_048_576:
        raise RuntimeError("runtime CLI returned too much evidence")
    return stdout


def _first_json_object(content: bytes) -> object:
    text = content.decode("utf-8")
    start = text.find("{")
    if start < 0:
        raise ValueError("runtime CLI returned no JSON object")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    return value


class StrictHermesCliPlanner:
    """Lazy Hermes CLI adapter accepting only the typed runtime plan contract."""

    def __init__(self, executable: str, artifacts: ContentAddressedRuntimeArtifactPort) -> None:
        self._executable = executable
        self._artifacts = artifacts

    async def plan(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> HermesPlanResult:
        return await self._invoke(command, grant)

    async def design_agent(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> HermesPlanResult:
        return await self._invoke(command, grant)

    async def _invoke(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> HermesPlanResult:
        prompt = self._artifacts.read_bytes(command.payload.prompt_ref).decode("utf-8")
        envelope = {
            "instruction": "Return exactly one captain.hermes-plan-result.v1 JSON object.",
            "command": command.model_dump(mode="json", by_alias=True),
            "grant": grant.model_dump(mode="json", by_alias=True),
            "prompt": prompt,
        }
        stdout = await _bounded_process(
            (
                self._executable,
                "-z",
                json.dumps(envelope, sort_keys=True, separators=(",", ":")),
            ),
            timeout=command.payload.limits.wall_seconds,
        )
        result = HermesPlanResult.model_validate(_first_json_object(stdout))
        if (
            result.correlation_id != command.correlation_id
            or result.subject_version != command.subject_version
            or result.project_id != command.payload.project_id
        ):
            raise ValueError("Hermes runtime result changed Captain authority")
        return result


class StrictCodexCliExecution:
    """Lazy Codex CLI adapter; session-control operations remain explicit gaps."""

    def __init__(self, executable: str, artifacts: ContentAddressedRuntimeArtifactPort) -> None:
        self._executable = executable
        self._artifacts = artifacts

    async def start(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return await self._invoke(command, grant)

    async def resume(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return await self._invoke(command, grant)

    async def status(self, command: AgentRuntimeCommand, grant: CapabilityGrant) -> AgentRuntimeResult:
        del command, grant
        raise _todo("codex_session_status_bridge")

    async def cancel(self, command: AgentRuntimeCommand, grant: CapabilityGrant) -> AgentRuntimeResult:
        del command, grant
        raise _todo("codex_session_cancel_bridge")

    async def heartbeat(self, command: AgentRuntimeCommand, grant: CapabilityGrant) -> AgentRuntimeResult:
        del command, grant
        raise _todo("codex_session_heartbeat_bridge")

    async def _invoke(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        prompt = self._artifacts.read_bytes(command.payload.prompt_ref).decode("utf-8")
        envelope = {
            "instruction": "Complete the authorized work and end with exactly one captain.agent-runtime-result.v1 JSON object.",
            "command": command.model_dump(mode="json", by_alias=True),
            "grant": grant.model_dump(mode="json", by_alias=True),
            "prompt": prompt,
        }
        stdout = await _bounded_process(
            (
                self._executable,
                "exec",
                "--json",
                json.dumps(envelope, sort_keys=True, separators=(",", ":")),
            ),
            timeout=command.payload.limits.wall_seconds,
        )
        candidates: list[object] = []
        for line in stdout.decode("utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("schema") == "captain.agent-runtime-result.v1":
                candidates.append(event)
            item = event.get("item") if isinstance(event, dict) else None
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str) and "captain.agent-runtime-result.v1" in text:
                    candidates.append(_first_json_object(text.encode("utf-8")))
        if len(candidates) != 1:
            raise ValueError("Codex runtime returned no unique typed result")
        result = AgentRuntimeResult.model_validate(candidates[0])
        if (
            result.command_id != command.event_id
            or result.correlation_id != command.correlation_id
            or result.grant_id != grant.grant_id
        ):
            raise ValueError("Codex runtime result changed Captain authority")
        return result


class _PreflightUnavailableCapabilityEvidenceBackend:
    """Fail-closed fallback used only when no production bridge was injected."""

    async def run(self, request: Any) -> None:
        del request
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TODO_TOOL:capability_release_executor_bridge",
        )

    async def lifecycle_blocks(self, request: Any) -> Any:
        del request
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TODO_TOOL:capability_release_executor_bridge",
        )


def build_runtime_app_from_environment(
    settings: RuntimeEntrypointSettings,
    environ: Mapping[str, str],
    *,
    evidence_backend: Any | None = None,
) -> Any:
    """Build 8091 with lazy CLI/Gateway ports and an injectable evidence bridge.

    ``capability_v3_evidence_bridge.build_capability_evidence_backend`` supplies
    the production backend once its authority-bound dependencies have been
    assembled.  Preflight may omit it; evidence routes then fail with an exact
    ``TODO_TOOL`` marker and can never manufacture provider evidence.
    """

    from agenten.agent_factory.capability_factory_production import (
        create_capability_factory_runtime_app,
    )

    artifact_root_value = environ.get("CAPTAIN_RUNTIME_ARTIFACT_ROOT", "").strip()
    if not artifact_root_value:
        raise _todo("configuration:CAPTAIN_RUNTIME_ARTIFACT_ROOT")
    artifacts = ContentAddressedRuntimeArtifactPort(Path(artifact_root_value))
    hermes = StrictHermesCliPlanner(
        _resolved_executable(environ, "HERMES_EXECUTABLE", "hermes"),
        artifacts,
    )
    codex = StrictCodexCliExecution(
        _resolved_executable(environ, "CODEX_EXECUTABLE", "codex"),
        artifacts,
    )
    client = httpx.AsyncClient(timeout=30.0)
    executor = compose_gateway_backed_runtime_executor(
        settings=settings,
        client=client,
        hermes=hermes,
        codex=codex,
        artifacts=artifacts,
    )
    selected_backend = (
        evidence_backend
        if evidence_backend is not None
        else _PreflightUnavailableCapabilityEvidenceBackend()
    )
    app = create_capability_factory_runtime_app(
        runtime_executor=executor,
        backend=selected_backend,
        token=SecretStr(settings.runtime_token.get_secret_value()),
    )
    app.state.gateway_http_client = client
    app.state.capability_evidence_backend = selected_backend
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def close_gateway_client(application: Any):
        try:
            async with original_lifespan(application):
                yield
        finally:
            await client.aclose()

    app.router.lifespan_context = close_gateway_client
    return app


def build_runtime_app_with_v3_evidence(
    settings: RuntimeEntrypointSettings,
    environ: Mapping[str, str],
    *,
    context: Any,
) -> Any:
    """Compose the authenticated runtime with the explicit V2-to-V3 bridge."""

    from agenten.agent_factory.capability_v3_evidence_bridge import (
        build_capability_evidence_backend,
    )

    backend = build_capability_evidence_backend(context=context)
    return build_runtime_app_from_environment(
        settings,
        environ,
        evidence_backend=backend,
    )


class _TodoCapabilityRuntime:
    async def prepare(self, job: Any, authority: Any) -> CapabilityExecutionPlan:
        del job, authority
        raise _todo("claim_aware_capability_runtime")

    async def execute(
        self,
        plan: CapabilityExecutionPlan,
        authority: Any,
        claim: Any,
        *,
        effect_id: UUID,
    ) -> CapabilityRuntimeExecution:
        del plan, authority, claim, effect_id
        raise _todo("claim_aware_capability_runtime")

    def guarantees_durable_idempotency(self, plan: Any, authority: Any) -> bool:
        del plan, authority
        return False

    async def lookup_effect(self, *, command_id: UUID, effect_id: UUID) -> None:
        del command_id, effect_id
        return None

    async def derive_outcome(
        self,
        plan: CapabilityExecutionPlan,
        authority: Any,
        result: Any,
    ) -> ExecutionOutcomeV1:
        del plan, authority, result
        raise _todo("claim_aware_capability_runtime")


class GatewayCapabilityFactoryPort:
    """Package-C adapter over the real GatewayStore authority.

    The legacy result write cannot satisfy Package C's execution-claim fence.
    That single operation therefore fails closed rather than silently dropping
    the credential/fencing inputs.
    """

    def __init__(self, store: GatewayStore) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def record_runtime_result(self, result: Any, **claim: Any) -> object:
        del result, claim
        raise _todo("gateway_claim_aware_runtime_result_endpoint")

    def projection_events(self, correlation_id: UUID) -> tuple[Any, ...]:
        cursor = -1
        projected: list[Any] = []
        while True:
            records, has_more = self._store.minibook_projection_feed(
                after_index=cursor,
                limit=100,
            )
            for index, block_type, data, parent in records:
                cursor = index
                event = (
                    factory_promotion_projection(data, parent)
                    if block_type == "agent_factory_block" and parent is not None
                    else runtime_result_projection(data)
                )
                if event is not None and event.correlation_id == correlation_id:
                    projected.append(event)
            if not has_more:
                return tuple(projected)


class _UtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ProductionCapabilityFactoryEntrypoint(CapabilityFactoryEntrypoint):
    """Package-C entrypoint that materializes its canonical input in shared CAS."""

    def __init__(
        self,
        *,
        production_artifacts: ContentAddressedArtifactStore,
        **dependencies: Any,
    ) -> None:
        super().__init__(**dependencies)
        self._production_artifacts = production_artifacts

    def seed_input(self, input_path: Path) -> ArtifactRef:
        """Persist exact canonical bytes before any creation boundary is called."""

        document = load_factory_input(input_path)
        source = input_path.read_bytes()
        stored = self._production_artifacts.put(
            source,
            "text/markdown",
            namespace="factory-input",
        )
        if (
            stored.sha256 != document.input_ref.sha256
            or stored.uri.rsplit("/", 1)[-1] != document.input_ref.sha256
        ):
            raise ValueError("canonical Factory input CAS binding changed")
        self._production_artifacts.read_bytes(stored)
        return document.input_ref

    async def run(
        self,
        *,
        input_path: Path,
        correlation_id: UUID,
        subject_version: int,
        wall_clock_budget_seconds: int,
    ) -> CapabilityFactoryRunSummary:
        self.seed_input(input_path)
        return await super().run(
            input_path=input_path,
            correlation_id=correlation_id,
            subject_version=subject_version,
            wall_clock_budget_seconds=wall_clock_budget_seconds,
        )


class _TodoPreparedDispatch:
    def prepare(self, **_: Any) -> FactoryLivePreparedDispatch:
        raise _todo("factory_live_prepared_dispatch_bridge")

    async def execute(
        self, request: FactoryLiveEffectRequestV1
    ) -> FactoryLiveEffectOutcomeV1:
        del request
        raise _todo("factory_live_prepared_dispatch_bridge")

    async def recover(
        self, request: FactoryLiveEffectRequestV1
    ) -> FactoryLiveEffectOutcomeV1 | None:
        del request
        raise _todo("factory_live_prepared_dispatch_bridge")


class _TodoMaterializer:
    def validate_next(
        self,
        job: Any,
        action: FactoryAction,
        expected_skill_digests: Mapping[str, str],
    ) -> FactoryAction:
        del job, expected_skill_digests
        return action

    async def dispatch_next(self, job_id: UUID) -> FactoryAction:
        del job_id
        raise _todo("factory_live_materializer_bridge")


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ProductionToolRequired(f"TODO_TOOL:configuration:{name}")
    return value


def _released_skill(root: Path) -> ReleasedSkillRefV1:
    skill_root = root / "agenten" / "agent_factory" / "skills" / "captain-agent-factory-loop"
    if not skill_root.is_dir():
        raise _todo("released_capability_factory_skill")
    entries: list[tuple[str, str]] = []
    for path in sorted(item for item in skill_root.rglob("*") if item.is_file()):
        entries.append(
            (
                path.relative_to(skill_root).as_posix(),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    digest = hashlib.sha256(repr(entries).encode("utf-8")).hexdigest()
    return ReleasedSkillRefV1(
        skill_id="captain-agent-factory-loop",
        version=1,
        content_ref=ForgeArtifactRef(
            uri=f"artifact://released-skill/{digest}",
            sha256=digest,
            media_type="application/octet-stream",
        ),
        content_sha256=digest,
    )


def build_capability_factory_entrypoint(config: Any) -> CapabilityFactoryEntrypoint:
    """Build the local production graph without starting an external effect."""

    root = Path(config.workspace_root).resolve()
    dsn = _required_environment("TEST_MARIADB_DSN")
    if not dsn.rstrip("/").endswith("/captain_test"):
        raise ProductionToolRequired("TODO_TOOL:configuration:TEST_MARIADB_DSN")
    artifact_root = Path(config.artifact_dir).resolve()
    shared_root_value = _required_environment("CAPTAIN_RUNTIME_ARTIFACT_ROOT")
    shared_root_candidate = Path(shared_root_value)
    shared_root = (
        shared_root_candidate
        if shared_root_candidate.is_absolute()
        else root / shared_root_candidate
    ).resolve()
    if shared_root != artifact_root:
        raise _todo("configuration:shared_capability_artifact_root")
    content = ContentAddressedArtifactStore(artifact_root)
    store = LazyGatewayStore(dsn)
    gateway = GatewayCapabilityFactoryPort(store)
    minibook_api_key = _required_environment("MINIBOOK_API_KEY")
    capability_http = httpx.AsyncClient(timeout=30.0)
    minibook = MinibookClient(
        config.minibook_url,
        minibook_api_key,
        projection_api_key=config.minibook_projection_api_key.get_secret_value(),
    )
    return ProductionCapabilityFactoryEntrypoint(
        production_artifacts=content,
        checkpoint_store=FileCapabilityFactoryCheckpointStore(config.checkpoint_dir),
        holdout_store=InMemoryPrivateHoldoutStore(),
        repository=GatewayFactoryRepository(store),
        catalog=GatewayCapabilityCatalog(store),
        released_skill=_released_skill(root),
        creation=MinibookSwarmCreationHttpPort(
            config.minibook_url,
            SecretStr(minibook_api_key),
            capability_http,
        ),
        content_store=content,
        sandbox_runner=DockerCapabilitySandboxRunner(image=config.sandbox_image),
        evidence_issuer=RuntimeCaptainEvidenceHttpPort(
            config.runtime_url,
            SecretStr(config.runtime_token.get_secret_value()),
            capability_http,
        ),
        gateway=gateway,
        runtime=_TodoCapabilityRuntime(),
        projector=MinibookProjector(
            minibook,
            ProjectionCursorStore(artifact_root / "projection-cursor.sqlite3"),
            owner_id="capability-factory-production",
        ),
        clock=_UtcClock(),
    )


def build_factory_live_runtime(context: Any) -> FactoryLiveExternalRuntimeGraph:
    """Delegate to the real attestable paid Factory effect graph."""

    from agenten.agent_factory.factory_live_paid_ports import (
        build_factory_live_runtime as build_paid_runtime,
    )

    return build_paid_runtime(context)


def production_manifest_commands(workspace_root: Path) -> dict[AdapterManifestKind, str]:
    root = workspace_root.resolve()
    script = root / "scripts" / "generate-capability-adapter-manifest.py"
    module = root / "agenten" / "agent_factory" / "production_adapter_bundle.py"
    paid_module = root / "agenten" / "agent_factory" / "factory_live_paid_ports.py"
    output = root / ".captain-cook" / "adapters"
    prefix = f'python "{script}" --workspace-root "{root}" --module "{module}"'
    return {
        AdapterManifestKind.ENTRYPOINT: (
            f'{prefix} --factory-symbol build_capability_factory_entrypoint '
            f'--target "{output / "capability-entrypoint.manifest.json"}" --kind entrypoint'
        ),
        AdapterManifestKind.FACTORY_LIVE_RUNTIME: (
            f'python "{script}" --workspace-root "{root}" --module "{paid_module}" '
            f'--factory-symbol build_factory_live_runtime '
            f'--target "{output / "factory-live-runtime.manifest.json"}" '
            "--kind factory_live_runtime"
        ),
    }
