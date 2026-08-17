"""Concrete host-AutoGen execution for the generic capability benchmark."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from autogen_agentchat.messages import HandoffMessage

from agenten.agent_factory.candidate_evaluation import FactoryCandidateEvaluator
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryRole
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
)
from agenten.agent_factory.team_execution import (
    AuthorityFreeBaselineTool,
    BudgetedChatCompletionClient,
    CaptainReleasedSkillAuthority,
    HostAutoGenSessionExecutor,
    HostAutoGenSessionIdentityV1,
    ResolvedFactoryHoldoutCase,
    SealedSingleAgentPolicyV1,
)
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    RuntimeOperation,
    RuntimeStatus,
)
from gateway.capability_benchmark_execution_service import (
    BenchmarkRuntimeEffectResult,
    GatewayBenchmarkToolBrokerSession,
)
from gateway.capability_generic_benchmark import (
    GenericAuthorizedToolCall,
    GenericBenchmarkExecutionPlan,
    GenericExecutionCaseView,
)
from gateway.capability_live_pipeline import CapabilityLiveAuthorityUnavailable


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()


def _execution_policy_digest(job: AgentFactoryJobV3) -> str:
    return hashlib.sha256(_canonical(
        job.execution_policy.model_dump(mode="json", by_alias=True)
    )).hexdigest()


class _PublicExecutionTask:
    def __init__(self, reference: PrivateHoldoutRef, body: bytes) -> None:
        self._reference = reference
        self._body = body

    async def resolve(self, reference: PrivateHoldoutRef) -> ResolvedFactoryHoldoutCase:
        if reference != self._reference:
            raise ValueError("benchmark task reference changed")
        return ResolvedFactoryHoldoutCase(reference=reference, body=self._body)

    async def evaluate(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ValueError("generic evaluation is owned by the independent evaluator")


class FactoryHostAutoGenBenchmarkEffect:
    """Run the released candidate or sealed baseline through host AutoGen."""

    def __init__(self, *, factory: object, artifacts: object,
                 clock=lambda: datetime.now(timezone.utc)) -> None:
        if not callable(getattr(factory, "snapshot", None)):
            raise ValueError("benchmark Factory authority is invalid")
        if not callable(getattr(artifacts, "put", None)) or not callable(
            getattr(artifacts, "read_bytes", None)
        ):
            raise ValueError("benchmark artifact authority is invalid")
        self._factory = factory
        self._artifacts = artifacts
        self._clock = clock

    async def execute(
        self, command: AgentRuntimeCommand, *,
        execution_plan: GenericBenchmarkExecutionPlan,
        team_kind: str,
        authorized_tools: tuple[GenericAuthorizedToolCall, ...],
        tool_broker: GatewayBenchmarkToolBrokerSession,
        execution_case: GenericExecutionCaseView | None = None,
    ) -> BenchmarkRuntimeEffectResult:
        if (
            command.payload.operation is not RuntimeOperation.AUTOGEN_BENCHMARK
            or team_kind not in {"team", "single_agent"}
            or execution_case is None
            or tuple(item.tool_key for item in authorized_tools)
            != execution_case.declared_tool_keys
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "host AutoGen benchmark authority is invalid"
            )
        snapshot = self._factory.snapshot(UUID(command.payload.project_id))
        job = snapshot.job
        if type(job) is not AgentFactoryJobV3:
            raise CapabilityLiveAuthorityUnavailable(
                "host AutoGen Factory job is invalid"
            )
        authorities = self._factory.team_execution.benchmark_authorities(job)
        candidate, ports, evidence_store = authorities
        if candidate.candidate.source_archive_ref != execution_plan.candidate_ref:
            raise CapabilityLiveAuthorityUnavailable(
                "host AutoGen candidate authority changed"
            )
        try:
            model_binding = json.loads(
                self._artifacts.read_bytes(execution_plan.model_ref)
            )
            policy_binding = json.loads(
                self._artifacts.read_bytes(execution_plan.execution_policy_ref)
            )
            with FactoryCandidateEvaluator().verified_team_workspace(candidate) as (
                _workspace, manifest,
            ):
                manifest_names = tuple(agent.name for agent in manifest.agents)
                manifest_tools = {
                    tool for agent in manifest.agents for tool in agent.tools
                }
            if (
                candidate.candidate.team_manifest.reference
                != execution_plan.team_manifest_ref
                or manifest_names != execution_plan.team_agent_names
                or set(execution_case.declared_tool_keys) - manifest_tools
                or model_binding != {
                    "schema": "captain.generic-benchmark-model.v1",
                    "model_id": ports.model,
                }
                or policy_binding != {
                    "schema": "captain.generic-benchmark-execution-policy.v1",
                    "model_policy_digest": (
                        json.loads(self._artifacts.read_bytes(
                            execution_plan.baseline_policy_ref
                        ))["model_policy_digest"]
                    ),
                    "execution_policy_sha256": _execution_policy_digest(job),
                }
            ):
                raise ValueError("binding")
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "host AutoGen execution plan authority changed"
            ) from None

        body = _canonical({
            "schema": "captain.generic-benchmark-public-task.v1",
            "setup": execution_case.setup,
            "action": execution_case.action,
            "opaque_source_refs": execution_case.opaque_source_refs,
        })
        body_sha = hashlib.sha256(body).hexdigest()
        case_ref = PrivateHoldoutRef(
            holdout_id=f"holdout-{body_sha[:12]}",
            uri=f"holdout://holdout-{body_sha[:12]}", sha256=body_sha,
        )
        now = self._clock()
        attempt = max(
            (block.attempt for block in snapshot.blocks if block.attempt is not None),
            default=1,
        )
        lease = issue_factory_lease(
            job=job, role=FactoryRole.REAL_CASE_TESTER, attempt=attempt,
            workspace_ref=(
                f"workspace://generic-benchmark/{job.job_id}/{command.event_id}"
            ), now=now,
        )
        released = ports.released_skill_catalog.released_for(
            job, FactorySkillStep.EXECUTE_TEAM,
        )
        binding = _canonical({
            "job_id": str(job.job_id), "command_id": str(command.event_id),
            "case_sha256": body_sha, "variant": team_kind,
            "released_skill_sha256": released.content_sha256,
        })
        idempotency_key = hashlib.sha256(binding).hexdigest()
        invocation = FactorySkillInvocationV1(
            schema="captain.factory-skill-invocation.v1",
            invocation_id=uuid5(NAMESPACE_URL, "generic-benchmark:" + idempotency_key),
            job_id=job.job_id, correlation_id=job.correlation_id,
            subject_version=job.subject_version, attempt=attempt,
            step=FactorySkillStep.EXECUTE_TEAM, released_skill=released,
            input_ref=job.input_ref, input_sha256=job.input_ref.sha256,
            lease=lease, idempotency_key=idempotency_key,
            acceptance_assertion_ids=job.acceptance_assertion_ids,
            execution_scope_ref=case_ref,
        )
        paid_authority = CaptainReleasedSkillAuthority(
            catalog=ports.released_skill_catalog, skill_root=ports.skill_root,
        )

        def model_client_for(_identity: HostAutoGenSessionIdentityV1) -> BudgetedChatCompletionClient:
            return BudgetedChatCompletionClient(
                job=job, invocation=invocation, attempt=attempt,
                delegate=ports.model_client_for(job, invocation),
                budget=ports.budget, evidence_store=evidence_store,
                provider=ports.provider, model=ports.model,
                max_cost_per_call=ports.max_cost_per_call,
                paid_effect_authority=paid_authority,
                pricing_authority=ports.pricing_authority, clock=ports.clock,
            )

        authorizations = {item.tool_key: item for item in authorized_tools}

        def broker_tool(tool_key: str):
            authorization = authorizations[tool_key]
            operation = tool_broker.operation_for(tool_key)

            def execute(arguments: dict[str, object]) -> dict[str, object]:
                return tool_broker.execute_for_host(
                    tool_key=tool_key, call_id=authorization.call_id,
                    lease_id=authorization.lease_id, operation=operation,
                    arguments=arguments,
                )

            execute.__name__ = tool_key
            execute.__doc__ = f"Execute the authorized {tool_key} benchmark tool."
            return execute

        tools = {key: broker_tool(key) for key in execution_case.declared_tool_keys}
        baseline_tools = {
            key: AuthorityFreeBaselineTool(name=key, function=function)
            for key, function in tools.items()
        }
        session = HostAutoGenSessionExecutor(
            model_client_factory=model_client_for,
            evidence_store=evidence_store,
            holdouts=_PublicExecutionTask(case_ref, body),  # type: ignore[arg-type]
            tools=tools, baseline_tools=baseline_tools,
            clock=ports.clock,
        )
        variant = "candidate" if team_kind == "team" else "single_agent_baseline"
        subject_id = (
            candidate.candidate.candidate_id
            if team_kind == "team" else execution_plan.baseline_agent_name
        )
        identity = HostAutoGenSessionIdentityV1.for_factory_execution(
            job=job, invocation=invocation, case_ref=case_ref,
            subject_id=subject_id, variant=variant,
            request_id=command.event_id,
            runtime_session_id=f"generic-benchmark-{command.event_id}",
            effect_id=hashlib.sha256(binding + b":effect").hexdigest(),
            claim_id=uuid5(command.event_id, "host-autogen-claim"), fence=1,
            model=ports.model,
        )
        started = time.monotonic_ns()
        if team_kind == "team":
            result = await session.run_candidate(
                job=job, invocation=invocation, case_ref=case_ref,
                identity=identity, candidate=candidate,
                allowed_tools=execution_case.declared_tool_keys,
                allowed_models=job.execution_policy.allowed_models,
                max_seconds=float(candidate.candidate.timeout_seconds),
            )
            execution_ref = execution_plan.team_manifest_ref
            agent_names = execution_plan.team_agent_names
        else:
            policy = self._baseline_policy(execution_plan, job)
            with FactoryCandidateEvaluator().verified_team_workspace(candidate) as (
                workspace, manifest,
            ):
                if candidate.candidate.team_manifest.reference != execution_plan.team_manifest_ref:
                    raise CapabilityLiveAuthorityUnavailable(
                        "host AutoGen team manifest authority changed"
                    )
                result = await session.run_baseline(
                    job=job, invocation=invocation, case_ref=case_ref,
                    identity=identity, policy=policy, workspace=workspace,
                    allowed_models=job.execution_policy.allowed_models,
                    max_seconds=float(candidate.candidate.timeout_seconds),
                )
                del manifest
            execution_ref = execution_plan.baseline_policy_ref
            agent_names = (execution_plan.baseline_agent_name,)
        latency_ms = max(1, (time.monotonic_ns() - started) // 1_000_000)
        result_refs = tool_broker.result_refs()
        if set(result_refs) != set(execution_case.declared_tool_keys):
            raise CapabilityLiveAuthorityUnavailable(
                "host AutoGen did not execute the exact declared tools"
            )
        messages = tuple(
            (f"message-{index}", str(getattr(message, "content", "")))
            for index, message in enumerate(result.task_result.messages, start=1)
        )
        handoffs = tuple(
            (
                str(message.source), str(message.target), "autogen_handoff",
            )
            for message in result.task_result.messages
            if isinstance(message, HandoffMessage)
        )
        output = {
            "schema": "captain.capability-benchmark-runtime-output.v1",
            "command_id": str(command.event_id),
            "observations": {
                "conversation_pattern": result.conversation_pattern,
                "message_count": result.message_count,
                "handoff_count": result.handoff_count,
                "tool_call_count": result.tool_call_count,
                "termination_reason": result.termination_reason,
            },
            "rationale": result.termination_reason,
            "messages": [
                {"message_key": key, "content": content} for key, content in messages
            ],
            "handoffs": [
                {"from_agent": source, "to_agent": target, "reason_code": reason}
                for source, target, reason in handoffs
            ],
            "tool_result_refs": {
                key: reference.model_dump(mode="json")
                for key, reference in result_refs.items()
            },
            "cost_usd": format(sum(
                (receipt.cost_usd for receipt in result.usage_receipts), Decimal("0")
            ), "f"),
            "latency_ms": latency_ms,
            "candidate_ref": execution_plan.candidate_ref.model_dump(mode="json"),
            "execution_ref": execution_ref.model_dump(mode="json"),
            "agent_names": list(agent_names),
        }
        output_ref = self._put(_canonical(output))
        runtime_result = AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=uuid5(command.event_id, "host-autogen-benchmark-result"),
            command_id=command.event_id, correlation_id=command.correlation_id,
            occurred_at=self._clock(), producer="agent-runtime",
            subject_id=command.subject_id, subject_version=command.subject_version,
            grant_id=f"benchmark-{command.event_id}",
            operation=RuntimeOperation.AUTOGEN_BENCHMARK,
            status=RuntimeStatus.SUCCEEDED,
            session_id=identity.runtime_session_id,
            artifact_refs=(output_ref, *result_refs.values()),
            evidence_refs=(result.runtime_evidence_ref,),
        )
        return BenchmarkRuntimeEffectResult(
            result=runtime_result, observations=output["observations"],  # type: ignore[arg-type]
            rationale=result.termination_reason, messages=messages,
            handoffs=handoffs, tool_result_refs=result_refs,
            cost_usd=sum(
                (receipt.cost_usd for receipt in result.usage_receipts), Decimal("0")
            ), latency_ms=latency_ms,
            candidate_ref=execution_plan.candidate_ref,
            execution_ref=execution_ref, agent_names=agent_names,
        )

    def _baseline_policy(
        self, plan: GenericBenchmarkExecutionPlan, job: AgentFactoryJobV3,
    ) -> SealedSingleAgentPolicyV1:
        try:
            value = json.loads(self._artifacts.read_bytes(plan.baseline_policy_ref))
            if (
                not isinstance(value, dict)
                or value.get("schema") != "captain.generic-benchmark-baseline-policy.v1"
                or value.get("candidate_ref")
                != plan.candidate_ref.model_dump(mode="json")
                or value.get("agent_name") != plan.baseline_agent_name
                or value.get("execution_policy_sha256") != _execution_policy_digest(job)
            ):
                raise ValueError("binding")
            return SealedSingleAgentPolicyV1.seal(
                agent_name=plan.baseline_agent_name,
                system_prompt_ref=ArtifactRef.model_validate(value["system_prompt_ref"]),
                execution_policy_sha256=_execution_policy_digest(job),
                model=str(value["model_id"]),
                allowed_tools=tuple(str(item) for item in value["allowed_tools"]),
                max_messages=int(value["max_messages"]),
                max_tool_calls=int(value["max_tool_calls"]),
            )
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "sealed single-agent benchmark policy is invalid"
            ) from None

    def _put(self, payload: bytes) -> ArtifactRef:
        reference = self._artifacts.put(payload, "application/json")
        if (
            reference.sha256 != hashlib.sha256(payload).hexdigest()
            or self._artifacts.read_bytes(reference) != payload
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "host AutoGen runtime evidence readback failed"
            )
        return reference


__all__ = ["FactoryHostAutoGenBenchmarkEffect"]
