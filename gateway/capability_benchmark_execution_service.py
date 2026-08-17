"""Gateway-owned execution and evidence issuance for isolated benchmarks."""

from __future__ import annotations

import hashlib
import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from collections.abc import Mapping
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import SecretStr
from agenten.provider_proxy.contracts import ProviderBudgetContextV1
from agenten.agent_factory.execution_budget import FactoryBudgetReservationV1

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


@dataclass(frozen=True)
class PreparedBenchmarkExecution:
    request: GenericBenchmarkExecutionRequest
    canonical_case_ref: object
    job: object
    command: AgentRuntimeCommand
    trace_id: UUID
    run_id: UUID
    batch_id: str
    authorized_tools: tuple[GenericAuthorizedToolCall, ...]
    material: dict[str, object]


class BenchmarkToolRegistryPort(Protocol):
    digest: str

    def require_declared(self, tool_keys: tuple[str, ...]) -> None: ...

    def operation_for(self, tool_key: str) -> str: ...

    def execute(
        self, *, tool_key: str, operation: str,
        arguments: Mapping[str, object],
    ) -> dict[str, object]: ...

    def execute_effect(
        self, *, tool_key: str, operation: str,
        arguments: Mapping[str, object], effect_id: str,
    ) -> dict[str, object]: ...

    def read_effect(
        self, *, tool_key: str, effect_id: str,
        request_sha256: str,
    ) -> dict[str, object] | None: ...

    def effect_request_sha256(
        self, *, tool_key: str, operation: str,
        arguments: Mapping[str, object], effect_id: str,
    ) -> str: ...


@dataclass(frozen=True)
class DurableBenchmarkToolResult:
    value: dict[str, object]
    result_ref: ArtifactRef
    receipt_ref: ArtifactRef


class GatewayDurableBenchmarkToolBroker:
    """Execute one scoped host effect and persist terminal readback before return."""

    def __init__(
        self, *, store: object, artifacts: "BenchmarkEvidenceArtifactsPort",
        registry: BenchmarkToolRegistryPort,
        after_persist=lambda: None,
    ) -> None:
        self._store = store
        self._artifacts = artifacts
        self._registry = registry
        self._after_persist = after_persist

    def execute(
        self, *, authorization: GenericAuthorizedToolCall,
        scoped_token: SecretStr, operation: str,
        arguments: Mapping[str, object],
    ) -> DurableBenchmarkToolResult:
        token = scoped_token.get_secret_value()
        token_sha256 = hashlib.sha256(token.encode()).hexdigest()
        authority = self._store.capability_tool_authorization(
            authorization.call_ref.sha256
        )
        if (
            not isinstance(authority, dict)
            or authority.get("tool_key") != authorization.tool_key
            or authorization.operation != operation
            or authority.get("operation") != operation
            or authority.get("call_id") != authorization.call_id
            or authority.get("lease_id") != authorization.lease_id
            or authority.get("call_ref")
            != authorization.call_ref.model_dump(mode="json")
            or authority.get("lease_ref")
            != authorization.lease_ref.model_dump(mode="json")
            or authority.get("scoped_token_sha256") != token_sha256
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark tool broker authority changed"
            )
        argument_bytes = json.dumps(
            dict(arguments), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        arguments_sha256 = hashlib.sha256(argument_bytes).hexdigest()
        effect_id = hashlib.sha256(json.dumps({
            "call_sha256": authorization.call_ref.sha256,
            "lease_sha256": authorization.lease_ref.sha256,
            "arguments_sha256": arguments_sha256,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        stored = self._store.capability_tool_effect(
            authorization.call_ref.sha256
        )
        if stored is not None:
            return self._read_stored(
                stored, authorization=authorization,
                arguments_sha256=arguments_sha256,
                scoped_token_sha256=token_sha256,
            )
        request_sha256 = self._registry.effect_request_sha256(
            tool_key=authorization.tool_key, operation=operation,
            arguments=arguments, effect_id=effect_id,
        )
        prior = self._registry.read_effect(
            tool_key=authorization.tool_key, effect_id=effect_id,
            request_sha256=request_sha256,
        )
        value = prior if prior is not None else self._registry.execute_effect(
            tool_key=authorization.tool_key, operation=operation,
            arguments=arguments, effect_id=effect_id,
        )
        result_ref = self._put(value)
        receipt = {
            "schema": "captain.capability-benchmark-tool-effect-receipt.v1",
            "status": "succeeded", "effect_id": effect_id,
            "tool_key": authorization.tool_key, "operation": operation,
            "job_id": authority["job_id"],
            "case_digest": authority["case_digest"],
            "trace_id": authority["trace_id"], "run_id": authority["run_id"],
            "model_policy_digest": authority["model_policy_digest"],
            "batch_id": authority["batch_id"],
            "command_id": authority["command_id"],
            "candidate_sha256": authority["candidate_sha256"],
            "execution_sha256": authority["execution_sha256"],
            "call_sha256": authorization.call_ref.sha256,
            "lease_sha256": authorization.lease_ref.sha256,
            "arguments_sha256": arguments_sha256,
            "result_sha256": result_ref.sha256,
        }
        receipt_ref = self._put(receipt)
        self._store.record_capability_tool_effect(
            call_ref=authorization.call_ref,
            lease_ref=authorization.lease_ref,
            result_ref=result_ref, receipt_ref=receipt_ref,
            arguments_sha256=arguments_sha256,
            scoped_token_sha256=token_sha256,
        )
        self._after_persist()
        stored = self._store.capability_tool_effect(
            authorization.call_ref.sha256
        )
        if stored is None:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark tool effect readback is unavailable"
            )
        return self._read_stored(
            stored, authorization=authorization,
            arguments_sha256=arguments_sha256,
            scoped_token_sha256=token_sha256,
        )

    def _put(self, value: dict[str, object]) -> ArtifactRef:
        body = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()
        reference = self._artifacts.put(body, "application/json")
        if (
            reference.sha256 != hashlib.sha256(body).hexdigest()
            or self._artifacts.read_bytes(reference) != body  # type: ignore[attr-defined]
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark tool broker CAS readback changed"
            )
        return reference

    def _read_stored(
        self, stored: dict[str, object], *,
        authorization: GenericAuthorizedToolCall,
        arguments_sha256: str, scoped_token_sha256: str,
    ) -> DurableBenchmarkToolResult:
        try:
            if (
                stored.get("call_ref") != authorization.call_ref.model_dump(mode="json")
                or stored.get("lease_ref") != authorization.lease_ref.model_dump(mode="json")
                or stored.get("arguments_sha256") != arguments_sha256
                or stored.get("scoped_token_sha256") != scoped_token_sha256
            ):
                raise ValueError("binding")
            result_ref = ArtifactRef.model_validate(stored["result_ref"])
            receipt_ref = ArtifactRef.model_validate(stored["receipt_ref"])
            value = json.loads(self._artifacts.read_bytes(result_ref))  # type: ignore[attr-defined]
            receipt = json.loads(self._artifacts.read_bytes(receipt_ref))  # type: ignore[attr-defined]
            if (
                not isinstance(value, dict) or not isinstance(receipt, dict)
                or receipt.get("status") != "succeeded"
                or receipt.get("call_sha256") != authorization.call_ref.sha256
                or receipt.get("lease_sha256") != authorization.lease_ref.sha256
                or receipt.get("result_sha256") != result_ref.sha256
                or receipt.get("arguments_sha256") != arguments_sha256
            ):
                raise ValueError("readback")
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark tool effect readback changed authority"
            ) from None
        return DurableBenchmarkToolResult(
            value=value, result_ref=result_ref, receipt_ref=receipt_ref,
        )


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
                 runtime_effect: BenchmarkRuntimeEffectPort | None,
                 tool_issuer: BenchmarkToolIssuerPort,
                 tool_registry: BenchmarkToolRegistryPort,
                 provider_binding: Mapping[str, object] | None = None,
                 clock=lambda: datetime.now(timezone.utc)) -> None:
        self._store = store
        self._artifacts = artifacts
        self._private_cases = private_cases
        self._executor_authority_id = executor_authority_id
        self._runtime_effect = runtime_effect
        self._tool_issuer = tool_issuer
        self._tool_registry = tool_registry
        self._provider_binding = None if provider_binding is None else dict(provider_binding)
        self._clock = clock

    def prepare(
        self, request: GenericBenchmarkExecutionRequest,
    ) -> PreparedBenchmarkExecution:
        """Persist the exact process execution and scoped leases before dispatch."""

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
        canonical_case_ref, expected_view = self._private_cases.resolve_execution_transport(
            request.case_ref, authority_id=self._executor_authority_id,
        )
        transported = request.execution_case
        if (
            type(expected_view) is not GenericExecutionCaseView
            or expected_view.setup != transported.setup
            or expected_view.action != transported.action
            or expected_view.opaque_source_refs != transported.opaque_source_refs
            or expected_view.declared_tool_keys != transported.declared_tool_keys
            or expected_view.tool_authority_sha256 != transported.tool_authority_sha256
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark execution tool authority changed"
            )
        tools = transported.declared_tool_keys
        tools_digest = hashlib.sha256(json.dumps(
            list(tools), sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        if not tools or len(set(tools)) != len(tools) or tools_digest != transported.tool_authority_sha256:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark execution tool authority is invalid"
            )
        self._tool_registry.require_declared(tools)
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
            "job_id": str(request.job_id), "case_digest": request.case_ref.case_digest,
            "trace_id": str(trace_id), "run_id": str(run_id),
            "team_kind": request.team_kind,
            "candidate_ref": request.execution_plan.candidate_ref.model_dump(mode="json"),
            "candidate_manifest_ref": request.execution_plan.candidate_manifest_ref.model_dump(mode="json"),
            "execution_ref": (
                request.execution_plan.team_manifest_ref.model_dump(mode="json")
                if request.team_kind == "team"
                else request.execution_plan.baseline_policy_ref.model_dump(mode="json")
            ),
            "model_ref": request.execution_plan.model_ref.model_dump(mode="json"),
            "execution_policy_ref": request.execution_plan.execution_policy_ref.model_dump(mode="json"),
            "setup": transported.setup, "action": transported.action,
            "declared_tool_keys": list(tools),
            "tool_authority_sha256": tools_digest,
        })
        command_id = uuid5(self._executor_authority_id, f"command:{request.request_nonce}")
        subtask_id = f"case-{request.case_ref.case_id[-16:]}"
        provider_binding = self._required_provider_binding()
        provider_result_id = uuid5(command_id, "captain.runtime-result")
        provider_session_id = f"benchmark-{command_id}"
        reservation_id = uuid5(command_id, "benchmark-provider-reservation")
        budget = self._store.factory_budget(request.job_id)
        ceiling = min(
            job.execution_policy.max_cost_usd / Decimal("30"),
            budget.remaining_usd,
        ).quantize(
            Decimal("0.01")
        )
        if ceiling <= 0:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark provider budget is unavailable"
            )
        authority_ref = (
            "gateway://capability-benchmark-authorizations/" + str(command_id)
        )
        context_seed = {
            "schema": "captain.provider-budget-context.v1",
            "command_id": command_id,
            "original_command_id": request.request_nonce,
            "result_id": provider_result_id,
            "reservation_id": reservation_id,
            "job_id": request.job_id, "run_id": trace_id,
            "input_id": f"{request.case_ref.case_digest}:{run_id}",
            "correlation_id": job.correlation_id,
            "capability_id": request.capability_id,
            "capability_version": job.subject_version,
            "session_id": provider_session_id,
            "cost_authority_ref": authority_ref,
            "ceiling_usd": ceiling,
            "expires_at": job.occurred_at.replace(microsecond=0)
            + timedelta(
                seconds=job.execution_policy.max_runtime_seconds
            ),
            "policy_sha256": provider_binding["provider_policy_sha256"],
            "price_card_sha256": provider_binding["provider_price_card_sha256"],
        }
        provider_context = ProviderBudgetContextV1.model_validate(context_seed)
        self._store.reserve_factory_budget(FactoryBudgetReservationV1(
            schema="captain.factory-budget-reservation.v1",
            reservation_id=reservation_id, job_id=request.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            execution_policy_sha256=_factory_execution_policy_sha256(job),
            attempt=1, requested_usd=ceiling,
            reserved_at=job.occurred_at.replace(microsecond=0),
            expires_at=provider_context.expires_at,
        ))
        command = AgentRuntimeCommand(
            schema="captain.agent-runtime-command.v1", event_id=command_id,
            correlation_id=job.correlation_id, causation_id=request.request_nonce,
            occurred_at=job.occurred_at, producer="captain", subject_id=subtask_id,
            subject_version=job.subject_version,
            payload=AgentRuntimeCommandPayload(
                operation=RuntimeOperation.AUTOGEN_BENCHMARK,
                project_id=str(request.job_id), batch_id=batch_id,
                subtask_id=subtask_id,
                workspace_ref=f"workspace://capability-benchmark/{request.job_id}",
                prompt_ref=prompt_ref, integration_intent=IntegrationIntent.NONE,
                capability_profile=CapabilityProfile.FACTORY_REAL_CASE_TESTER,
                limits=RuntimeLimits(wall_seconds=300, max_iterations=1),
                maximum_cost_usd=ceiling,
                budget_reservation_id=reservation_id,
                cost_authority_ref=authority_ref,
                cost_job_id=request.job_id, cost_run_id=trace_id,
                cost_input_id=f"{request.case_ref.case_digest}:{run_id}",
                cost_capability_id=request.capability_id,
                cost_capability_version=job.subject_version,
                provider_proxy_url=str(provider_binding["provider_proxy_url"]),
                provider_policy_sha256=str(provider_binding["provider_policy_sha256"]),
                provider_price_card_sha256=str(provider_binding["provider_price_card_sha256"]),
                provider_context_sha256=provider_context.sha256,
                provider_proxy_executable_sha256=str(provider_binding["provider_proxy_executable_sha256"]),
                provider_proxy_code_sha256=str(provider_binding["provider_proxy_code_sha256"]),
                provider_paid_command_sha256=str(provider_binding["provider_paid_command_sha256"]),
                provider_hard_ceiling_enforced=True,
                provider_session_id=provider_session_id,
                provider_result_id=provider_result_id,
            ),
        )
        self._store.record_capability_benchmark_work_batch(
            job_id=request.job_id, correlation_id=job.correlation_id,
            batch_id=batch_id, case_digest=request.case_ref.case_digest,
            trace_id=trace_id, run_id=run_id, command_id=command_id,
            declared_tool_keys=tools, tool_authority_sha256=tools_digest,
        )
        self._store.accept_runtime_command(command)
        self._store.record_capability_benchmark_provider_authority(
            context=provider_context,
            provider_binding=provider_binding,
            case_digest=request.case_ref.case_digest,
            trace_id=trace_id, run_id=run_id,
        )
        execution_ref = (
            request.execution_plan.team_manifest_ref
            if request.team_kind == "team"
            else request.execution_plan.baseline_policy_ref
        )
        authorized_tools = tuple(self._tool_issuer.authorize(
            job_id=request.job_id, case_ref=canonical_case_ref,
            tool_key=tool_key, operation=self._tool_registry.operation_for(tool_key),
            call_id=f"call-{command_id}-{tool_key}",
            lease_id=f"lease-{command_id}-{tool_key}",
            trace_id=trace_id, run_id=run_id,
            model_policy_digest=request.model_policy_digest,
            batch_id=batch_id, command_id=command_id,
            candidate_ref=request.execution_plan.candidate_ref,
            execution_ref=execution_ref,
        ) for tool_key in tools)

        def material(reference: ArtifactRef) -> str:
            body = self._artifacts.read_bytes(reference)  # type: ignore[attr-defined]
            if hashlib.sha256(body).hexdigest() != reference.sha256:
                raise CapabilityLiveAuthorityUnavailable(
                    "benchmark process material changed authority"
                )
            return base64.b64encode(body).decode("ascii")

        process_material = {
            "schema": "captain.capability-benchmark-process-material.v1",
            "job": job.model_dump(mode="json", by_alias=True),
            "candidate_archive_base64": material(request.execution_plan.candidate_ref),
            "candidate_manifest_base64": material(request.execution_plan.candidate_manifest_ref),
            "baseline_policy_base64": material(request.execution_plan.baseline_policy_ref),
            "model_binding_base64": material(request.execution_plan.model_ref),
            "execution_policy_base64": material(request.execution_plan.execution_policy_ref),
            "provider_binding": {
                **provider_binding,
                "provider_context_sha256": provider_context.sha256,
            },
            "provider_context": provider_context.model_dump(
                mode="json", by_alias=True
            ),
        }
        return PreparedBenchmarkExecution(
            request=request, canonical_case_ref=canonical_case_ref, job=job,
            command=command, trace_id=trace_id, run_id=run_id,
            batch_id=batch_id, authorized_tools=authorized_tools,
            material=process_material,
        )

    def complete_process_execution(
        self, request: GenericBenchmarkExecutionRequest,
        payload: Mapping[str, object],
    ) -> GenericBenchmarkExecutionEvidence:
        """Admit only process evidence backed by durable broker readbacks."""

        canonical_case_ref, _view = self._private_cases.resolve_execution_transport(
            request.case_ref, authority_id=self._executor_authority_id,
        )
        job = self._store.factory_job(request.job_id).job
        trace_id = uuid5(self._executor_authority_id, f"trace:{request.request_nonce}")
        run_id = uuid5(self._executor_authority_id, f"run:{request.request_nonce}")
        batch_id = f"bench-{request.job_id.hex[:12]}-{request.request_nonce.hex[:12]}"
        command_id = uuid5(self._executor_authority_id, f"command:{request.request_nonce}")
        operation = self._store.runtime_operation(command_id)
        command = operation.command
        tools = request.execution_case.declared_tool_keys
        expected_keys = {
            "schema", "observations", "rationale", "messages", "handoffs",
            "tool_result_refs", "latency_ms", "candidate_ref",
            "execution_ref", "agent_names",
        }
        try:
            if (
                set(payload) != expected_keys
                or payload.get("schema")
                != "captain.capability-benchmark-process-result.v1"
                or command.event_id != command_id
                or command.payload.batch_id != batch_id
                or not isinstance(payload["observations"], dict)
                or not isinstance(payload["rationale"], str)
                or not isinstance(payload["messages"], list)
                or not isinstance(payload["handoffs"], list)
                or not isinstance(payload["tool_result_refs"], dict)
            ):
                raise ValueError("shape")
            candidate_ref = ArtifactRef.model_validate(payload["candidate_ref"])
            execution_ref = ArtifactRef.model_validate(payload["execution_ref"])
            expected_execution_ref = (
                request.execution_plan.team_manifest_ref
                if request.team_kind == "team"
                else request.execution_plan.baseline_policy_ref
            )
            agent_names = tuple(str(item) for item in payload["agent_names"])  # type: ignore[union-attr]
            expected_agents = (
                request.execution_plan.team_agent_names
                if request.team_kind == "team"
                else (request.execution_plan.baseline_agent_name,)
            )
            tool_result_refs = {
                str(key): ArtifactRef.model_validate(value)
                for key, value in payload["tool_result_refs"].items()  # type: ignore[union-attr]
            }
            if (
                candidate_ref != request.execution_plan.candidate_ref
                or execution_ref != expected_execution_ref
                or agent_names != expected_agents
                or set(tool_result_refs) != set(tools)
            ):
                raise ValueError("binding")
            latency_ms = int(payload["latency_ms"])
            if latency_ms < 1:
                raise ValueError("metrics")
        except (AttributeError, KeyError, TypeError, ValueError):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark process result changed authority"
            ) from None

        cost_usd = self._settled_provider_cost(
            request=request, job=job, command_id=command_id,
            trace_id=trace_id, run_id=run_id, command=command,
        )
        if self._store.finalize_capability_benchmark_provider_cost(
            command_id
        ) != cost_usd:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark provider cost readback changed authority"
            )

        tool_calls: list[GenericToolCallEvidence] = []
        for tool_key, result_ref in tool_result_refs.items():
            call_id = f"call-{command_id}-{tool_key}"
            lease_id = f"lease-{command_id}-{tool_key}"
            authorization = self._tool_issuer.authorize(
                job_id=request.job_id, case_ref=canonical_case_ref,
                tool_key=tool_key,
                operation=self._tool_registry.operation_for(tool_key),
                call_id=call_id, lease_id=lease_id, trace_id=trace_id,
                run_id=run_id, model_policy_digest=request.model_policy_digest,
                batch_id=batch_id, command_id=command_id,
                candidate_ref=request.execution_plan.candidate_ref,
                execution_ref=execution_ref,
            )
            stored = self._store.capability_tool_effect(
                authorization.call_ref.sha256
            )
            if (
                not isinstance(stored, dict)
                or stored.get("result_ref") != result_ref.model_dump(mode="json")
            ):
                raise CapabilityLiveAuthorityUnavailable(
                    "benchmark process tool readback is unavailable"
                )
            receipt_ref = ArtifactRef.model_validate(stored.get("receipt_ref"))
            tool_calls.append(GenericToolCallEvidence(
                tool_key=tool_key, call_ref=authorization.call_ref,
                lease_ref=authorization.lease_ref, receipt_ref=receipt_ref,
            ))

        output_ref = self._put({
            **dict(payload),
            "schema": "captain.capability-benchmark-runtime-output.v1",
            "command_id": str(command_id),
        })
        result = AgentRuntimeResult(
            schema="captain.agent-runtime-result.v1",
            event_id=uuid5(command_id, "isolated-host-autogen-result"),
            command_id=command_id, correlation_id=job.correlation_id,
            occurred_at=self._clock(), producer="agent-runtime",
            subject_id=command.subject_id, subject_version=job.subject_version,
            grant_id=f"benchmark-{command_id}",
            operation=RuntimeOperation.AUTOGEN_BENCHMARK,
            status=RuntimeStatus.SUCCEEDED,
            session_id=f"isolated-benchmark-{command_id}",
            artifact_refs=(output_ref, *tool_result_refs.values()),
            evidence_refs=tuple(item.receipt_ref for item in tool_calls),
        )
        self._store.record_runtime_result(result)
        readback = self._store.runtime_operation(command_id)
        if readback.command != command or readback.result != result:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark process Runtime readback changed authority"
            )
        common = {
            "case_digest": request.case_ref.case_digest,
            "trace_id": str(trace_id), "run_id": str(run_id),
            "model_policy_digest": request.model_policy_digest,
        }
        outcome_ref = self._put({
            "schema": "captain.capability-benchmark-outcome.v1", **common,
            "observations": payload["observations"],
        })
        rationale_ref = self._put({
            "schema": "captain.capability-benchmark-rationale.v1", **common,
            "text": payload["rationale"],
        })
        message_refs = tuple(self._put({
            "schema": "captain.capability-benchmark-message.v1", **common,
            "message_key": str(item["message_key"]),
            "content": str(item["content"]),
        }) for item in payload["messages"])  # type: ignore[union-attr]
        handoff_refs = tuple(self._put({
            "schema": "captain.capability-benchmark-handoff.v1", **common,
            "from_agent": str(item["from_agent"]),
            "to_agent": str(item["to_agent"]),
            "reason_code": str(item["reason_code"]),
        }) for item in payload["handoffs"])  # type: ignore[union-attr]
        return GenericBenchmarkExecutionEvidence(
            case_ref=request.case_ref, team_kind=request.team_kind,
            agent_count=len(agent_names), model_id=request.model_id,
            model_policy_digest=request.model_policy_digest,
            input_ref=request.input_ref, capability_id=request.capability_id,
            outcome_ref=outcome_ref, message_event_refs=message_refs,
            handoff_event_refs=handoff_refs, tool_calls=tuple(tool_calls),
            rationale_ref=rationale_ref, cost_usd=cost_usd,
            latency_ms=latency_ms, trace_id=trace_id, run_id=run_id,
            request_nonce=request.request_nonce,
            request_digest=request.request_digest,
        )

    def _required_provider_binding(self) -> dict[str, object]:
        required = {
            "provider_proxy_url", "provider_policy_sha256",
            "provider_price_card_sha256", "provider_context_sha256",
            "provider_proxy_executable_sha256", "provider_proxy_code_sha256",
            "provider_paid_command_sha256", "provider_hard_ceiling_enforced",
        }
        binding = self._provider_binding
        if (
            binding is None or set(binding) != required
            or binding.get("provider_hard_ceiling_enforced") is not True
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark provider authority is unavailable"
            )
        return dict(binding)

    def _settled_provider_cost(
        self, *, request: GenericBenchmarkExecutionRequest, job: object,
        command_id: UUID, trace_id: UUID, run_id: UUID,
        command: AgentRuntimeCommand,
    ) -> Decimal:
        binding = self._required_provider_binding()
        settlements = tuple(
            self._store.capability_benchmark_provider_settlements(command_id)
        )
        if not settlements:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark provider settlement is unavailable"
            )
        if len(settlements) > 30:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark provider settlement authority changed"
            )
        expected = {
            "job_id": str(request.job_id),
            "correlation_id": str(job.correlation_id),
            "case_digest": request.case_ref.case_digest,
            "trace_id": str(trace_id), "run_id": str(run_id),
            "command_id": str(command_id), "provider": "openai",
            "model": request.model_id, **binding,
        }
        expected["provider_context_sha256"] = (
            command.payload.provider_context_sha256
        )
        expected["provider_result_id"] = str(command.payload.provider_result_id)
        expected["provider_session_id"] = command.payload.provider_session_id
        call_ids: set[str] = set()
        total = Decimal("0")
        required = {
            "schema", "settlement_id", "provider_call_id", "job_id",
            "correlation_id", "case_digest", "trace_id", "run_id",
            "command_id", "provider_result_id", "provider_session_id",
            "provider", "model", "input_units", "cached_input_units",
            "output_units", "actual_cost_usd", *binding,
        }
        for value in settlements:
            try:
                cost = Decimal(str(value["actual_cost_usd"]))
                call_id = str(value["provider_call_id"])
                result_id = str(value["provider_result_id"])
                if (
                    set(value) != required
                    or value.get("schema")
                    != "captain.capability-benchmark-provider-settlement.v1"
                    or any(value.get(key) != item for key, item in expected.items())
                    or not call_id or not result_id
                    or call_id in call_ids
                    or int(value["input_units"]) < 0
                    or int(value["cached_input_units"]) < 0
                    or int(value["output_units"]) < 0
                    or int(value["cached_input_units"]) > int(value["input_units"])
                    or not cost.is_finite() or cost < 0
                ):
                    raise ValueError("binding")
            except (KeyError, TypeError, ValueError):
                raise CapabilityLiveAuthorityUnavailable(
                    "benchmark provider settlement authority changed"
                ) from None
            call_ids.add(call_id)
            total += cost
        if total > job.execution_policy.max_cost_usd:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark provider settlement exceeds Factory budget"
            )
        return total

    async def execute(
        self, request: GenericBenchmarkExecutionRequest,
    ) -> GenericBenchmarkExecutionEvidence:
        if self._runtime_effect is None:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark execution belongs to the isolated process"
            )
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
            "candidate_manifest_ref": request.execution_plan.candidate_manifest_ref.model_dump(mode="json"),
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


def _factory_execution_policy_sha256(job: object) -> str:
    payload = job.execution_policy.model_dump(mode="json", by_alias=True)
    rendered = format(job.execution_policy.max_cost_usd, "f")
    payload["max_cost_usd"] = (
        rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    )
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


__all__ = [
    "BenchmarkRuntimeEffectPort",
    "BenchmarkRuntimeEffectResult",
    "DurableBenchmarkToolResult",
    "GatewayDurableBenchmarkToolBroker",
    "GatewayBenchmarkExecutionService",
    "GatewayRuntimeBenchmarkEffect",
]
