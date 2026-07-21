"""Gateway-owned production wiring for the paid Factory runtime.

The external adapter module supplies provider-specific implementations.  This
module attests that module before import and binds its prepared effects and
materializer to one Gateway job, one action source, and one durable effect
ledger.  It never writes MariaDB outside ``GatewayFactoryRepository`` and the
existing Gateway ledger adapters.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import SecretStr

from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryEvidenceBlock, FactoryPhase
from agenten.agent_factory.factory_live_entrypoint import FactoryLiveDispatcherPort
from agenten.agent_factory.factory_live_prepared_dispatch import (
    FactoryLiveActionSourcePort,
    FactoryLivePreparedDispatch,
    FactoryLivePreparedDispatchPort,
    PreparedFactoryLiveEffectExecutor,
    PreparedFactoryLivePlan,
)
from agenten.agent_factory.factory_live_runner import (
    FactoryLiveEffectLedger,
    FactoryLiveEffectOutcomeV1,
    FactoryLiveEffectRequestV1,
    FactoryLiveRunner,
)
from agenten.agent_factory.service import FactoryCoordinator, FactoryRepository
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind, FactoryProjection


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PREPARED_ACTIONS = frozenset(
    {
        FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
        FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
    }
)


class FactoryLiveBootstrapError(ValueError):
    """A redacted, fail-closed production bootstrap failure."""


class FactoryLiveWorkflowRepository(FactoryRepository, Protocol):
    def workflow_artifacts(self, job_id: UUID) -> tuple[object, ...]: ...


class FactoryLiveMaterializerPort(Protocol):
    def validate_next(
        self,
        job: AgentFactoryJobV3,
        action: FactoryAction,
        expected_skill_digests: Mapping[str, str],
    ) -> FactoryAction: ...

    async def dispatch_next(self, job_id: UUID) -> FactoryAction: ...


@dataclass(frozen=True)
class FactoryLiveExternalRuntimeGraph:
    """Provider-owned ports accepted only after static adapter attestation."""

    prepared_dispatch: FactoryLivePreparedDispatchPort
    materializer: FactoryLiveMaterializerPort
    clock: Callable[[], datetime]

    def __post_init__(self) -> None:
        if any(
            not callable(getattr(self.prepared_dispatch, method, None))
            for method in ("prepare", "execute", "recover")
        ) or any(
            not callable(getattr(self.materializer, method, None))
            for method in ("validate_next", "dispatch_next")
        ) or not callable(self.clock):
            raise TypeError("production Factory runtime graph is incomplete")


@dataclass(frozen=True)
class GatewayFactoryLiveRuntimeContext:
    """Authority and authenticated runtime endpoint supplied to the adapter."""

    composition: object
    runtime_url: str
    runtime_token: SecretStr


@dataclass(frozen=True)
class _GatewayFactoryLiveAssembly:
    composition: object
    lifecycle: "GatewayFactoryLiveLifecycle"
    dispatcher: "MaterializingFactoryLiveDispatcher"
    runner: FactoryLiveRunner


class GatewayFactoryLiveRuntimeConstructors:
    """Memoized constructors that keep every port on one runtime graph."""

    def __init__(self, environment: "FactoryLiveEnvironment") -> None:
        self._environment = environment
        self._assembly: _GatewayFactoryLiveAssembly | None = None

    def lifecycle(self, composition: object) -> "GatewayFactoryLiveLifecycle":
        return self._assembly_for(composition).lifecycle

    def dispatcher(self, composition: object) -> "MaterializingFactoryLiveDispatcher":
        return self._assembly_for(composition).dispatcher

    def runner(self, composition: object) -> FactoryLiveRunner:
        return self._assembly_for(composition).runner

    def _assembly_for(self, composition: object) -> _GatewayFactoryLiveAssembly:
        if self._assembly is not None:
            if self._assembly.composition is not composition:
                raise ValueError("Factory runtime constructors cannot cross compositions")
            return self._assembly
        job = getattr(composition, "job", None)
        repository = getattr(composition, "repository", None)
        effect_ledger = getattr(composition, "live_effects", None)
        skill_digests = getattr(composition, "skill_digests", None)
        if (
            not isinstance(job, AgentFactoryJobV3)
            or job.job_id != self._environment.job_id
            or repository is None
            or effect_ledger is None
            or not isinstance(skill_digests, Mapping)
        ):
            raise ValueError("Factory runtime composition is outside Gateway authority")
        context = GatewayFactoryLiveRuntimeContext(
            composition=composition,
            runtime_url=self._environment.runtime_url,
            runtime_token=self._environment.runtime_token,
        )
        try:
            external = self._environment.graph_factory(context)
        except Exception:
            raise FactoryLiveBootstrapError(
                "production Factory runtime graph could not be constructed"
            ) from None
        if not isinstance(external, FactoryLiveExternalRuntimeGraph):
            raise FactoryLiveBootstrapError(
                "production adapter returned an invalid Factory runtime graph"
            )
        lifecycle = GatewayFactoryLiveLifecycle(repository)
        prepared = GatewayBoundFactoryLivePreparedDispatchPort(
            job=job,
            actions=lifecycle,
            repository=repository,
            delegate=external.prepared_dispatch,
        )
        plan = PreparedFactoryLivePlan(
            actions=lifecycle,
            dispatch=prepared,
            expected_skill_digests=skill_digests,
        )
        dispatcher = MaterializingFactoryLiveDispatcher(
            job=job,
            actions=lifecycle,
            repository=repository,
            plan=plan,
            delegate=external.materializer,
            expected_skill_digests=skill_digests,
        )
        runner = build_factory_live_runner(
            repository=repository,
            effect_ledger=effect_ledger,
            plan=plan,
            prepared_dispatch=prepared,
            clock=external.clock,
        )
        self._assembly = _GatewayFactoryLiveAssembly(
            composition=composition,
            lifecycle=lifecycle,
            dispatcher=dispatcher,
            runner=runner,
        )
        return self._assembly


class GatewayFactoryLiveLifecycle:
    """Expose the existing coordinator plus an authoritative promotion read."""

    def __init__(self, repository: FactoryLiveWorkflowRepository) -> None:
        self._repository = repository
        self._coordinator = FactoryCoordinator(repository)

    def next_action(self, job_id: UUID) -> FactoryAction:
        return self._coordinator.next_action(job_id)

    def projection(self, job_id: UUID) -> FactoryProjection:
        return self._coordinator.projection(job_id)

    def record(self, block: FactoryEvidenceBlock) -> bool:
        return self._coordinator.record(block)

    def promotion_block(self, job_id: UUID) -> FactoryEvidenceBlock | None:
        for block in reversed(self._repository.blocks(job_id)):
            if (
                block.phase is FactoryPhase.CAPABILITY_PROMOTED
                and block.producer == "captain"
            ):
                return block
        return None


class GatewayBoundFactoryLivePreparedDispatchPort:
    """Concrete prepared-effect port bound to one Gateway-owned runtime graph."""

    def __init__(
        self,
        *,
        job: AgentFactoryJobV3,
        actions: FactoryLiveActionSourcePort,
        repository: FactoryLiveWorkflowRepository,
        delegate: FactoryLivePreparedDispatchPort,
    ) -> None:
        if any(
            not callable(getattr(delegate, method, None))
            for method in ("prepare", "execute", "recover")
        ):
            raise TypeError("production Factory prepared dispatch port is incomplete")
        self._job = job
        self._actions = actions
        self._repository = repository
        self._delegate = delegate

    def prepare(
        self,
        *,
        job: AgentFactoryJobV3,
        action: FactoryAction,
        expected_skill_digests: Mapping[str, str],
        projection: FactoryProjection,
        workflow_artifacts: tuple[object, ...],
    ) -> FactoryLivePreparedDispatch:
        if (
            job != self._job
            or self._repository.job(job.job_id) != job
            or self._actions.next_action(job.job_id) != action
            or projection.job != job
            or projection.attempt != action.attempt
            or self._repository.workflow_artifacts(job.job_id) != workflow_artifacts
        ):
            raise ValueError("prepared Factory dispatch is outside Gateway authority")
        prepared = self._delegate.prepare(
            job=job,
            action=action,
            expected_skill_digests=expected_skill_digests,
            projection=projection,
            workflow_artifacts=workflow_artifacts,
        )
        if not isinstance(prepared, FactoryLivePreparedDispatch):
            raise TypeError("production Factory prepared dispatch is untyped")
        if prepared.action != action:
            raise ValueError("production Factory prepared dispatch changed the action")
        return prepared

    async def execute(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectOutcomeV1:
        self._require_request(request)
        return await self._delegate.execute(request)

    async def recover(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectOutcomeV1 | None:
        self._require_request(request)
        return await self._delegate.recover(request)

    def _require_request(self, request: FactoryLiveEffectRequestV1) -> None:
        if (
            request.job_id != self._job.job_id
            or request.correlation_id != self._job.correlation_id
            or request.subject_version != self._job.subject_version
        ):
            raise ValueError("production Factory effect is outside Gateway authority")


class MaterializingFactoryLiveDispatcher:
    """Stage the exact runner plan before allowing its workflow materializer."""

    def __init__(
        self,
        *,
        job: AgentFactoryJobV3,
        actions: FactoryLiveActionSourcePort,
        repository: FactoryLiveWorkflowRepository,
        plan: PreparedFactoryLivePlan,
        delegate: FactoryLiveMaterializerPort,
        expected_skill_digests: Mapping[str, str],
    ) -> None:
        if any(
            not callable(getattr(delegate, method, None))
            for method in ("validate_next", "dispatch_next")
        ):
            raise TypeError("production Factory materializer is incomplete")
        self._job = job
        self._actions = actions
        self._repository = repository
        self._plan = plan
        self._delegate = delegate
        self._expected_skill_digests = dict(expected_skill_digests)
        self._staged_action: FactoryAction | None = None

    def validate_next(
        self,
        job: AgentFactoryJobV3,
        action: FactoryAction,
        expected_skill_digests: Mapping[str, str],
    ) -> FactoryAction:
        if (
            job != self._job
            or dict(expected_skill_digests) != self._expected_skill_digests
            or self._actions.next_action(job.job_id) != action
        ):
            raise ValueError("Factory materializer validation is outside Gateway authority")
        validated = self._delegate.validate_next(
            job,
            action,
            expected_skill_digests,
        )
        if validated != action:
            raise ValueError("Factory materializer changed the Gateway action")
        if action.kind in _PREPARED_ACTIONS:
            projection = self._actions.projection(job.job_id)
            self._plan.effects_for(
                job=job,
                mode=job.execution_policy.mode.value,
                projection=projection,
                workflow_artifacts=self._repository.workflow_artifacts(job.job_id),
            )
        self._staged_action = action
        return action

    async def dispatch_next(self, job_id: UUID) -> FactoryAction:
        current = self._actions.next_action(job_id)
        if job_id != self._job.job_id or self._staged_action != current:
            raise ValueError("Factory materializer requires the staged Gateway action")
        dispatched = await self._delegate.dispatch_next(job_id)
        if dispatched != current:
            raise ValueError("Factory materializer executed a different Gateway action")
        self._staged_action = None
        return dispatched


def build_factory_live_runner(
    *,
    repository: FactoryLiveWorkflowRepository,
    effect_ledger: FactoryLiveEffectLedger,
    plan: PreparedFactoryLivePlan,
    prepared_dispatch: GatewayBoundFactoryLivePreparedDispatchPort,
    clock: Callable[[], Any],
) -> FactoryLiveRunner:
    """Build the durable runner from the same prepared graph as the dispatcher."""

    return FactoryLiveRunner(
        repository=repository,
        effect_ledger=effect_ledger,
        plan=plan,
        executor=PreparedFactoryLiveEffectExecutor(
            plan=plan,
            dispatch=prepared_dispatch,
        ),
        clock=clock,
    )


@dataclass(frozen=True)
class FactoryLiveEnvironment:
    job_id: UUID
    runtime_url: str
    runtime_token: SecretStr
    graph_factory: Callable[[object], object]
    manifest_sha256: str
    module_sha256: str


@dataclass(frozen=True)
class GatewayFactoryLiveGateBootstrap:
    """Prepared Gateway resources used by preflight and the one-shot gate."""

    environment: FactoryLiveEnvironment
    adapter_factory: object
    repository: object
    effect_history: object
    evidence_collector: object
    clock: Callable[[], datetime]


def bootstrap_factory_live_gate(
    settings: object,
    *,
    environ: Mapping[str, str] | None = None,
) -> GatewayFactoryLiveGateBootstrap:
    """Create the sole-writer Gateway graph from redacted environment aliases."""

    from blockchain.mariadb_storage import MariaDBStorage
    from agenten.agent_factory.live_observed_evidence import (
        GatewayFactoryLiveObservedEvidenceCollector,
    )
    from gateway.factory_live_composition import (
        GatewayFactoryLiveConstructors,
        GatewayPreparedFactoryLiveAdapterFactory,
    )
    from gateway.factory_repository import (
        GatewayFactoryLiveEffectLedger,
        GatewayFactoryRepository,
    )
    from gateway.store import GatewayStore

    source = os.environ if environ is None else environ
    root = getattr(settings, "repository_root", None)
    database_dsn = getattr(settings, "database_dsn", None)
    if not isinstance(root, Path) or not isinstance(database_dsn, SecretStr):
        raise FactoryLiveBootstrapError("Factory live settings are invalid")
    environment = load_factory_live_environment(source, workspace_root=root)
    try:
        store = GatewayStore(MariaDBStorage(database_dsn.get_secret_value()))
        repository = GatewayFactoryRepository(store)
        effect_history = GatewayFactoryLiveEffectLedger(store)
        runtime = GatewayFactoryLiveRuntimeConstructors(environment)
        constructors = GatewayFactoryLiveConstructors(
            repository=lambda supplied: GatewayFactoryRepository(supplied),
            lifecycle=runtime.lifecycle,
            dispatcher=runtime.dispatcher,
            runner=runtime.runner,
        )
        adapter_factory = GatewayPreparedFactoryLiveAdapterFactory(
            store=store,
            job_id=environment.job_id,
            constructors=constructors,
        )
        collector = GatewayFactoryLiveObservedEvidenceCollector(
            repository=repository,
            effect_history=effect_history,
        )
    except Exception:
        raise FactoryLiveBootstrapError(
            "Gateway-owned Factory runtime could not be initialized"
        ) from None
    return GatewayFactoryLiveGateBootstrap(
        environment=environment,
        adapter_factory=adapter_factory,
        repository=repository,
        effect_history=effect_history,
        evidence_collector=collector,
        clock=lambda: datetime.now(timezone.utc),
    )


def load_factory_live_environment(
    environ: Mapping[str, str],
    *,
    workspace_root: Path,
) -> FactoryLiveEnvironment:
    """Attest the Package-C manifest contract before importing provider code."""

    aliases = (
        "CAPTAIN_FACTORY_JOB_ID",
        "CAPTAIN_RUNTIME_URL",
        "CAPTAIN_RUNTIME_TOKEN",
        "FACTORY_LIVE_RUNTIME_ADAPTER_MANIFEST",
        "FACTORY_LIVE_RUNTIME_ADAPTER_SHA256",
    )
    values = {name: environ.get(name, "").strip() for name in aliases}
    if any(not value for value in values.values()):
        raise FactoryLiveBootstrapError("Factory live environment is incomplete")
    try:
        job_id = UUID(values["CAPTAIN_FACTORY_JOB_ID"])
        runtime_url = _runtime_url(values["CAPTAIN_RUNTIME_URL"])
        manifest_digest = values["FACTORY_LIVE_RUNTIME_ADAPTER_SHA256"]
        if _SHA256.fullmatch(manifest_digest) is None:
            raise ValueError("digest")
        root = workspace_root.resolve()
        manifest_path = _workspace_path(
            root,
            values["FACTORY_LIVE_RUNTIME_ADAPTER_MANIFEST"],
        )
        manifest_bytes = manifest_path.read_bytes()
        if len(manifest_bytes) > 65_536:
            raise ValueError("manifest size")
        if hashlib.sha256(manifest_bytes).hexdigest() != manifest_digest:
            raise ValueError("manifest digest")
        payload = json.loads(manifest_bytes)
        module_path, module_sha256, factory_symbol = _manifest_contract(root, payload)
        factory = _load_factory(module_path, module_sha256, factory_symbol)
    except FactoryLiveBootstrapError:
        raise
    except Exception:
        raise FactoryLiveBootstrapError(
            "Factory live runtime or adapter manifest is invalid"
        ) from None
    return FactoryLiveEnvironment(
        job_id=job_id,
        runtime_url=runtime_url,
        runtime_token=SecretStr(values["CAPTAIN_RUNTIME_TOKEN"]),
        graph_factory=factory,
        manifest_sha256=manifest_digest,
        module_sha256=module_sha256,
    )


def _runtime_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})
    ):
        raise ValueError("runtime URL")
    return value.rstrip("/")


def _workspace_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    path = (candidate if candidate.is_absolute() else root / candidate).resolve()
    path.relative_to(root)
    if not path.is_file():
        raise ValueError("workspace file")
    return path


def _manifest_contract(
    root: Path,
    payload: object,
) -> tuple[Path, str, str]:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "module_path", "module_sha256", "factory_symbol"}
        or payload.get("schema") != "captain.factory-live-runtime-adapter-manifest.v1"
        or not isinstance(payload.get("module_path"), str)
        or not isinstance(payload.get("module_sha256"), str)
        or _SHA256.fullmatch(payload["module_sha256"]) is None
        or not isinstance(payload.get("factory_symbol"), str)
        or _SYMBOL.fullmatch(payload["factory_symbol"]) is None
    ):
        raise ValueError("manifest contract")
    module_path = _workspace_path(root, payload["module_path"])
    if module_path.suffix.casefold() != ".py":
        raise ValueError("adapter module")
    module_bytes = module_path.read_bytes()
    if len(module_bytes) > 1_048_576:
        raise ValueError("adapter size")
    if hashlib.sha256(module_bytes).hexdigest() != payload["module_sha256"]:
        raise ValueError("adapter digest")
    tree = ast.parse(module_bytes.decode("utf-8"), filename=str(module_path))
    matches = tuple(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == payload["factory_symbol"]
    )
    if len(matches) != 1 or isinstance(matches[0], ast.AsyncFunctionDef):
        raise ValueError("adapter factory")
    return module_path, payload["module_sha256"], payload["factory_symbol"]


def _load_factory(
    module_path: Path,
    module_sha256: str,
    factory_symbol: str,
) -> Callable[[object], object]:
    current = module_path.read_bytes()
    if hashlib.sha256(current).hexdigest() != module_sha256:
        raise ValueError("adapter changed")
    module_name = "_captain_factory_live_adapter_" + module_sha256
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError("adapter loader")
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    factory = getattr(module, factory_symbol)
    if not callable(factory):
        raise TypeError("adapter factory")
    return factory
