"""Deterministic ports for the production six-skill Factory coordinator.

This module deliberately owns no lifecycle policy.  Captain transitions are
derived and written only by ``FactoryCoordinator`` and
``FactorySixSkillLiveCoordinator``.  The fakes below stop at process/provider
boundaries and expose their calls for integration assertions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.execution_budget import (
    FactoryBudgetProjection,
    InMemoryFactoryBudgetLedger,
)
from agenten.agent_factory.factory_feedback import FactoryFeedbackBuilder
from agenten.agent_factory.factory_live_runner import (
    FactoryInfrastructureFailure,
    FactoryLiveEffectKind,
    FactoryLiveEffectOutcomeV1,
    FactoryLiveEffectRequestV1,
    FactoryLiveRunner,
    InMemoryFactoryLiveEffectLedger,
)
from agenten.agent_factory.hermes_cli import (
    HermesCliFactory,
    HermesCliSettings,
    InMemoryFactorySkillReplayStore,
    _factory_invocation,
)
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.live_composition import (
    FactoryLiveRuntimePorts,
    compose_live_factory_runtime,
)
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatcher
from agenten.agent_factory.service import FactoryCoordinator, InMemoryFactoryRepository
from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_factory.skill_workflow_contracts import (
    CodebaseInventoryV1,
    CodexBuildBriefV1,
    FactoryFeedbackRecommendation,
    FactoryFeedbackV1,
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.skill_sequence import SkillSequencePolicy
from agenten.agent_factory.state_machine import FactoryActionKind, FactoryProjection
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent
from tests.agent_factory.test_factory_live_runner import effect_outcome
from tests.agent_factory.test_hermes_cli import (
    _catalog_for,
    _improvement_authorization,
    _invocation_from_prompt,
)
from tests.agent_factory.test_release_gate import (
    workflow_evaluation,
    workflow_job,
    workflow_receipts,
    workflow_run,
)
from tests.agent_factory.test_skill_workflow_contracts import (
    brief_payload,
    feedback_payload,
    inventory_payload,
    invocation_payload,
    revision_payload,
    tool_gap_payload,
)


NOW = datetime(2026, 7, 21, 10, 5, tzinfo=timezone.utc)
FIRST_PASS_STEPS = (
    "discover",
    "brief_codex",
    "execute_team",
    "evaluate_team",
    "report_captain",
)
RETRY_STEPS = (
    *FIRST_PASS_STEPS,
    "discover",
    "improve_team",
    "brief_codex",
    "execute_team",
    "evaluate_team",
    "report_captain",
)


class WorkflowGatewayRepository(InMemoryFactoryRepository):
    """In-memory Gateway port with the production repository read surface."""

    def __init__(self, job: AgentFactoryJobV3) -> None:
        super().__init__()
        self.register(job)
        self.artifacts: list[object] = []
        self.budget = FactoryBudgetProjection(
            job_id=job.job_id,
            limit_usd=job.execution_policy.max_cost_usd,
            consumed_usd=Decimal("0"),
            reserved_usd=Decimal("0"),
            remaining_usd=job.execution_policy.max_cost_usd,
        )
        self.receipts: list[object] = []

    def workflow_artifacts(self, job_id: UUID) -> tuple[object, ...]:
        self.job(job_id)
        return tuple(self.artifacts)

    def workflow_budget_projection(self, job_id: UUID) -> FactoryBudgetProjection:
        self.job(job_id)
        return self.budget

    def workflow_usage_receipts(self, job_id: UUID) -> tuple[object, ...]:
        self.job(job_id)
        return tuple(self.receipts)

    def replace_budget(self, consumed: Decimal) -> None:
        remaining = max(Decimal("0"), self.budget.limit_usd - consumed)
        self.budget = self.budget.model_copy(
            update={
                "consumed_usd": consumed,
                "reserved_usd": Decimal("0"),
                "remaining_usd": remaining,
            }
        )


class DeterministicClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def __call__(self) -> datetime:
        return self.value


class DeterministicLeases:
    def __init__(self, clock: DeterministicClock) -> None:
        self._clock = clock

    def active(
        self,
        job: AgentFactoryJobV3,
        role: FactoryRole,
        attempt: int,
        now: datetime,
    ):
        assert now == self._clock()
        return issue_factory_lease(
            job=job,
            role=role,
            attempt=attempt,
            workspace_ref="workspace://factory/deterministic-integration",
            now=now,
        )


class DeterministicImprovements:
    def active(self, job, action, projection, now):
        del job, action, projection, now
        return _improvement_authorization()


class NullForge:
    async def submit(self, request: FactoryDispatch) -> object:
        return SimpleNamespace(job_id=request.job.job_id)


class DeterministicHermes(HermesCliFactory):
    """Real Hermes adapter with only its subprocess boundary replaced."""

    def __init__(self, harness: "SixSkillFactoryHarness", **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._harness = harness

    async def _run_skill_prompt(self, prompt: str, *, max_seconds: float) -> bytes:
        assert max_seconds > 0
        self._harness.process_calls += 1
        invocation = FactorySkillInvocationV1.model_validate(
            _invocation_from_prompt(prompt)
        )
        artifact = self._harness.artifact_for(invocation)
        self._harness.repository.artifacts.append(artifact)
        return artifact.model_dump_json(by_alias=True).encode("utf-8")


class DeterministicDispatcher:
    """External-boundary driver; every block is still validated by Captain."""

    def __init__(
        self,
        harness: "SixSkillFactoryHarness",
        dispatcher: FactoryDispatcher,
    ) -> None:
        self._harness = harness
        self._dispatcher = dispatcher

    def validate_next(
        self,
        job: AgentFactoryJobV3,
        action,
        expected_skill_digests,
    ):
        assert expected_skill_digests
        role = {
            FactoryActionKind.DISPATCH_AGENT_ARCHITECT: FactoryRole.AGENT_ARCHITECT,
            FactoryActionKind.DISPATCH_TOOL_INTEGRATOR: FactoryRole.TOOL_INTEGRATOR,
            FactoryActionKind.DISPATCH_REAL_CASE_TESTER: FactoryRole.REAL_CASE_TESTER,
            FactoryActionKind.DISPATCH_QUALITY_WARDEN: FactoryRole.QUALITY_WARDEN,
        }.get(action.kind)
        if role is not None:
            lease = self._harness.leases.active(
                job,
                role,
                action.attempt,
                self._harness.clock(),
            )
            improvement = None
            if role is FactoryRole.TOOL_INTEGRATOR and action.attempt > 1:
                improvement = self._harness.improvements.active(
                    job,
                    action,
                    self._harness.coordinator.projection(job.job_id),
                    self._harness.clock(),
                )
            self._harness.hermes.validate_dispatch_configuration(
                FactoryDispatch(
                    job=job,
                    action=action,
                    role=role,
                    lease=lease,
                    improvement_authorization=improvement,
                )
            )
        return action

    async def dispatch_next(self, job_id: UUID):
        action = self._harness.coordinator.next_action(job_id)
        if action.kind is FactoryActionKind.SUBMIT_FORGE_JOB:
            await self._harness.forge.submit(
                FactoryDispatch(
                    job=self._harness.job,
                    action=action,
                    role=None,
                    lease=None,
                )
            )
            self._harness.coordinator.record(
                self._harness.external_block(
                    FactoryPhase.AGENT_CODE_CREATED,
                    attempt=action.attempt,
                    producer="hermes",
                )
            )
            return action
        if action.kind is FactoryActionKind.DISPATCH_BUILD_VALIDATOR:
            self._harness.coordinator.record(
                self._harness.external_block(
                    FactoryPhase.BUILD_PASSED,
                    attempt=action.attempt,
                    producer="hermes",
                )
            )
            return action
        if action.kind is FactoryActionKind.DISPATCH_REAL_CASE_TESTER:
            executions = self._harness.materialize_execution_batch(action.attempt)
            self._harness.repository.artifacts.extend(executions)
            self._harness.coordinator.record(
                self._harness.external_block(
                    FactoryPhase.REAL_CASE_EVIDENCE,
                    attempt=action.attempt,
                    producer="hermes",
                    role=FactoryRole.REAL_CASE_TESTER,
                    artifact_refs=tuple(item.artifact_ref for item in executions),
                    evidence_refs=tuple(
                        reference
                        for item in executions
                        for reference in item.evidence_refs
                    ),
                    assertion_ids=self._harness.job.acceptance_assertion_ids,
                )
            )
            return action
        return await self._dispatcher.dispatch_next(job_id)


class RecordingLiveEffectLedger(InMemoryFactoryLiveEffectLedger):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def claim(self, request: FactoryLiveEffectRequestV1):
        self._events.append(f"claim:{request.kind.value}")
        return super().claim(request)


class LiveEffectPlan:
    def __init__(self, harness: "SixSkillFactoryHarness") -> None:
        self._harness = harness
        self._policy = SkillSequencePolicy()

    def effects_for(
        self,
        *,
        job: AgentFactoryJobV3,
        mode: str,
        projection: FactoryProjection,
        workflow_artifacts: tuple[object, ...],
    ) -> tuple[FactoryLiveEffectRequestV1, ...]:
        del mode, workflow_artifacts
        action = self._harness.coordinator.next_action(job.job_id)
        if action.kind is FactoryActionKind.DISPATCH_TOOL_INTEGRATOR:
            steps = self._policy.steps_for(
                role=FactoryRole.TOOL_INTEGRATOR,
                attempt=action.attempt,
            )
            return tuple(
                self._request(job, action, step, index)
                for index, step in enumerate(steps, start=1)
            )
        if action.kind is FactoryActionKind.DISPATCH_REAL_CASE_TESTER:
            return tuple(
                self._request(
                    job,
                    action,
                    FactorySkillStep.EXECUTE_TEAM,
                    run_number,
                )
                for run_number in range(
                    1, job.execution_policy.required_live_runs + 1
                )
            )
        if (
            action.kind is FactoryActionKind.COMPLETE
            and (
                self._harness._last_result is not None
                or self._harness.restart_after_evidence
            )
        ):
            replay_action = SimpleNamespace(
                kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
                attempt=projection.attempt,
            )
            return tuple(
                self._request(
                    job,
                    replay_action,
                    FactorySkillStep.EXECUTE_TEAM,
                    run_number,
                )
                for run_number in range(
                    1, job.execution_policy.required_live_runs + 1
                )
            )
        return ()

    def _request(
        self,
        job: AgentFactoryJobV3,
        action,
        step: FactorySkillStep,
        sequence: int,
    ) -> FactoryLiveEffectRequestV1:
        role = (
            FactoryRole.REAL_CASE_TESTER
            if step is FactorySkillStep.EXECUTE_TEAM
            else FactoryRole.TOOL_INTEGRATOR
        )
        dispatch = FactoryDispatch(
            job=job,
            action=action,
            role=role,
            lease=self._harness.leases.active(
                job,
                role,
                action.attempt,
                NOW,
            ),
            improvement_authorization=(
                self._harness.improvements.active(
                    job,
                    action,
                    projection=self._harness.coordinator.projection(job.job_id),
                    now=NOW,
                )
                if action.attempt > 1 and role is FactoryRole.TOOL_INTEGRATOR
                else None
            ),
        )
        released = self._harness.catalog.released_for(job, step)
        invocation = _factory_invocation(
            dispatch,
            step=step,
            released_skill=released,
            input_ref=job.input_ref,
        )
        identity = hashlib.sha256(
            f"{job.job_id}:{action.attempt}:{step.value}:{sequence}".encode()
        ).hexdigest()
        invocation = invocation.model_copy(
            update={
                "invocation_id": uuid5(
                    NAMESPACE_URL,
                    f"deterministic-live-invocation:{identity}",
                ),
                "idempotency_key": identity,
                "execution_scope_ref": (
                    job.private_holdout_refs[0]
                    if step is FactorySkillStep.EXECUTE_TEAM
                    else None
                ),
            }
        )
        kind = (
            FactoryLiveEffectKind.PROVIDER
            if step is FactorySkillStep.EXECUTE_TEAM
            else FactoryLiveEffectKind.CODEX
        )
        return FactoryLiveEffectRequestV1(
            schema_name="captain.factory-live-effect-request.v1",
            effect_id=uuid5(
                NAMESPACE_URL,
                f"deterministic-live-effect:{identity}",
            ),
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=action.attempt,
            kind=kind,
            idempotency_key=identity,
            input_ref=invocation.input_ref,
            invocation=invocation,
        )


class RecoveringLiveExecutor:
    def __init__(self, harness: "SixSkillFactoryHarness") -> None:
        self._harness = harness
        self.execute_calls = 0
        self.recover_calls = 0

    async def execute(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectOutcomeV1:
        self.execute_calls += 1
        self._harness.mark_pre_promotion()
        if (
            request.kind is FactoryLiveEffectKind.PROVIDER
            and self._harness.paid_cost_usd + Decimal("0.25")
            > self._harness.job.execution_policy.max_cost_usd
        ):
            from agenten.agent_factory.execution_budget import BudgetExhausted

            raise BudgetExhausted("factory USD budget is exhausted")
        self._harness.effect_order.append(f"start:{request.kind.value}")
        self._harness.effect_counts[request.kind.value] += 1
        self._harness.stage_request(request)
        if request.kind is FactoryLiveEffectKind.PROVIDER:
            self._harness.paid_cost_usd += Decimal("0.25")
        if request.kind is FactoryLiveEffectKind.PROVIDER and self._harness.with_n8n:
            self._harness.effect_counts["n8n"] += 1
        if (
            request.kind is FactoryLiveEffectKind.PROVIDER
            and self._harness.job.execution_policy.mode.value == "release"
            and not self._harness._controlled_recovery_injected
        ):
            self._harness._controlled_recovery_injected = True
            raise FactoryInfrastructureFailure("controlled deterministic recovery")
        outcome = effect_outcome(request)
        self._harness.stage_outcome(request.kind, outcome)
        return outcome

    async def recover(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectOutcomeV1:
        self.recover_calls += 1
        self._harness.effect_order.append(f"recover:{request.kind.value}")
        self._harness.stage_request(request)
        outcome = effect_outcome(request)
        self._harness.stage_outcome(request.kind, outcome)
        return outcome


@dataclass(frozen=True)
class SixSkillHarnessResult:
    coordinator_result: object
    skill_steps: tuple[str, ...]
    attempts: int
    gateway_projection: FactoryProjection
    gateway_phases: tuple[str, ...]
    workflow_artifacts: tuple[object, ...]
    usage_receipts: tuple[object, ...]
    total_cost_usd: Decimal
    feedback: FactoryFeedbackV1
    tool_gap_severities: tuple[str, ...]
    effect_counts: dict[str, int]
    effect_order: tuple[str, ...]
    infrastructure_recoveries: int
    recovered_reservations: int
    replayed_evidence: int
    used_composed_ports: bool
    live_status: str
    captain_promoted: bool
    pre_promotion_status: str

    @property
    def worker_ready_claim_changed_projection(self) -> bool:
        return False


class SixSkillFactoryHarness:
    """Compose deterministic boundaries around the production coordinator."""

    def __init__(
        self,
        *,
        mode: Literal["demo", "release"] = "release",
        first_run: Literal["passed", "behavioral_failure"] = "passed",
        tool_gap: Literal["required", "optional"] | None = None,
        failure: Literal["credential_required", "infrastructure_failure"] | None = None,
        budget_usd: Decimal = Decimal("5.00"),
        change_skill_digest: bool = False,
        restart_after_reservation: bool = False,
        restart_after_evidence: bool = False,
        with_n8n: bool = False,
    ) -> None:
        base_job = workflow_job(mode=mode)
        policy = base_job.execution_policy.model_copy(
            update={"max_cost_usd": budget_usd}
        )
        self.job = base_job.model_copy(update={"execution_policy": policy})
        self.first_run = first_run
        self.tool_gap = tool_gap
        self.failure = failure
        self.change_skill_digest = change_skill_digest
        self.restart_after_reservation = restart_after_reservation
        self.restart_after_evidence = restart_after_evidence
        self.with_n8n = with_n8n
        self.clock = DeterministicClock()
        self.repository = WorkflowGatewayRepository(self.job)
        self.coordinator = FactoryCoordinator(self.repository)
        self.forge = NullForge()
        self.process_calls = 0
        self.effect_counts = {"codex": 0, "n8n": 0, "provider": 0}
        self.paid_cost_usd = Decimal("0")
        self.effect_order: list[str] = []
        self._controlled_recovery_injected = False
        self._codex_outcomes: list[FactoryLiveEffectOutcomeV1] = []
        self._provider_outcomes: list[FactoryLiveEffectOutcomeV1] = []
        self._provider_requests: list[FactoryLiveEffectRequestV1] = []
        self._tmp = TemporaryDirectory(prefix="captain-six-skill-")
        self.root = Path(self._tmp.name)
        self.catalog = _catalog_for(self.root, *tuple(FactorySkillStep))
        self.replay_store = InMemoryFactorySkillReplayStore()
        self.evidence_store = FilesystemFactoryEvidenceStore(self.root / "evidence")
        self.hermes = DeterministicHermes(
            self,
            settings=HermesCliSettings(
                skill_root=self.root,
                evidence_root=self.root / "evidence",
            ),
            evidence_store=self.evidence_store,
            released_skill_catalog=self.catalog,
            replay_store=self.replay_store,
            clock=self.clock,
        )
        self.leases = DeterministicLeases(self.clock)
        self.improvements = DeterministicImprovements()
        raw_dispatcher = FactoryDispatcher(
            coordinator=self.coordinator,
            hermes=self.hermes,
            forge=self.forge,
            leases=self.leases,
            clock=self.clock,
            improvements=self.improvements,
        )
        self.dispatcher = DeterministicDispatcher(self, raw_dispatcher)
        self.effect_ledger = RecordingLiveEffectLedger(self.effect_order)
        self.live_executor = RecoveringLiveExecutor(self)
        self.live_runner = FactoryLiveRunner(
            repository=self.repository,
            effect_ledger=self.effect_ledger,
            plan=LiveEffectPlan(self),
            executor=self.live_executor,
            clock=self.clock,
        )
        self.used_composed_ports = self._compose_ports()
        self._attempt_runs: dict[int, tuple[TeamExecutionEvidenceV1, ...]] = {}
        self._feedback_by_attempt: dict[int, FactoryFeedbackV1] = {}
        self._last_result: SixSkillHarnessResult | None = None
        self._replayed_evidence = 0
        self._pre_promotion_status = "running"
        if change_skill_digest:
            first_skill = self.root / "captain-factory-discover" / "SKILL.md"
            first_skill.write_text("# digest changed after release\n", encoding="utf-8")

    async def run(self) -> SixSkillHarnessResult:
        from agenten.agent_factory.factory_live_entrypoint import (
            FactorySixSkillLiveCoordinator,
        )

        if self._last_result is not None:
            replay = await self.live_runner.run(
                self.job,
                mode=self.job.execution_policy.mode.value,
            )
            self._replayed_evidence += sum(item.replayed for item in replay.effects)
        runtime = FactorySixSkillLiveCoordinator(
            coordinator=self.coordinator,
            repository=self.repository,
            dispatcher=self.dispatcher,
            live_runner=self.live_runner,
            clock=self.clock,
        )
        result = await runtime.run(
            self.job,
            self.job.execution_policy.mode.value,
        )
        if self.restart_after_evidence and result.status == "ready_to_use":
            replay = await self.live_runner.run(self.job, mode="release")
            self._replayed_evidence += sum(item.replayed for item in replay.effects)
        projection = self.coordinator.projection(self.job.job_id)
        feedback = self._latest_feedback()
        receipts = tuple(self.repository.receipts)
        wrapped = SixSkillHarnessResult(
            coordinator_result=result,
            skill_steps=tuple(step.value for step in result.skill_steps),
            attempts=result.attempt,
            gateway_projection=projection,
            gateway_phases=tuple(
                block.phase.value for block in self.coordinator.blocks(self.job.job_id)
            ),
            workflow_artifacts=tuple(self.repository.artifacts),
            usage_receipts=receipts,
            total_cost_usd=sum(
                (receipt.cost_usd for receipt in receipts), Decimal("0")
            ),
            feedback=feedback,
            tool_gap_severities=tuple(gap.severity for gap in feedback.tool_gaps),
            effect_counts=dict(self.effect_counts),
            effect_order=tuple(self.effect_order),
            infrastructure_recoveries=self.live_executor.recover_calls,
            recovered_reservations=self.live_executor.recover_calls,
            replayed_evidence=self._replayed_evidence,
            used_composed_ports=self.used_composed_ports,
            live_status=(
                result.runner_reports[-1].status
                if result.runner_reports
                else result.status
            ),
            captain_promoted=result.promotion_block is not None,
            pre_promotion_status=self._pre_promotion_status,
        )
        self._last_result = wrapped
        return wrapped

    def artifact_for(self, invocation: FactorySkillInvocationV1):
        step = invocation.step
        attempt = invocation.attempt
        if step is FactorySkillStep.DISCOVER:
            artifact = CodebaseInventoryV1.model_validate(
                self._bound_payload(inventory_payload(), invocation)
            )
        elif step is FactorySkillStep.BRIEF_CODEX:
            payload = brief_payload()
            assignment = payload["build_assignment"]
            assert isinstance(assignment, dict)
            assignment.update(
                {
                    "correlation_id": str(invocation.correlation_id),
                    "subject_version": invocation.subject_version,
                    "attempt": attempt,
                    "idempotency_key": invocation.idempotency_key,
                    "released_skill": {
                        "skill_id": invocation.released_skill.skill_id,
                        "version": invocation.released_skill.version,
                        "content_ref": invocation.released_skill.content_ref.model_dump(
                            mode="json"
                        ),
                        "content_sha256": invocation.released_skill.content_sha256,
                    },
                    "workspace_ref": invocation.lease.workspace_ref,
                    "public_assertion_ids": list(
                        invocation.acceptance_assertion_ids
                    ),
                }
            )
            if not self._codex_outcomes:
                raise AssertionError("Codex brief was materialized before its claimed effect")
            retry_refs: list[dict[str, object]] = []
            if invocation.attempt > 1:
                authorization = _improvement_authorization()
                retry_refs = [
                    authorization.authorization_ref.model_dump(mode="json"),
                    authorization.failed_evaluation.artifact_ref.model_dump(
                        mode="json"
                    ),
                    authorization.prior_candidate_ref.model_dump(mode="json"),
                ]
            payload["context_refs"] = [
                *list(payload["context_refs"]),
                *retry_refs,
                self._codex_outcomes[-1].evidence_ref.model_dump(mode="json"),
            ]
            artifact = CodexBuildBriefV1.model_validate(
                self._bound_payload(payload, invocation)
            )
        elif step is FactorySkillStep.IMPROVE_TEAM:
            from agenten.agent_factory.skill_workflow_contracts import CandidateRevisionV1

            artifact = CandidateRevisionV1.model_validate(
                self._bound_payload(revision_payload(), invocation)
            )
        elif step is FactorySkillStep.EXECUTE_TEAM:
            artifact = self._execution_for(invocation)
        elif step is FactorySkillStep.EVALUATE_TEAM:
            artifact = self._evaluation_for(invocation)
        elif step is FactorySkillStep.REPORT_CAPTAIN:
            artifact = self._feedback_for(invocation)
        else:
            raise AssertionError(f"unhandled skill step {step.value}")
        return artifact

    def _execution_for(
        self,
        invocation: FactorySkillInvocationV1,
    ) -> TeamExecutionEvidenceV1:
        raise AssertionError(
            "execute_team must consume claimed provider results through the dispatcher"
        )

    def materialize_execution_batch(
        self,
        attempt: int,
    ) -> tuple[TeamExecutionEvidenceV1, ...]:
        failed = (
            self.first_run == "behavioral_failure" and attempt == 1
        ) or (
            attempt == 2
            and self.repository.budget.remaining_usd <= 0
        ) or self.failure is not None
        run_count = (
            1
            if self.job.execution_policy.mode.value == "demo"
            else self.job.execution_policy.required_live_runs
        )
        runs = tuple(workflow_run(number, attempt=attempt) for number in range(1, run_count + 1))
        staged = tuple(
            outcome
            for outcome in self._provider_outcomes
            if outcome.attempt == attempt
        )
        requests = tuple(
            request
            for request in self._provider_requests
            if request.attempt == attempt
        )
        if len(staged) != run_count or len(requests) != run_count:
            raise AssertionError(
                "execute_team evidence was materialized before exact claimed provider effects"
            )
        runs = tuple(
            TeamExecutionEvidenceV1.model_validate(
                self._bound_payload(
                    run.model_copy(
                        update={
                            "evidence_refs": tuple(
                                dict.fromkeys(
                                    (*run.evidence_refs, outcome.evidence_ref)
                                )
                            )
                        }
                    ).model_dump(mode="json", by_alias=True),
                    request.invocation,
                )
            )
            for run, outcome, request in zip(runs, staged, requests, strict=True)
        )
        if failed:
            runs = tuple(self._failed_run(run) for run in runs)
        self._attempt_runs[attempt] = runs
        cost = Decimal("0") if attempt == 2 and self.repository.budget.remaining_usd <= 0 else Decimal("0.25")
        if cost:
            receipts = tuple(
                receipt.model_copy(update={"cost_usd": cost})
                for receipt in workflow_receipts(runs)
            )
            self.repository.receipts.extend(receipts)
            consumed = self.repository.budget.consumed_usd + sum(
                (receipt.cost_usd for receipt in receipts), Decimal("0")
            )
            self.repository.replace_budget(consumed)
        return runs

    def _evaluation_for(
        self,
        invocation: FactorySkillInvocationV1,
    ) -> TeamEvaluationV1:
        runs = self._attempt_runs[invocation.attempt]
        evaluation = workflow_evaluation(runs, budget=self.repository.budget)
        if self.failure is not None:
            evaluation = evaluation.model_copy(update={"failure_class": self.failure})
        return TeamEvaluationV1.model_validate(
            self._bound_payload(
                evaluation.model_dump(mode="json", by_alias=True),
                invocation,
            )
        )

    def _feedback_for(
        self,
        invocation: FactorySkillInvocationV1,
    ) -> FactoryFeedbackV1:
        evaluation = next(
            artifact
            for artifact in reversed(self.repository.artifacts)
            if isinstance(artifact, TeamEvaluationV1)
            and artifact.attempt == invocation.attempt
        )
        gaps: tuple[ToolGapMarker, ...] = ()
        if self.tool_gap is not None:
            gap = ToolGapMarker.model_validate(
                tool_gap_payload(severity=self.tool_gap)
            )
            gaps = (gap,)
        feedback = FactoryFeedbackBuilder(clock=self.clock).build(
            invocation=invocation,
            candidate_ref=self._attempt_runs[invocation.attempt][0].candidate_ref,
            evaluation=evaluation,
            tool_gaps=gaps,
            budget_projection=self.repository.budget,
        )
        self._feedback_by_attempt[invocation.attempt] = feedback
        return feedback

    def mark_pre_promotion(self) -> None:
        self._pre_promotion_status = self.coordinator.projection(
            self.job.job_id
        ).status.value

    def stage_outcome(
        self,
        kind: FactoryLiveEffectKind,
        outcome: FactoryLiveEffectOutcomeV1,
    ) -> None:
        target = (
            self._provider_outcomes
            if kind is FactoryLiveEffectKind.PROVIDER
            else self._codex_outcomes
        )
        if outcome not in target:
            target.append(outcome)

    def stage_request(self, request: FactoryLiveEffectRequestV1) -> None:
        if (
            request.kind is FactoryLiveEffectKind.PROVIDER
            and request not in self._provider_requests
        ):
            self._provider_requests.append(request)

    def external_block(
        self,
        phase: FactoryPhase,
        *,
        attempt: int,
        producer: str,
        role: FactoryRole = FactoryRole.TOOL_INTEGRATOR,
        artifact_refs: tuple[ArtifactRef, ...] | None = None,
        evidence_refs: tuple[ArtifactRef, ...] | None = None,
        assertion_ids: tuple[str, ...] = (),
    ) -> FactoryEvidenceBlock:
        reference = ArtifactRef(
            uri=f"artifact://deterministic/{attempt}/{phase.value}",
            sha256=hashlib.sha256(
                f"{attempt}:{phase.value}".encode("utf-8")
            ).hexdigest(),
            media_type="application/json",
        )
        return FactoryEvidenceBlock(
            schema_name="captain.agent-factory-block.v1",
            event_id=uuid5(
                NAMESPACE_URL,
                f"deterministic:{self.job.job_id}:{attempt}:{phase.value}",
            ),
            job_id=self.job.job_id,
            correlation_id=self.job.correlation_id,
            causation_id=self.job.event_id,
            occurred_at=self.clock(),
            producer=producer,
            subject_version=self.job.subject_version,
            attempt=attempt,
            phase=phase,
            role=role,
            status=FactoryBlockStatus.SUCCEEDED,
            artifact_refs=artifact_refs or (reference,),
            evidence_refs=evidence_refs or (reference,),
            assertion_ids=assertion_ids,
            lease_id=f"deterministic-{attempt}-{phase.value}",
        )

    def _bound_payload(
        self,
        payload: dict[str, object],
        invocation: FactorySkillInvocationV1,
    ) -> dict[str, object]:
        bound = dict(payload)
        bound.update(
            {
                "invocation": invocation.model_dump(mode="json", by_alias=True),
                "invocation_id": str(invocation.invocation_id),
                "job_id": str(invocation.job_id),
                "correlation_id": str(invocation.correlation_id),
                "subject_version": invocation.subject_version,
                "attempt": invocation.attempt,
                "occurred_at": (NOW + timedelta(minutes=1)).isoformat(),
                "acceptance_assertion_ids": list(
                    invocation.acceptance_assertion_ids
                ),
            }
        )
        return bound

    @staticmethod
    def _failed_run(run: TeamExecutionEvidenceV1) -> TeamExecutionEvidenceV1:
        outcomes = tuple(
            outcome.model_copy(update={"status": "failed"})
            for outcome in run.execution_outcome.assertion_outcomes
        )
        execution_outcome = run.execution_outcome.model_copy(
            update={"status": "failed", "assertion_outcomes": outcomes}
        )
        return run.model_copy(
            update={"status": "failed", "execution_outcome": execution_outcome}
        )

    def _latest_feedback(self) -> FactoryFeedbackV1:
        if self._feedback_by_attempt:
            return self._feedback_by_attempt[max(self._feedback_by_attempt)]
        return FactoryFeedbackV1.model_validate(feedback_payload())

    def _compose_ports(self) -> bool:
        integration_intent = (
            IntegrationIntent.N8N if self.with_n8n else IntegrationIntent.NONE
        )
        n8n_lease = None
        n8n_delegate = None
        if self.with_n8n:
            n8n_lease = issue_factory_lease(
                job=self.job,
                role=FactoryRole.TOOL_INTEGRATOR,
                attempt=1,
                workspace_ref="workspace://factory/n8n",
                now=NOW,
                integration_intent=IntegrationIntent.N8N,
            )
            n8n_delegate = object()
        components = compose_live_factory_runtime(
            job=self.job,
            evidence_store=self.evidence_store,
            ports=FactoryLiveRuntimePorts(
                hermes=self.hermes,
                codex=object(),  # type: ignore[arg-type]
                context7=None,
                candidate_provider=object(),  # type: ignore[arg-type]
                minibook=None,
                model_client_for=lambda *_: object(),  # type: ignore[return-value]
                budget=InMemoryFactoryBudgetLedger(),
                pricing_source=object(),  # type: ignore[arg-type]
                replay_store=self.replay_store,
                holdout_source=object(),  # type: ignore[arg-type]
                holdout_evaluator=object(),  # type: ignore[arg-type]
                integration_intent=integration_intent,
                n8n_delegate=n8n_delegate,  # type: ignore[arg-type]
                n8n_lease=n8n_lease,
                n8n_authority=object(),  # type: ignore[arg-type]
                released_skill_catalog=self.catalog,
                skill_root=self.root,
                tools={},
                provider="deterministic-provider",
                model="approved-model-id",
                max_cost_per_call=Decimal("0.25"),
                clock=self.clock,
            ),
            holdout_id=self.job.private_holdout_refs[0].holdout_id,
        )
        return components.hermes is self.hermes


__all__ = [
    "FIRST_PASS_STEPS",
    "RETRY_STEPS",
    "SixSkillFactoryHarness",
    "SixSkillHarnessResult",
]
