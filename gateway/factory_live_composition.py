"""Gateway-owned composition root for the paid Factory live gate."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.factory_live_entrypoint import (
    FactoryLiveConfigurationError,
    FactoryLiveDispatcherPort,
    FactoryLiveLifecyclePort,
    FactoryLivePreflightSettings,
    FactoryLiveRunnerPort,
    PreparedFactoryLiveAdapter,
)
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FACTORY_SKILL_ID_BY_STEP,
    FactorySkillStep,
    released_skill_capability_matches_job,
)
from gateway.factory_repository import (
    GatewayFactoryBudgetLedger,
    GatewayFactoryLeases,
    GatewayFactoryLiveEffectLedger,
    GatewayFactoryWorkflowArtifactSink,
)
from gateway.store import GatewayStore


class GatewayFactoryLiveRepositoryPort(Protocol):
    """Gateway reads required before any live adapter is considered prepared."""

    def job(self, job_id: UUID) -> AgentFactoryJobV3: ...

    def released_for(
        self,
        job: AgentFactoryJobV3,
        step: FactorySkillStep,
    ) -> ReleasedHermesSkill: ...

    def workflow_artifacts(self, job_id: UUID) -> tuple[object, ...]: ...


@dataclass(frozen=True)
class GatewayFactoryLiveComposition:
    """One authority-bound graph supplied to every runtime constructor."""

    settings: FactoryLivePreflightSettings
    job: AgentFactoryJobV3
    skill_digests: Mapping[str, str]
    repository: GatewayFactoryLiveRepositoryPort
    leases: GatewayFactoryLeases
    budget: GatewayFactoryBudgetLedger
    live_effects: GatewayFactoryLiveEffectLedger
    workflow_sink: GatewayFactoryWorkflowArtifactSink


@dataclass(frozen=True)
class GatewayFactoryLiveConstructors:
    """Explicit constructors for runtime parts owned by adjacent workstreams."""

    repository: Callable[[GatewayStore], GatewayFactoryLiveRepositoryPort]
    lifecycle: Callable[[GatewayFactoryLiveComposition], FactoryLiveLifecyclePort]
    dispatcher: Callable[[GatewayFactoryLiveComposition], FactoryLiveDispatcherPort]
    runner: Callable[[GatewayFactoryLiveComposition], FactoryLiveRunnerPort]


class GatewayPreparedFactoryLiveAdapterFactory:
    """Prepare live ports around one Gateway-owned job without running effects."""

    def __init__(
        self,
        *,
        store: GatewayStore,
        job_id: UUID,
        constructors: GatewayFactoryLiveConstructors,
    ) -> None:
        self._store = store
        self._job_id = job_id
        self._constructors = constructors

    def prepare(
        self,
        settings: FactoryLivePreflightSettings,
        expected_skill_digests: Mapping[str, str],
    ) -> PreparedFactoryLiveAdapter:
        repository = self._constructors.repository(self._store)
        job = repository.job(self._job_id)
        self._require_exact_job_and_policy(job, settings)
        self._require_exact_released_skills(
            repository,
            job,
            expected_skill_digests,
        )
        composition = GatewayFactoryLiveComposition(
            settings=settings,
            job=job,
            skill_digests=dict(expected_skill_digests),
            repository=repository,
            leases=GatewayFactoryLeases(self._store),
            budget=GatewayFactoryBudgetLedger(self._store),
            live_effects=GatewayFactoryLiveEffectLedger(self._store),
            workflow_sink=GatewayFactoryWorkflowArtifactSink(self._store),
        )
        return PreparedFactoryLiveAdapter(
            mode=settings.mode,
            max_cost_usd=settings.max_cost_usd,
            model=settings.model,
            with_n8n=settings.with_n8n,
            skill_digests=expected_skill_digests,
            lifecycle=self._constructors.lifecycle(composition),
            repository=repository,
            dispatcher=self._constructors.dispatcher(composition),
            live_runner=self._constructors.runner(composition),
        )

    def _require_exact_job_and_policy(
        self,
        job: AgentFactoryJobV3,
        settings: FactoryLivePreflightSettings,
    ) -> None:
        if not isinstance(job, AgentFactoryJobV3) or job.job_id != self._job_id:
            raise FactoryLiveConfigurationError(
                "Gateway Factory job does not match the configured job ID"
            )
        policy = job.execution_policy
        if not policy.live_execution:
            raise FactoryLiveConfigurationError(
                "Gateway Factory job does not authorize live execution"
            )
        if policy.mode.value != settings.mode:
            raise FactoryLiveConfigurationError(
                "Gateway Factory execution mode does not match preflight settings"
            )
        if policy.max_cost_usd != settings.max_cost_usd:
            raise FactoryLiveConfigurationError(
                "Gateway Factory budget does not match preflight settings"
            )
        if settings.model not in policy.allowed_models:
            raise FactoryLiveConfigurationError(
                "Gateway Factory model is not allowed by the execution policy"
            )

    @staticmethod
    def _require_exact_released_skills(
        repository: GatewayFactoryLiveRepositoryPort,
        job: AgentFactoryJobV3,
        expected_skill_digests: Mapping[str, str],
    ) -> None:
        expected_ids = set(FACTORY_SKILL_ID_BY_STEP.values())
        if set(expected_skill_digests) != expected_ids:
            raise FactoryLiveConfigurationError(
                "Gateway Factory skill digest set is not exact"
            )
        for step in FactorySkillStep:
            expected_id = FACTORY_SKILL_ID_BY_STEP[step]
            released = repository.released_for(job, step)
            if (
                released.skill_id != expected_id
                or not released_skill_capability_matches_job(
                    released.capability, job.required_capability
                )
                or released.content_sha256 != expected_skill_digests[expected_id]
            ):
                raise FactoryLiveConfigurationError(
                    "Gateway released skill digest does not match the live gate"
                )
