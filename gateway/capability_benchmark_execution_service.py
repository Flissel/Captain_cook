"""Gateway-owned execution and evidence issuance for isolated benchmarks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from collections.abc import Mapping
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeCommandPayload,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityProfile,
    IntegrationIntent,
    RuntimeLimits,
    RuntimeOperation,
    RuntimeStatus,
)
from gateway.capability_generic_benchmark import (
    GenericBenchmarkExecutionEvidence,
    GenericBenchmarkExecutionRequest,
    GenericExecutionCaseView,
    GenericAuthorizedToolCall,
    GenericToolCallEvidence,
    generic_execution_request_digest,
)
from gateway.capability_live_pipeline import CapabilityLiveAuthorityUnavailable


@dataclass(frozen=True)
class BenchmarkRuntimeEffectResult:
    result: AgentRuntimeResult
    observations: dict[str, object]
    rationale: str
    messages: tuple[tuple[str, str], ...]
    handoffs: tuple[tuple[str, str, str], ...]
    tool_result_refs: dict[str, ArtifactRef]
    cost_usd: Decimal
    latency_ms: int
    candidate_ref: ArtifactRef
    execution_ref: ArtifactRef
    agent_names: tuple[str, ...]


class BenchmarkToolRegistryPort(Protocol):
    digest: str

    def require_declared(self, tool_keys: tuple[str, ...]) -> None: ...

    def operation_for(self, tool_key: str) -> str: ...

    def execute(
        self, *, tool_key: str, operation: str,
        arguments: Mapping[str, object],
    ) -> dict[str, object]: ...


class GatewayBenchmarkToolBrokerSession:
    """Single-use broker for the exact pre-authorized calls in one execution."""

    def __init__(
        self, *, registry: BenchmarkToolRegistryPort,
        artifacts: "BenchmarkEvidenceArtifactsPort",
        authorizations: tuple[GenericAuthorizedToolCall, ...],
    ) -> None:
        registry.require_declared(tuple(item.tool_key for item in authorizations))
        self.registry_digest = registry.digest
        self._registry = registry
        self._artifacts = artifacts
        self._by_key = {item.tool_key: item for item in authorizations}
        if len(self._by_key) != len(authorizations):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark tool authorization is ambiguous"
            )
        self._results: dict[str, ArtifactRef] = {}

    def execute(
        self, *, tool_key: str, call_id: str, lease_id: str,
        operation: str, arguments: Mapping[str, object],
    ) -> ArtifactRef:
        authorization = self._by_key.get(tool_key)
        if (
            authorization is None
            or authorization.call_id != call_id
            or authorization.lease_id != lease_id
            or tool_key in self._results
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark tool lease is invalid or already consumed"
            )
        try:
            result = self._registry.execute(
                tool_key=tool_key, operation=operation, arguments=arguments,
            )
            payload = json.dumps(
                result, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
            reference = self._artifacts.put(payload, "application/json")
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark tool effect failed"
            ) from None
        if (
            reference.sha256 != hashlib.sha256(payload).hexdigest()
            or callable(getattr(self._artifacts, "read_bytes", None))
            and self._artifacts.read_bytes(reference) != payload
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark tool result readback failed"
            )
        self._results[tool_key] = reference
        return reference

    def execute_for_host(
        self, *, tool_key: str, call_id: str, lease_id: str,
        operation: str, arguments: Mapping[str, object],
    ) -> dict[str, object]:
        reference = self.execute(
            tool_key=tool_key, call_id=call_id, lease_id=lease_id,
            operation=operation, arguments=arguments,
        )
        try:
            value = json.loads(self._artifacts.read_bytes(reference))  # type: ignore[attr-defined]
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark tool result readback failed"
            ) from None
        if not isinstance(value, dict):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark tool result readback failed"
            )
        return value

    def result_refs(self) -> dict[str, ArtifactRef]:
        return dict(self._results)

    def operation_for(self, tool_key: str) -> str:
        return self._registry.operation_for(tool_key)


class BenchmarkRuntimeEffectPort(Protocol):
    async def execute(
        self, command: AgentRuntimeCommand, *,
        execution_plan: object,
        team_kind: str,
        authorized_tools: tuple[GenericAuthorizedToolCall, ...],
        tool_broker: GatewayBenchmarkToolBrokerSession,
        execution_case: GenericExecutionCaseView | None = None,
    ) -> BenchmarkRuntimeEffectResult: ...


class GatewayRuntimeBenchmarkEffect:
    """Execute through the real Runtime and parse its exact public evidence."""

    def __init__(self, *, executor: object, artifacts: object) -> None:
        if not callable(getattr(executor, "execute", None)):
            raise ValueError("benchmark Runtime executor is invalid")
        if not callable(getattr(artifacts, "read_bytes", None)):
            raise ValueError("benchmark Runtime artifact authority is invalid")
        self._executor = executor
        self._artifacts = artifacts

    async def execute(
        self, command: AgentRuntimeCommand, *,
        execution_plan: object,
        team_kind: str,
        authorized_tools: tuple[GenericAuthorizedToolCall, ...],
        tool_broker: GatewayBenchmarkToolBrokerSession,
        execution_case: GenericExecutionCaseView | None = None,
    ) -> BenchmarkRuntimeEffectResult:
        if not authorized_tools:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark tool authorization is unavailable"
            )
        del tool_broker, execution_case
        result = await self._executor.execute(command)
        if not isinstance(result, AgentRuntimeResult):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark Runtime result is invalid"
            )
        candidates: list[dict[str, object]] = []
        for reference in (*result.artifact_refs, *result.evidence_refs):
            try:
                payload = self._artifacts.read_bytes(reference)
                value = json.loads(payload)
            except Exception:
                continue
            if (
                isinstance(value, dict)
                and value.get("schema")
                == "captain.capability-benchmark-runtime-output.v1"
            ):
                candidates.append(value)
        if len(candidates) != 1:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark Runtime output is unavailable"
            )
        value = candidates[0]
        required = {
            "schema", "command_id", "observations", "rationale", "messages",
            "handoffs", "tool_result_refs", "cost_usd", "latency_ms",
            "candidate_ref", "execution_ref", "agent_names",
        }
        try:
            if set(value) != required or value["command_id"] != str(command.event_id):
                raise ValueError("binding")
            messages = tuple(
                (str(item["message_key"]), str(item["content"]))
                for item in value["messages"]  # type: ignore[union-attr]
            )
            handoffs = tuple(
                (str(item["from_agent"]), str(item["to_agent"]), str(item["reason_code"]))
                for item in value["handoffs"]  # type: ignore[union-attr]
            )
            tool_results = {
                str(key): ArtifactRef.model_validate(reference)
                for key, reference in value["tool_result_refs"].items()  # type: ignore[union-attr]
            }
            cost = Decimal(str(value["cost_usd"]))
            latency = int(value["latency_ms"])
            candidate_ref = ArtifactRef.model_validate(value["candidate_ref"])
            execution_ref = ArtifactRef.model_validate(value["execution_ref"])
            agent_names = tuple(str(item) for item in value["agent_names"])
            expected_execution_ref = (
                execution_plan.team_manifest_ref
                if team_kind == "team"
                else execution_plan.baseline_policy_ref
            )
            expected_agents = (
                execution_plan.team_agent_names
                if team_kind == "team"
                else (execution_plan.baseline_agent_name,)
            )
            if (
                candidate_ref != execution_plan.candidate_ref
                or execution_ref != expected_execution_ref
                or agent_names != expected_agents
            ):
                raise ValueError("execution binding")
            if not isinstance(value["observations"], dict) or not isinstance(value["rationale"], str):
                raise ValueError("shape")
        except (AttributeError, KeyError, TypeError, ValueError):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark Runtime output is invalid"
            ) from None
        return BenchmarkRuntimeEffectResult(
            result=result, observations=value["observations"],  # type: ignore[arg-type]
            rationale=value["rationale"], messages=messages, handoffs=handoffs,  # type: ignore[arg-type]
            tool_result_refs=tool_results, cost_usd=cost, latency_ms=latency,
            candidate_ref=candidate_ref, execution_ref=execution_ref,
            agent_names=agent_names,
        )


class BenchmarkEvidenceArtifactsPort(Protocol):
    def put(self, payload: bytes, media_type: str) -> ArtifactRef: ...


class BenchmarkToolIssuerPort(Protocol):
    authority_id: UUID

    def issue(self, **kwargs: object) -> GenericToolCallEvidence: ...
    def authorize(self, **kwargs: object) -> GenericAuthorizedToolCall: ...
    def complete(
        self, authorization: GenericAuthorizedToolCall, **kwargs: object,
    ) -> GenericToolCallEvidence: ...


class GatewayBenchmarkExecutionService:
    """Persist authority before Runtime effect and issue only read-back receipts."""

    def __init__(self, *, store: object, artifacts: BenchmarkEvidenceArtifactsPort,
                 private_cases: object, executor_authority_id: UUID,
                 runtime_effect: BenchmarkRuntimeEffectPort,
                 tool_issuer: BenchmarkToolIssuerPort,
                 tool_registry: BenchmarkToolRegistryPort,
                 clock=lambda: datetime.now(timezone.utc)) -> None:
        self._store = store
        self._artifacts = artifacts
        self._private_cases = private_cases
        self._executor_authority_id = executor_authority_id
        self._runtime_effect = runtime_effect
        self._tool_issuer = tool_issuer
        self._tool_registry = tool_registry
        self._clock = clock

    async def execute(
        self, request: GenericBenchmarkExecutionRequest,
    ) -> GenericBenchmarkExecutionEvidence:
        if request.request_digest != generic_execution_request_digest(
            job_id=request.job_id, capability_id=request.capability_id,
            input_ref=request.input_ref, case_ref=request.case_ref,
            team_kind=request.team_kind, model_id=request.model_id,
            model_policy_digest=request.model_policy_digest,
            execution_case=request.execution_case,
            execution_plan=request.execution_plan,
            request_nonce=request.request_nonce,
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark execution request authority changed"
            )
        canonical_case_ref, expected_view = (
            self._private_cases.resolve_execution_transport(
                request.case_ref, authority_id=self._executor_authority_id,
            )
        )
        transported = request.execution_case
        if (
            type(expected_view) is not GenericExecutionCaseView
            or expected_view.setup != transported.setup
            or expected_view.action != transported.action
            or expected_view.opaque_source_refs != transported.opaque_source_refs
            or expected_view.declared_tool_keys != transported.declared_tool_keys
            or expected_view.tool_authority_sha256
            != transported.tool_authority_sha256
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark execution tool authority changed"
            )
        tools = request.execution_case.declared_tool_keys
        expected_tools_digest = hashlib.sha256(json.dumps(
            list(tools), sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        if (
            not tools or len(set(tools)) != len(tools)
            or expected_tools_digest != request.execution_case.tool_authority_sha256
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark execution tool authority is invalid"
            )
        job = self._store.factory_job(request.job_id).job
        if (
            job.job_id != request.job_id
            or job.required_capability != request.capability_id
            or job.input_ref != request.input_ref
            or request.model_id not in job.execution_policy.allowed_models
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark execution Factory authority changed"
            )
        trace_id = uuid5(self._executor_authority_id, f"trace:{request.request_nonce}")
        run_id = uuid5(self._executor_authority_id, f"run:{request.request_nonce}")
        batch_id = f"bench-{request.job_id.hex[:12]}-{request.request_nonce.hex[:12]}"
        prompt_ref = self._put({
            "schema": "captain.capability-benchmark-runtime-input.v1",
            "job_id": str(request.job_id),
            "case_digest": request.case_ref.case_digest,
            "trace_id": str(trace_id),
            "run_id": str(run_id),
            "team_kind": request.team_kind,
            "candidate_ref": request.execution_plan.candidate_ref.model_dump(mode="json"),
            "execution_ref": (
                request.execution_plan.team_manifest_ref.model_dump(mode="json")
                if request.team_kind == "team"
                else request.execution_plan.baseline_policy_ref.model_dump(mode="json")
            ),
            "model_ref": request.execution_plan.model_ref.model_dump(mode="json"),
            "execution_policy_ref": request.execution_plan.execution_policy_ref.model_dump(mode="json"),
            "setup": request.execution_case.setup,
            "action": request.execution_case.action,
            "declared_tool_keys": list(tools),
            "tool_authority_sha256": expected_tools_digest,
        })
        command_id = uuid5(self._executor_authority_id, f"command:{request.request_nonce}")
        subtask_id = f"case-{request.case_ref.case_id[-16:]}"
        command = AgentRuntimeCommand(
            schema="captain.agent-runtime-command.v1",
            event_id=command_id,
            correlation_id=job.correlation_id,
            causation_id=request.request_nonce,
            occurred_at=self._clock(),
            producer="captain",
            subject_id=subtask_id,
            subject_version=job.subject_version,
            payload=AgentRuntimeCommandPayload(
                operation=RuntimeOperation.AUTOGEN_BENCHMARK,
                project_id=str(request.job_id), batch_id=batch_id,
                subtask_id=subtask_id,
                workspace_ref=f"workspace://capability-benchmark/{request.job_id}",
                prompt_ref=prompt_ref, integration_intent=IntegrationIntent.NONE,
                capability_profile=CapabilityProfile.FACTORY_REAL_CASE_TESTER,
                limits=RuntimeLimits(wall_seconds=300, max_iterations=1),
            ),
        )
        self._store.record_capability_benchmark_work_batch(
            job_id=request.job_id, correlation_id=job.correlation_id,
            batch_id=batch_id, case_digest=request.case_ref.case_digest,
            trace_id=trace_id, run_id=run_id, command_id=command_id,
            declared_tool_keys=tools,
            tool_authority_sha256=expected_tools_digest,
        )
        self._store.accept_runtime_command(command)
        authorized_tools = tuple(
            self._tool_issuer.authorize(
                job_id=request.job_id, case_ref=canonical_case_ref,
                tool_key=tool_key,
                operation=self._tool_registry.operation_for(tool_key),
                call_id=f"call-{command_id}-{tool_key}",
                lease_id=f"lease-{command_id}-{tool_key}",
                trace_id=trace_id, run_id=run_id,
                model_policy_digest=request.model_policy_digest,
                batch_id=batch_id, command_id=command_id,
                candidate_ref=request.execution_plan.candidate_ref,
                execution_ref=(
                    request.execution_plan.team_manifest_ref
                    if request.team_kind == "team"
                    else request.execution_plan.baseline_policy_ref
                ),
            )
            for tool_key in tools
        )
        broker = GatewayBenchmarkToolBrokerSession(
            registry=self._tool_registry, artifacts=self._artifacts,
            authorizations=authorized_tools,
        )
        effect = await self._runtime_effect.execute(
            command,
            execution_plan=request.execution_plan,
            team_kind=request.team_kind,
            authorized_tools=authorized_tools,
            tool_broker=broker,
            execution_case=request.execution_case,
        )
        result = effect.result
        expected_execution_ref = (
            request.execution_plan.team_manifest_ref
            if request.team_kind == "team"
            else request.execution_plan.baseline_policy_ref
        )
        expected_agents = (
            request.execution_plan.team_agent_names
            if request.team_kind == "team"
            else (request.execution_plan.baseline_agent_name,)
        )
        if (
            result.command_id != command_id
            or result.correlation_id != job.correlation_id
            or result.operation is not RuntimeOperation.AUTOGEN_BENCHMARK
            or result.status is not RuntimeStatus.SUCCEEDED
            or result.subject_version != job.subject_version
            or effect.candidate_ref != request.execution_plan.candidate_ref
            or effect.execution_ref != expected_execution_ref
            or effect.agent_names != expected_agents
            or set(effect.tool_result_refs) != set(tools)
            or effect.tool_result_refs != broker.result_refs()
            or any(
                reference not in (*result.artifact_refs, *result.evidence_refs)
                for reference in effect.tool_result_refs.values()
            )
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark Runtime effect readback changed authority"
            )
        self._store.record_runtime_result(result)
        operation = self._store.runtime_operation(command_id)
        if operation.command != command or operation.result != result:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark Runtime operation readback changed authority"
            )
        common = {
            "case_digest": request.case_ref.case_digest,
            "trace_id": str(trace_id), "run_id": str(run_id),
            "model_policy_digest": request.model_policy_digest,
        }
        outcome_ref = self._put({
            "schema": "captain.capability-benchmark-outcome.v1",
            **common, "observations": effect.observations,
        })
        rationale_ref = self._put({
            "schema": "captain.capability-benchmark-rationale.v1",
            **common, "text": effect.rationale,
        })
        message_refs = tuple(self._put({
            "schema": "captain.capability-benchmark-message.v1", **common,
            "message_key": key, "content": content,
        }) for key, content in effect.messages)
        handoff_refs = tuple(self._put({
            "schema": "captain.capability-benchmark-handoff.v1", **common,
            "from_agent": source, "to_agent": target, "reason_code": reason,
        }) for source, target, reason in effect.handoffs)
        tool_calls = tuple(
            self._tool_issuer.complete(
                authorization,
                job_id=request.job_id, case_ref=canonical_case_ref,
                operation=self._tool_registry.operation_for(
                    authorization.tool_key
                ),
                trace_id=trace_id, run_id=run_id,
                model_policy_digest=request.model_policy_digest,
                result_ref_sha256=effect.tool_result_refs[authorization.tool_key].sha256,
                batch_id=batch_id, execution_result_id=result.event_id,
            )
            for authorization in authorized_tools
        )
        return GenericBenchmarkExecutionEvidence(
            case_ref=request.case_ref, team_kind=request.team_kind,
            agent_count=len(effect.agent_names),
            model_id=request.model_id,
            model_policy_digest=request.model_policy_digest,
            input_ref=request.input_ref, capability_id=request.capability_id,
            outcome_ref=outcome_ref, message_event_refs=message_refs,
            handoff_event_refs=handoff_refs, tool_calls=tool_calls,
            rationale_ref=rationale_ref, cost_usd=effect.cost_usd,
            latency_ms=effect.latency_ms, trace_id=trace_id, run_id=run_id,
            request_nonce=request.request_nonce,
            request_digest=request.request_digest,
        )

    def _put(self, value: dict[str, object]) -> ArtifactRef:
        payload = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()
        reference = self._artifacts.put(payload, "application/json")
        if reference.sha256 != hashlib.sha256(payload).hexdigest():
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark evidence CAS readback changed bytes"
            )
        return reference


__all__ = [
    "BenchmarkRuntimeEffectPort",
    "BenchmarkRuntimeEffectResult",
    "GatewayBenchmarkExecutionService",
    "GatewayRuntimeBenchmarkEffect",
]
