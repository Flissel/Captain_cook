"""Paid Factory ports: released Hermes skills -> durable CAS -> materialization."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from agenten.agent_factory.capability_live_adapters import ContentAddressedArtifactStore
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryRole
from agenten.agent_factory.execution_budget import BudgetExhausted, FactoryBudgetPort
from agenten.agent_factory.factory_live_prepared_dispatch import FactoryLivePreparedDispatch
from agenten.agent_factory.factory_live_runner import (
    FactoryLiveEffectKind,
    FactoryLiveEffectOutcomeV1,
    FactoryLiveEffectRequestV1,
)
from agenten.agent_factory.hermes_cli import (
    FACTORY_SKILL_ID_BY_STEP,
    HermesCliFactory,
    HermesCliSettings,
    ReleasedFactorySkillCatalog,
    _factory_invocation,
    _factory_skill_prompt,
    _factory_usage_receipt,
    _parse_paid_usage,
    _parse_workflow_artifact,
    _remaining_reservable_usd,
    _require_released_skill_directory,
)
from agenten.agent_factory.minibook_forge import MinibookForgeSettings, MinibookSwarmForge
from agenten.agent_factory.orchestration import (
    FactoryClock,
    FactoryDispatch,
    FactoryDispatchError,
    FactoryDispatcher,
    FactoryImprovementAuthorizationPort,
)
from agenten.agent_factory.service import FactoryCoordinator, FactoryWorkflowArtifactSink
from agenten.agent_factory.skill_sequence import SkillSequencePolicy
from agenten.agent_factory.skill_sequence import FactoryImprovementAuthorizationV1
from agenten.agent_factory.skill_workflow_contracts import (
    CandidateRevisionV1,
    CodebaseInventoryV1,
    CodexBuildBriefV1,
    FactoryFeedbackV1,
    FactorySkillStep,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind, FactoryProjection
from agenten.agent_factory.team_execution import _run_scoped_invocation
from agenten.agent_runtime.contracts import ArtifactRef

FactoryWorkflowArtifact = (
    CodebaseInventoryV1 | CodexBuildBriefV1 | CandidateRevisionV1
    | TeamExecutionEvidenceV1 | TeamEvaluationV1 | FactoryFeedbackV1
)
_PREPARED_ROLES = {
    FactoryActionKind.DISPATCH_TOOL_INTEGRATOR: FactoryRole.TOOL_INTEGRATOR,
    FactoryActionKind.DISPATCH_REAL_CASE_TESTER: FactoryRole.REAL_CASE_TESTER,
}


class PreparedHermesEffectPort(Protocol):
    async def execute(
        self, *, job: AgentFactoryJobV3, request: FactoryLiveEffectRequestV1
    ) -> FactoryWorkflowArtifact: ...


class _UtcFactoryClock(FactoryClock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class _GatewayImprovementAuthorizations(FactoryImprovementAuthorizationPort):
    """Rebuild retry authority only from the Captain block and typed prior evidence."""

    def __init__(self, repository: object) -> None:
        self._repository = repository

    def active(
        self,
        job: AgentFactoryJobV3,
        action: FactoryAction,
        projection: FactoryProjection,
        now: datetime,
    ) -> FactoryImprovementAuthorizationV1:
        if action.attempt <= 1 or projection.job != job or now >= job.deadline_at:
            raise FactoryDispatchError("retry improvement authority is not active")
        blocks = self._repository.blocks(job.job_id)
        request = next(
            (
                block
                for block in reversed(blocks)
                if block.phase.value == "improvement_requested"
                and block.producer == "captain"
                and block.attempt + 1 == action.attempt
            ),
            None,
        )
        if request is None:
            raise FactoryDispatchError("retry lacks Captain IMPROVEMENT_REQUESTED evidence")
        artifacts = self._repository.workflow_artifacts(job.job_id)
        evaluation = next(
            (
                artifact
                for artifact in reversed(artifacts)
                if isinstance(artifact, TeamEvaluationV1)
                and artifact.attempt == request.attempt
                and artifact.artifact_ref in request.artifact_refs
            ),
            None,
        )
        candidates = {
            artifact.candidate_ref
            for artifact in artifacts
            if isinstance(artifact, TeamExecutionEvidenceV1)
            and artifact.attempt == request.attempt
            and artifact.candidate_ref in request.artifact_refs
        }
        if evaluation is None or len(candidates) != 1 or not request.evidence_refs:
            raise FactoryDispatchError("retry authority lacks evaluated candidate evidence")
        return FactoryImprovementAuthorizationV1(
            schema_name="captain.factory-improvement-authorization.v1",
            authorization_ref=request.evidence_refs[0],
            authorized_attempt=action.attempt,
            request_block=request,
            failed_evaluation=evaluation,
            prior_candidate_ref=next(iter(candidates)),
            prior_green_assertion_ids=evaluation.prior_green_regression_ids,
        )


@dataclass(frozen=True)
class _CasInputMaterializer:
    store: ContentAddressedArtifactStore

    def materialize(self, reference: ArtifactRef) -> Path:
        content = self.store.read_bytes(reference)
        target = self.store.root / "materialized" / reference.sha256 / "TO_BE_BUILT.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != content:
            raise FactoryDispatchError("materialized Factory input digest conflicts")
        if not target.exists():
            target.write_bytes(content)
        return target


class HermesCliPreparedEffectPort:
    """Execute exactly one invocation and book its machine-readable paid usage."""

    def __init__(
        self,
        *,
        settings: HermesCliSettings,
        budget: FactoryBudgetPort,
        artifacts: ContentAddressedArtifactStore,
        clock: Callable[[], datetime],
        prompt_runner: Callable[[str, float, Path], object] | None = None,
    ) -> None:
        self._settings = settings
        self._budget = budget
        self._artifacts = artifacts
        self._clock = clock
        self._cli = HermesCliFactory(settings=settings, clock=clock)
        self._prompt_runner = prompt_runner

    async def execute(
        self, *, job: AgentFactoryJobV3, request: FactoryLiveEffectRequestV1
    ) -> FactoryWorkflowArtifact:
        invocation = request.invocation
        skill_name = FACTORY_SKILL_ID_BY_STEP[invocation.step]
        started_at = self._clock()
        if shutil.which(self._settings.executable) is None:
            raise FactoryDispatchError("Hermes CLI executable is unavailable")
        _require_released_skill_directory(
            self._settings.skill_root,
            skill_name=skill_name,
            released_skill=invocation.released_skill,
            now=started_at,
        )
        reservation = self._budget.reserve(
            job,
            attempt=request.attempt,
            requested_usd=_remaining_reservable_usd(self._budget, job),
            now=started_at,
            invocation_id=invocation.invocation_id,
        )
        live_binding = json.dumps(
            {
                "effect_id": str(request.effect_id),
                "run_id": str(request.run_id) if request.run_id else None,
                "run_number": request.run_effect_index,
                "run_count": request.run_effect_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        prompt = "\n".join(
            (_factory_skill_prompt(invocation, skill_name=skill_name), f"captain_live_effect_json={live_binding}")
        )
        try:
            with tempfile.TemporaryDirectory(prefix="captain-prepared-hermes-") as temporary:
                usage_path = Path(temporary) / "usage.json"
                stdout = await self._run_prompt(
                    prompt,
                    max_seconds=min(
                        float(self._settings.timeout_seconds),
                        (invocation.lease.expires_at - started_at).total_seconds(),
                    ),
                    usage_path=usage_path,
                )
                ended_at = self._clock()
                usage = _parse_paid_usage(usage_path.read_bytes())
        except BudgetExhausted:
            raise
        except Exception as exc:
            raise FactoryDispatchError("provider_cost_unresolved") from exc
        if (
            usage.model not in job.execution_policy.allowed_models
            or ended_at < started_at
            or ended_at >= invocation.lease.expires_at
            or ended_at > reservation.expires_at
        ):
            raise FactoryDispatchError("prepared Hermes usage is outside Captain policy")
        artifact = _parse_workflow_artifact(stdout, step=invocation.step)
        if artifact.invocation != invocation:
            raise FactoryDispatchError("prepared Hermes artifact changed its invocation")
        usage_ref = self._artifacts.put(
            json.dumps(
                usage.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8"),
            "application/json",
            namespace="factory-live-usage",
        )
        action_kind = (
            FactoryActionKind.DISPATCH_REAL_CASE_TESTER
            if invocation.step is FactorySkillStep.EXECUTE_TEAM
            else FactoryActionKind.DISPATCH_TOOL_INTEGRATOR
        )
        dispatch = FactoryDispatch(
            job=job,
            action=FactoryAction(kind=action_kind, attempt=request.attempt, job_id=job.job_id),
            role=invocation.lease.role,
            lease=invocation.lease,
        )
        receipt = _factory_usage_receipt(
            dispatch,
            invocation=invocation,
            reservation=reservation,
            usage=usage,
            started_at=started_at,
            ended_at=ended_at,
            evidence_ref=usage_ref,
        )
        self._budget.record_usage(job, reservation, receipt)
        return artifact

    async def _run_prompt(self, prompt: str, *, max_seconds: float, usage_path: Path) -> bytes:
        if max_seconds <= 0:
            raise FactoryDispatchError("prepared Hermes invocation lease expired")
        if self._prompt_runner is None:
            return await self._cli._run_skill_prompt(
                prompt, max_seconds=max_seconds, usage_file=usage_path
            )
        value = self._prompt_runner(prompt, max_seconds, usage_path)
        if asyncio.iscoroutine(value):
            value = await value
        if not isinstance(value, bytes):
            raise TypeError("prepared Hermes prompt runner must return bytes")
        return value


class DurableFactoryLivePreparedDispatch:
    """Prepare deterministic requests and stage exact Hermes results in CAS."""

    def __init__(
        self,
        *,
        job: AgentFactoryJobV3,
        released_skills: ReleasedFactorySkillCatalog,
        leases: object,
        effects: PreparedHermesEffectPort,
        artifacts: ContentAddressedArtifactStore,
        clock: Callable[[], datetime],
        improvements: FactoryImprovementAuthorizationPort | None = None,
    ) -> None:
        self._job = job
        self._released_skills = released_skills
        self._leases = leases
        self._effects = effects
        self._artifacts = artifacts
        self._clock = clock
        self._improvements = improvements
        self._policy = SkillSequencePolicy()
        self._prepared: dict[UUID, FactoryLiveEffectRequestV1] = {}
        self._action_by_effect: dict[UUID, FactoryAction] = {}

    def prepare(
        self,
        *,
        job: AgentFactoryJobV3,
        action: FactoryAction,
        expected_skill_digests: Mapping[str, str],
        projection: FactoryProjection,
        workflow_artifacts: tuple[object, ...],
    ) -> FactoryLivePreparedDispatch:
        del workflow_artifacts
        role = _PREPARED_ROLES.get(action.kind)
        if job != self._job or projection.job != job or role is None:
            raise ValueError("prepared live dispatch is outside its Factory job")
        lease = self._leases.active(job, role, action.attempt, self._clock())
        improvement = None
        if role is FactoryRole.TOOL_INTEGRATOR and action.attempt > 1:
            if self._improvements is None:
                raise FactoryDispatchError("retry requires improvement authorization")
            improvement = self._improvements.active(job, action, projection, self._clock())
        dispatch = FactoryDispatch(
            job=job,
            action=action,
            role=role,
            lease=lease,
            improvement_authorization=improvement,
        )
        input_ref = improvement.authorization_ref if improvement is not None else job.input_ref
        steps = self._policy.steps_for(role=role, attempt=action.attempt)
        if role is FactoryRole.REAL_CASE_TESTER:
            steps = (FactorySkillStep.EXECUTE_TEAM,) * job.execution_policy.required_live_runs
        requests: list[FactoryLiveEffectRequestV1] = []
        for sequence, step in enumerate(steps, start=1):
            released = self._released_skills.released_for(job, step)
            skill_name = FACTORY_SKILL_ID_BY_STEP[step]
            if expected_skill_digests.get(skill_name) != released.content_sha256:
                raise ValueError("prepared Factory skill digest is not Captain-released")
            invocation = _factory_invocation(
                dispatch,
                step=step,
                released_skill=released,
                input_ref=input_ref,
            )
            if step is FactorySkillStep.EXECUTE_TEAM:
                if len(job.private_holdout_refs) != 1:
                    raise ValueError("prepared live execution requires one explicit holdout")
                invocation = _run_scoped_invocation(
                    invocation,
                    job.private_holdout_refs[0],
                    sequence,
                    required_live_runs=job.execution_policy.required_live_runs,
                )
            identity = "|".join(
                (str(job.job_id), str(action.attempt), step.value, invocation.idempotency_key)
            )
            requests.append(
                FactoryLiveEffectRequestV1(
                    schema_name="captain.factory-live-effect-request.v1",
                    effect_id=uuid5(NAMESPACE_URL, f"captain.factory-live-effect:{identity}"),
                    job_id=job.job_id,
                    correlation_id=job.correlation_id,
                    subject_version=job.subject_version,
                    attempt=action.attempt,
                    kind=(
                        FactoryLiveEffectKind.PROVIDER
                        if step is FactorySkillStep.EXECUTE_TEAM
                        else FactoryLiveEffectKind.CODEX
                    ),
                    idempotency_key=invocation.idempotency_key,
                    input_ref=invocation.input_ref,
                    invocation=invocation,
                )
            )
            input_ref = invocation.input_ref
        if job.execution_policy.mode.value == "release":
            run_id = uuid5(
                NAMESPACE_URL,
                "|".join(
                    (
                        "captain.factory-live-run.v1",
                        str(job.job_id),
                        str(job.subject_version),
                        str(action.attempt),
                        *(str(request.effect_id) for request in requests),
                    )
                ),
            )
            count = len(requests)
            requests = [
                request.model_copy(
                    update={
                        "run_id": run_id,
                        "run_effect_index": index,
                        "run_effect_count": count,
                    }
                )
                for index, request in enumerate(requests, start=1)
            ]
        for request in requests:
            existing = self._prepared.get(request.effect_id)
            if existing is not None and existing != request:
                raise ValueError("prepared Factory effect identity conflicts")
            self._prepared[request.effect_id] = request
            self._action_by_effect[request.effect_id] = action
        return FactoryLivePreparedDispatch(action=action, requests=tuple(requests))

    async def execute(self, request: FactoryLiveEffectRequestV1) -> FactoryLiveEffectOutcomeV1:
        self._require_prepared(request)
        if self._artifacts.binding("factory-live-outcome", str(request.effect_id)) is not None:
            raise ValueError("prepared Factory effect already has durable evidence")
        try:
            artifact = await self._effects.execute(job=self._job, request=request)
        except BudgetExhausted:
            return self._persist_outcome(
                request,
                self._blocked(request, "budget_exhausted", "factory USD budget is exhausted"),
            )
        except FactoryDispatchError as exc:
            detail = str(exc).lower()
            if "executable" in detail and "unavailable" in detail:
                return self._persist_outcome(
                    request,
                    self._blocked(request, "required_tool", "Hermes CLI is unavailable"),
                )
            if "credential" in detail or "authentication" in detail:
                return self._persist_outcome(
                    request,
                    self._blocked(
                        request,
                        "credential_required",
                        "Hermes provider authentication is unavailable",
                    ),
                )
            raise
        evidence_ref = self._artifacts.put(
            artifact.model_dump_json(by_alias=True).encode("utf-8"),
            "application/json",
            namespace="factory-live-effect",
        )
        self._artifacts.bind("factory-live-effect", str(request.effect_id), evidence_ref)
        status = "succeeded"
        if isinstance(artifact, TeamExecutionEvidenceV1) and artifact.status != "succeeded":
            status = "behavioral_failure"
        outcome = FactoryLiveEffectOutcomeV1(
            schema_name="captain.factory-live-effect-outcome.v1",
            outcome_id=uuid5(NAMESPACE_URL, f"captain.factory-live-outcome:{request.effect_id}"),
            effect_id=request.effect_id,
            job_id=request.job_id,
            correlation_id=request.correlation_id,
            subject_version=request.subject_version,
            attempt=request.attempt,
            status=status,
            evidence_ref=evidence_ref,
            completed_at=self._clock(),
        )
        return self._persist_outcome(request, outcome)

    async def recover(self, request: FactoryLiveEffectRequestV1) -> FactoryLiveEffectOutcomeV1 | None:
        self._require_prepared(request)
        reference = self._artifacts.binding("factory-live-outcome", str(request.effect_id))
        if reference is None:
            return None
        outcome = FactoryLiveEffectOutcomeV1.model_validate_json(self._artifacts.read_bytes(reference))
        if outcome.effect_id != request.effect_id or outcome.job_id != request.job_id:
            raise ValueError("durable Factory outcome changed its authority binding")
        return outcome.model_copy(update={"completion_origin": "recover"})

    def artifact_for(self, request: FactoryLiveEffectRequestV1) -> FactoryWorkflowArtifact:
        self._require_prepared(request)
        reference = self.evidence_ref_for(request)
        return _parse_workflow_artifact(
            self._artifacts.read_bytes(reference), step=request.invocation.step
        )

    def evidence_ref_for(self, request: FactoryLiveEffectRequestV1) -> ArtifactRef:
        self._require_prepared(request)
        reference = self._artifacts.binding("factory-live-effect", str(request.effect_id))
        if reference is None:
            raise ValueError("Factory effect has no durable artifact")
        return reference

    def requests_for(self, action: FactoryAction) -> tuple[FactoryLiveEffectRequestV1, ...]:
        return tuple(
            request
            for effect_id, request in self._prepared.items()
            if self._action_by_effect.get(effect_id) == action
        )

    def _persist_outcome(
        self, request: FactoryLiveEffectRequestV1, outcome: FactoryLiveEffectOutcomeV1
    ) -> FactoryLiveEffectOutcomeV1:
        if outcome.effect_id != request.effect_id:
            raise ValueError("Factory outcome changed its effect binding")
        reference = self._artifacts.put(
            outcome.model_dump_json(by_alias=True).encode("utf-8"),
            "application/json",
            namespace="factory-live-outcome",
        )
        self._artifacts.bind("factory-live-outcome", str(request.effect_id), reference)
        return outcome

    def _require_prepared(self, request: FactoryLiveEffectRequestV1) -> None:
        if self._prepared.get(request.effect_id) != request:
            raise ValueError("Factory effect is not the exact prepared request")

    def _blocked(
        self, request: FactoryLiveEffectRequestV1, status: str, reason: str
    ) -> FactoryLiveEffectOutcomeV1:
        return FactoryLiveEffectOutcomeV1(
            schema_name="captain.factory-live-effect-outcome.v1",
            outcome_id=uuid5(NAMESPACE_URL, f"captain.factory-live-outcome:{request.effect_id}"),
            effect_id=request.effect_id,
            job_id=request.job_id,
            correlation_id=request.correlation_id,
            subject_version=request.subject_version,
            attempt=request.attempt,
            status=status,
            reason=reason,
            completed_at=self._clock(),
        )


class PreparedFactoryLiveMaterializer:
    """Append staged artifacts; never restart their paid external effects."""

    def __init__(
        self,
        *,
        job: AgentFactoryJobV3,
        coordinator: FactoryCoordinator,
        prepared: DurableFactoryLivePreparedDispatch,
        workflow_sink: FactoryWorkflowArtifactSink,
        delegate: FactoryDispatcher,
        expected_skill_digests: Mapping[str, str],
    ) -> None:
        self._job = job
        self._coordinator = coordinator
        self._prepared = prepared
        self._workflow_sink = workflow_sink
        self._delegate = delegate
        self._expected_skill_digests = dict(expected_skill_digests)
        self._validated: FactoryAction | None = None

    def validate_next(
        self,
        job: AgentFactoryJobV3,
        action: FactoryAction,
        expected_skill_digests: Mapping[str, str],
    ) -> FactoryAction:
        if job != self._job or dict(expected_skill_digests) != self._expected_skill_digests:
            raise ValueError("Factory materializer authority mismatch")
        if self._coordinator.next_action(job.job_id) != action:
            raise ValueError("Factory materializer action is stale")
        self._validated = action
        return action

    async def dispatch_next(self, job_id: UUID) -> FactoryAction:
        action = self._coordinator.next_action(job_id)
        if job_id != self._job.job_id or action != self._validated:
            raise ValueError("Factory materializer requires its validated action")
        if action.kind not in _PREPARED_ROLES:
            dispatched = await self._delegate.dispatch_next(job_id)
            self._validated = None
            return dispatched
        requests = self._prepared.requests_for(action)
        if not requests:
            raise ValueError("Factory materializer lacks the prepared effect batch")
        artifacts = tuple(self._prepared.artifact_for(request) for request in requests)
        if any(artifact.attempt != action.attempt for artifact in artifacts):
            raise ValueError("Factory materializer artifact attempt mismatch")
        for artifact in artifacts:
            await self._workflow_sink.persist(artifact)
        from agenten.agent_factory.hermes_cli import _factory_block_for

        block = _factory_block_for(
            FactoryDispatch(
                job=self._job,
                action=action,
                role=_PREPARED_ROLES[action.kind],
                lease=requests[0].invocation.lease,
            ),
            artifacts=artifacts,
            transcript_refs=tuple(
                self._prepared.evidence_ref_for(request) for request in requests
            ),
        )
        self._coordinator.record(block)
        self._validated = None
        return action


def build_factory_live_runtime(context: object):
    """Build the attestable local Hermes/CAS runtime with no manual DI step."""

    from gateway.factory_live_runtime import FactoryLiveExternalRuntimeGraph

    composition = getattr(context, "composition", None)
    job = getattr(composition, "job", None)
    repository = getattr(composition, "repository", None)
    leases = getattr(composition, "leases", None)
    budget = getattr(composition, "budget", None)
    workflow_sink = getattr(composition, "workflow_sink", None)
    skill_digests = getattr(composition, "skill_digests", None)
    if (
        not isinstance(job, AgentFactoryJobV3)
        or repository is None
        or leases is None
        or budget is None
        or workflow_sink is None
        or not isinstance(skill_digests, Mapping)
    ):
        raise ValueError("Factory paid runtime composition is incomplete")
    factory_root = os.environ.get("CAPTAIN_FACTORY_ARTIFACT_ROOT", "").strip()
    runtime_root = os.environ.get("CAPTAIN_RUNTIME_ARTIFACT_ROOT", "").strip()
    if factory_root and runtime_root:
        if Path(factory_root).resolve() != Path(runtime_root).resolve():
            raise ValueError("Factory paid runtime and Package C artifact roots differ")
    root = Path(factory_root or runtime_root or "artifacts/capability-factory")
    if not root.is_absolute():
        root = Path.cwd() / root
    artifacts = ContentAddressedArtifactStore(root)
    clock = lambda: datetime.now(timezone.utc)
    settings = HermesCliSettings(
        executable=os.environ.get("HERMES_EXECUTABLE", "hermes"),
        skill_root=Path(
            os.environ.get(
                "CAPTAIN_FACTORY_SKILL_ROOT", "agenten/agent_factory/skills"
            )
        ),
        timeout_seconds=int(os.environ.get("CAPTAIN_FACTORY_HERMES_TIMEOUT", "900")),
        evidence_root=root / "hermes-evidence",
    )
    effects = HermesCliPreparedEffectPort(
        settings=settings,
        budget=budget,
        artifacts=artifacts,
        clock=clock,
    )
    improvements = _GatewayImprovementAuthorizations(repository)
    prepared = DurableFactoryLivePreparedDispatch(
        job=job,
        released_skills=repository,
        leases=leases,
        effects=effects,
        artifacts=artifacts,
        clock=clock,
        improvements=improvements,
    )
    coordinator = FactoryCoordinator(repository)
    hermes = HermesCliFactory(
        settings=settings,
        released_skill_catalog=repository,
        budget=budget,
        workflow_artifact_sink=workflow_sink,
        clock=clock,
    )
    forge = MinibookSwarmForge(
        materializer=_CasInputMaterializer(artifacts),
        settings=MinibookForgeSettings(working_directory=Path.cwd()),
    )
    delegate = FactoryDispatcher(
        coordinator=coordinator,
        hermes=hermes,
        forge=forge,
        leases=leases,
        clock=_UtcFactoryClock(),
        improvements=improvements,
    )
    materializer = PreparedFactoryLiveMaterializer(
        job=job,
        coordinator=coordinator,
        prepared=prepared,
        workflow_sink=workflow_sink,
        delegate=delegate,
        expected_skill_digests=skill_digests,
    )
    return FactoryLiveExternalRuntimeGraph(
        prepared_dispatch=prepared,
        materializer=materializer,
        clock=clock,
    )
