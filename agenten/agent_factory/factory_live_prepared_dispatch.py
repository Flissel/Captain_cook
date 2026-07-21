"""Production adapters from prepared Factory effects to the durable live runner.

Preparation is read-only with respect to external effects.  The returned
requests remain bound to one authoritative Gateway action, while execution and
recovery stay separate so a restarted runner cannot start the effect twice.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryRole
from agenten.agent_factory.factory_live_runner import (
    FactoryLiveEffectKind,
    FactoryLiveEffectOutcomeV1,
    FactoryLiveEffectRequestV1,
)
from agenten.agent_factory.skill_sequence import SkillSequencePolicy
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_factory.state_machine import (
    FactoryAction,
    FactoryActionKind,
    FactoryProjection,
)


_ACTION_EFFECT_KIND = {
    FactoryActionKind.DISPATCH_TOOL_INTEGRATOR: FactoryLiveEffectKind.CODEX,
    FactoryActionKind.DISPATCH_REAL_CASE_TESTER: FactoryLiveEffectKind.PROVIDER,
}


class FactoryLivePreparedDispatch(BaseModel):
    """Frozen action plus the exact effect requests staged for that action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: FactoryAction
    requests: tuple[FactoryLiveEffectRequestV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_exact_action_binding(self) -> "FactoryLivePreparedDispatch":
        expected_kind = _ACTION_EFFECT_KIND.get(self.action.kind)
        if expected_kind is None:
            raise ValueError("Factory prepared dispatch action has no live effect")
        if self.action.job_id is None:
            raise ValueError("Factory prepared dispatch action lacks its job binding")
        if any(
            request.job_id != self.action.job_id
            or request.attempt != self.action.attempt
            or request.kind is not expected_kind
            for request in self.requests
        ):
            raise ValueError("Factory prepared request does not match its action")
        identities = tuple(
            (
                request.effect_id,
                request.invocation.invocation_id,
                request.idempotency_key,
            )
            for request in self.requests
        )
        if (
            len({item[0] for item in identities}) != len(identities)
            or len({item[1] for item in identities}) != len(identities)
            or len({item[2] for item in identities}) != len(identities)
        ):
            raise ValueError(
                "Factory prepared requests require unique effect, invocation, and idempotency identities"
            )
        return self


class FactoryLiveActionSourcePort(Protocol):
    def next_action(self, job_id: UUID) -> FactoryAction: ...


class FactoryLivePreparedDispatchPort(Protocol):
    """Stage sealed requests, execute once, or recover existing evidence."""

    def prepare(
        self,
        *,
        job: AgentFactoryJobV3,
        action: FactoryAction,
        expected_skill_digests: Mapping[str, str],
        projection: FactoryProjection,
        workflow_artifacts: tuple[object, ...],
    ) -> FactoryLivePreparedDispatch: ...

    async def execute(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectOutcomeV1: ...

    async def recover(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectOutcomeV1 | None: ...


class PreparedFactoryLivePlan:
    """Build one runner plan from the current authoritative Factory action."""

    def __init__(
        self,
        *,
        actions: FactoryLiveActionSourcePort,
        dispatch: FactoryLivePreparedDispatchPort,
        expected_skill_digests: Mapping[str, str],
    ) -> None:
        if not callable(getattr(actions, "next_action", None)):
            raise TypeError("Factory live action source port is incomplete")
        if any(
            not callable(getattr(dispatch, method, None))
            for method in ("prepare", "execute", "recover")
        ):
            raise TypeError("Factory prepared dispatch port is incomplete")
        digests = dict(expected_skill_digests)
        if not digests or any(
            not name
            or len(digest) != 64
            or digest.lower() != digest
            or any(character not in "0123456789abcdef" for character in digest)
            for name, digest in digests.items()
        ):
            raise ValueError("Factory prepared dispatch skill digests are invalid")
        self._actions = actions
        self._dispatch = dispatch
        self._expected_skill_digests = digests
        self._prepared_by_effect_id: dict[
            UUID,
            tuple[FactoryLivePreparedDispatch, FactoryLiveEffectRequestV1],
        ] = {}
        self._last_plan: tuple[
            AgentFactoryJobV3,
            Literal["demo", "release"],
            FactoryProjection,
            tuple[object, ...],
            FactoryAction,
            tuple[FactoryLiveEffectRequestV1, ...],
        ] | None = None
        self._skill_policy = SkillSequencePolicy()

    def effects_for(
        self,
        *,
        job: AgentFactoryJobV3,
        mode: Literal["demo", "release"],
        projection: FactoryProjection,
        workflow_artifacts: tuple[object, ...],
    ) -> tuple[FactoryLiveEffectRequestV1, ...]:
        if job.execution_policy.mode.value != mode:
            raise ValueError("Factory prepared plan mode does not match the job")
        if projection.job != job:
            raise ValueError("Factory prepared plan projection does not match the job")
        action = self._actions.next_action(job.job_id)
        if action.job_id != job.job_id or action.attempt != projection.attempt:
            raise ValueError("Factory prepared plan action does not match the projection")
        if action.kind is FactoryActionKind.VALIDATE_FOR_PROMOTION:
            return ()
        if self._last_plan is not None:
            (
                cached_job,
                cached_mode,
                cached_projection,
                cached_artifacts,
                cached_action,
                cached_requests,
            ) = self._last_plan
            if (
                cached_job == job
                and cached_mode == mode
                and cached_projection == projection
                and cached_artifacts == workflow_artifacts
                and cached_action == action
            ):
                return cached_requests
        prepared = self._dispatch.prepare(
            job=job,
            action=action,
            expected_skill_digests=self._expected_skill_digests,
            projection=projection,
            workflow_artifacts=workflow_artifacts,
        )
        if not isinstance(prepared, FactoryLivePreparedDispatch):
            raise TypeError("Factory prepared dispatch port returned an untyped result")
        if prepared.action != action:
            raise ValueError("Factory prepared dispatch changed the Gateway action")
        requests = self._bind_release_run(job, mode, prepared.requests)
        prepared = FactoryLivePreparedDispatch(
            action=prepared.action,
            requests=requests,
        )
        expected_steps = self._expected_steps(job, action)
        if tuple(
            request.invocation.step for request in prepared.requests
        ) != expected_steps:
            raise ValueError("Factory prepared plan lacks the exact live request sequence")
        for request in prepared.requests:
            self._require_job_binding(job, action, request)
            existing = self._prepared_by_effect_id.get(request.effect_id)
            current = (prepared, request)
            if existing is not None and existing != current:
                raise ValueError("Factory effect identity conflicts with its prepared request")
            self._prepared_by_effect_id[request.effect_id] = current
        self._last_plan = (
            job,
            mode,
            projection,
            workflow_artifacts,
            action,
            prepared.requests,
        )
        return prepared.requests

    def require_prepared(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLivePreparedDispatch:
        existing = self._prepared_by_effect_id.get(request.effect_id)
        if existing is None or existing[1] != request:
            raise ValueError("Factory executor requires the exact prepared request")
        prepared = existing[0]
        if self._actions.next_action(request.job_id) != prepared.action:
            raise ValueError("Factory Gateway action changed before the live effect")
        return prepared

    def uses_dispatch(self, dispatch: FactoryLivePreparedDispatchPort) -> bool:
        return dispatch is self._dispatch

    @staticmethod
    def _bind_release_run(
        job: AgentFactoryJobV3,
        mode: Literal["demo", "release"],
        requests: tuple[FactoryLiveEffectRequestV1, ...],
    ) -> tuple[FactoryLiveEffectRequestV1, ...]:
        if mode != "release":
            return requests
        bound = tuple(request for request in requests if request.run_id is not None)
        if bound:
            first = bound[0]
            count = len(requests)
            if (
                len(bound) != count
                or first.run_effect_count != count
                or tuple(request.run_effect_index for request in requests)
                != tuple(range(1, count + 1))
                or any(
                    request.run_id != first.run_id
                    or request.run_effect_count != count
                    for request in requests
                )
            ):
                raise ValueError("Factory prepared release run binding mismatch")
            return requests
        run_id = uuid5(
            NAMESPACE_URL,
            "|".join(
                (
                    "captain.factory-live-run.v1",
                    str(job.job_id),
                    str(job.subject_version),
                    str(requests[0].attempt),
                    *(str(request.effect_id) for request in requests),
                )
            ),
        )
        count = len(requests)
        return tuple(
            request.model_copy(
                update={
                    "run_id": run_id,
                    "run_effect_index": index,
                    "run_effect_count": count,
                }
            )
            for index, request in enumerate(requests, start=1)
        )

    def _expected_steps(
        self,
        job: AgentFactoryJobV3,
        action: FactoryAction,
    ) -> tuple[FactorySkillStep, ...]:
        if action.kind is FactoryActionKind.DISPATCH_TOOL_INTEGRATOR:
            return self._skill_policy.steps_for(
                role=FactoryRole.TOOL_INTEGRATOR,
                attempt=action.attempt,
            )
        if action.kind is FactoryActionKind.DISPATCH_REAL_CASE_TESTER:
            return (
                FactorySkillStep.EXECUTE_TEAM,
            ) * job.execution_policy.required_live_runs
        raise ValueError("Factory action does not require a prepared live effect")

    @staticmethod
    def _require_job_binding(
        job: AgentFactoryJobV3,
        action: FactoryAction,
        request: FactoryLiveEffectRequestV1,
    ) -> None:
        invocation = request.invocation
        lease = invocation.lease
        if (
            request.job_id != job.job_id
            or request.correlation_id != job.correlation_id
            or request.subject_version != job.subject_version
            or request.attempt != action.attempt
            or invocation.job_id != job.job_id
            or invocation.correlation_id != job.correlation_id
            or invocation.subject_version != job.subject_version
            or invocation.attempt != action.attempt
            or lease.job_id != job.job_id
            or lease.correlation_id != job.correlation_id
            or lease.subject_version != job.subject_version
            or lease.attempt != action.attempt
            or request.idempotency_key != invocation.idempotency_key
            or request.input_ref != invocation.input_ref
        ):
            raise ValueError(
                "Factory prepared request lacks exact job, lease, or idempotency binding"
            )


class PreparedFactoryLiveEffectExecutor:
    """Execute or recover only effects registered by ``PreparedFactoryLivePlan``."""

    def __init__(
        self,
        *,
        plan: PreparedFactoryLivePlan,
        dispatch: FactoryLivePreparedDispatchPort,
    ) -> None:
        if not plan.uses_dispatch(dispatch):
            raise ValueError("Factory plan and executor require the same dispatch port")
        self._plan = plan
        self._dispatch = dispatch

    async def execute(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectOutcomeV1:
        self._plan.require_prepared(request)
        outcome = await self._dispatch.execute(request)
        return _require_outcome_binding(request, outcome)

    async def recover(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectOutcomeV1 | None:
        self._plan.require_prepared(request)
        outcome = await self._dispatch.recover(request)
        if outcome is None:
            return None
        return _require_outcome_binding(request, outcome)


def _require_outcome_binding(
    request: FactoryLiveEffectRequestV1,
    outcome: FactoryLiveEffectOutcomeV1,
) -> FactoryLiveEffectOutcomeV1:
    if not isinstance(outcome, FactoryLiveEffectOutcomeV1):
        raise TypeError("Factory prepared dispatch returned an untyped outcome")
    if (
        outcome.effect_id != request.effect_id
        or outcome.job_id != request.job_id
        or outcome.correlation_id != request.correlation_id
        or outcome.subject_version != request.subject_version
        or outcome.attempt != request.attempt
    ):
        raise ValueError("Factory prepared effect outcome binding mismatch")
    return outcome
