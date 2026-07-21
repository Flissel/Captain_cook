"""Explicit production composition seams for the six-skill Factory runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from autogen_core.models import ChatCompletionClient

from agenten.agent_factory.candidate_evaluation import FactoryCandidateProvider
from agenten.agent_factory.codebase_discovery import DocumentationDiscoveryPort
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.evidence_store import FactoryEvidenceStore
from agenten.agent_factory.execution_budget import FactoryBudgetPort
from agenten.agent_factory.hermes_cli import (
    FactorySkillReplayStore,
    ReleasedFactorySkillCatalog,
)
from agenten.agent_factory.orchestration import HermesFactoryPort
from agenten.agent_factory.skill_workflow_contracts import FactorySkillInvocationV1
from agenten.agent_factory.team_execution import (
    FactoryHoldoutEvaluatorPort,
    FactoryLiveTeamExecutionPorts,
    FactoryN8nGrantAuthorityPort,
    FactoryN8nToolAdapterPort,
    FactoryPricingAuthorityPort,
    TeamExecutionCandidateAdapter,
    compose_live_team_execution as _compose_live_team_execution,
)
from agenten.agent_runtime.ports import CodexExecutionPort

from .live_codex import BoundCodexBuildAdapter
from .live_context7 import VerifiedContext7DocumentationAdapter
from .live_forge import SealedForgeCandidateProvider
from .live_holdouts import CaptainPrivateHoldoutSelector
from .live_minibook import (
    MinibookProjectionReadPort,
    ReadOnlyMinibookProjectionAdapter,
)


@dataclass(frozen=True)
class FactoryLiveRuntimePorts:
    """Every external or authoritative dependency required by live wiring."""

    hermes: HermesFactoryPort
    codex: CodexExecutionPort
    context7: DocumentationDiscoveryPort | None
    candidate_provider: FactoryCandidateProvider
    minibook: MinibookProjectionReadPort | None
    model_client_for: Callable[
        [AgentFactoryJobV3, FactorySkillInvocationV1], ChatCompletionClient
    ]
    budget: FactoryBudgetPort
    pricing_authority: FactoryPricingAuthorityPort
    replay_store: FactorySkillReplayStore
    holdouts: FactoryHoldoutEvaluatorPort
    n8n_adapter: FactoryN8nToolAdapterPort
    n8n_authority: FactoryN8nGrantAuthorityPort
    released_skill_catalog: ReleasedFactorySkillCatalog
    skill_root: Path
    tools: Mapping[str, Callable[..., Any]]
    provider: str
    model: str
    max_cost_per_call: Decimal
    clock: Callable[[], datetime]


@dataclass(frozen=True)
class FactoryLiveRuntimeComponents:
    """Bound components consumed by the separate Task-11 runtime coordinator."""

    hermes: HermesFactoryPort
    codex: BoundCodexBuildAdapter
    context7: VerifiedContext7DocumentationAdapter | None
    candidate_provider: SealedForgeCandidateProvider
    minibook: ReadOnlyMinibookProjectionAdapter | None
    team_execution: TeamExecutionCandidateAdapter


def compose_live_factory_runtime(
    *,
    job: AgentFactoryJobV3,
    evidence_store: FactoryEvidenceStore,
    ports: FactoryLiveRuntimePorts,
    holdout_id: str,
) -> FactoryLiveRuntimeComponents:
    """Bind six-skill ports without inventing any external adapter or authority."""

    required = (
        ports.hermes,
        ports.codex,
        ports.candidate_provider,
        ports.model_client_for,
        ports.budget,
        ports.pricing_authority,
        ports.replay_store,
        ports.holdouts,
        ports.n8n_adapter,
        ports.n8n_authority,
        ports.released_skill_catalog,
        ports.skill_root,
        ports.clock,
    )
    if any(port is None for port in required):
        raise ValueError("live Factory composition requires every authoritative port")
    holdout_selector = CaptainPrivateHoldoutSelector(
        job=job,
        holdout_id=holdout_id,
    )
    team_execution = _compose_live_team_execution(
        job=job,
        evidence_store=evidence_store,
        ports=FactoryLiveTeamExecutionPorts(
            model_client_for=ports.model_client_for,
            budget=ports.budget,
            pricing_authority=ports.pricing_authority,
            replay_store=ports.replay_store,
            holdouts=ports.holdouts,
            n8n_adapter=ports.n8n_adapter,
            n8n_authority=ports.n8n_authority,
            released_skill_catalog=ports.released_skill_catalog,
            skill_root=ports.skill_root,
            tools=ports.tools,
            provider=ports.provider,
            model=ports.model,
            max_cost_per_call=ports.max_cost_per_call,
            clock=ports.clock,
        ),
        holdout_selector=holdout_selector,
    )
    return FactoryLiveRuntimeComponents(
        hermes=ports.hermes,
        codex=BoundCodexBuildAdapter(runtime=ports.codex, clock=ports.clock),
        context7=(
            VerifiedContext7DocumentationAdapter(ports.context7)
            if ports.context7 is not None
            else None
        ),
        candidate_provider=SealedForgeCandidateProvider(ports.candidate_provider),
        minibook=(
            ReadOnlyMinibookProjectionAdapter(ports.minibook)
            if ports.minibook is not None
            else None
        ),
        team_execution=team_execution,
    )
