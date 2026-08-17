"""Factory-child client for the narrow Gateway sole-writer API."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Iterator
from urllib import request as urllib_request
from uuid import NAMESPACE_URL, UUID, uuid5

from agenten.agent_factory.contracts import (
    FactoryEvidenceBlock,
    FactoryLease,
    GenericBenchmarkQualityEvidenceV1,
)
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.contracts import (
    FactoryJobProjection,
    FactorySkillAssignmentV1,
    FactoryWorkflowArtifactWriteReceipt,
    FactoryWriteReceipt,
    RuntimeOperationProjection,
    parse_factory_workflow_artifact,
)
from gateway.factory_process_rpc import (
    FactoryGatewayRpcCommand,
    FactoryGatewayRpcResult,
)
from agenten.agent_factory.forge_contracts import (
    CreationProgressV1, CreationResultV1, CreationSubmissionReceipt,
)


@dataclass
class _FactoryRpcContext:
    job_id: UUID
    correlation_id: UUID
    input_id: str
    attempt: int
    revision: int


class GatewayFactoryStoreRpcClient:
    """GatewayStore-shaped adapter with no database or administrator credential."""

    def __init__(
        self,
        *,
        gateway_url: str,
        factory_runtime_token: str,
        send: Callable[[dict[str, object]], dict[str, object]] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if (
            gateway_url.rstrip("/") != gateway_url
            or not gateway_url.startswith("http://127.0.0.1:")
            or len(factory_runtime_token) < 16
            or timeout_seconds <= 0
        ):
            raise ValueError("Factory Gateway RPC configuration is invalid")
        self._url = gateway_url + "/v1/factory-runtime/store"
        self._token = factory_runtime_token
        self._timeout = timeout_seconds
        self._send_override = send
        self._context: ContextVar[_FactoryRpcContext | None] = ContextVar(
            "factory_gateway_rpc_context", default=None
        )

    @contextmanager
    def bind(
        self, *, job_id: UUID, correlation_id: UUID, input_id: str,
        attempt: int, expected_revision: int,
    ) -> Iterator[None]:
        token = self._context.set(_FactoryRpcContext(
            job_id=job_id, correlation_id=correlation_id, input_id=input_id,
            attempt=attempt, revision=expected_revision,
        ))
        try:
            yield
        finally:
            self._context.reset(token)

    def _call(self, operation: str, payload: dict[str, object]) -> object:
        context = self._context.get()
        if context is None:
            raise RuntimeError("Factory Gateway RPC context is absent")
        canonical_payload = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        )
        identity = "|".join((
            str(context.job_id), str(context.correlation_id), context.input_id,
            str(context.attempt), operation, canonical_payload,
        ))
        idempotency_key = hashlib.sha256(identity.encode()).hexdigest()
        command = FactoryGatewayRpcCommand(
            schema="captain.factory-gateway-rpc-command.v1",
            operation=operation,
            request_id=uuid5(NAMESPACE_URL, "factory-rpc|" + idempotency_key),
            idempotency_key=idempotency_key,
            job_id=context.job_id,
            correlation_id=context.correlation_id,
            input_id=context.input_id,
            attempt=context.attempt,
            expected_revision=context.revision,
            payload=payload,
        )
        raw = (
            self._send_override(command.model_dump(mode="json", by_alias=True))
            if self._send_override is not None
            else self._send_http(command)
        )
        response = FactoryGatewayRpcResult.model_validate(raw)
        if response.request_id != command.request_id or response.job_id != context.job_id:
            raise RuntimeError("Factory Gateway RPC response binding changed")
        context.revision = response.revision
        return response.result["value"]

    def _send_http(self, command: FactoryGatewayRpcCommand) -> dict[str, object]:
        body = command.model_dump_json(by_alias=True).encode()
        outbound = urllib_request.Request(
            self._url, data=body, method="POST",
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib_request.urlopen(outbound, timeout=self._timeout) as response:
                value = json.loads(response.read())
        except Exception:
            raise RuntimeError("Factory Gateway RPC is unavailable") from None
        if not isinstance(value, dict):
            raise RuntimeError("Factory Gateway RPC response is invalid")
        return value

    def record_factory_job(self, job):
        return self._call("record_factory_job", {"job_id": str(job.job_id), "job": job.model_dump(mode="json", by_alias=True)})

    def factory_job(self, job_id: UUID) -> FactoryJobProjection:
        return FactoryJobProjection.model_validate(self._call("factory_job", {"job_id": str(job_id)}))

    def record_factory_block(self, block: FactoryEvidenceBlock, **_kwargs) -> FactoryWriteReceipt:
        return FactoryWriteReceipt.model_validate(self._call("record_factory_block", {"job_id": str(block.job_id), "block": block.model_dump(mode="json", by_alias=True)}))

    def record_factory_lease(self, lease: FactoryLease) -> FactoryWriteReceipt:
        return FactoryWriteReceipt.model_validate(self._call("record_factory_lease", {"job_id": str(lease.job_id), "lease": lease.model_dump(mode="json", by_alias=True)}))

    def factory_skill_assignment(self, job_id: UUID, step: FactorySkillStep) -> FactorySkillAssignmentV1:
        return FactorySkillAssignmentV1.model_validate(self._call("factory_skill_assignment", {"job_id": str(job_id), "step": step.value}))

    def factory_workflow_artifacts(self, job_id: UUID):
        value = self._call("factory_workflow_artifacts", {"job_id": str(job_id)})
        if not isinstance(value, list):
            raise RuntimeError("Factory artifact response is invalid")
        return tuple(parse_factory_workflow_artifact(item) for item in value)

    def record_factory_workflow_artifact(self, artifact) -> FactoryWorkflowArtifactWriteReceipt:
        return FactoryWorkflowArtifactWriteReceipt.model_validate(self._call("record_factory_workflow_artifact", {"job_id": str(artifact.job_id), "artifact": artifact.model_dump(mode="json", by_alias=True)}))

    def factory_budget(self, job_id: UUID) -> FactoryBudgetProjection:
        return FactoryBudgetProjection.model_validate(self._call("factory_budget", {"job_id": str(job_id)}))

    def runtime_operation(self, command_id: UUID):
        value = self._call("runtime_operation", {"command_id": str(command_id)})
        return None if value is None else RuntimeOperationProjection.model_validate(value)

    def business_benchmark_summary_by_artifact(self, artifact_ref: ArtifactRef):
        from agenten.agent_factory.business_benchmark_contracts import BusinessBenchmarkSummaryV1
        value = self._call("business_benchmark_summary_by_artifact", {"artifact_ref": artifact_ref.model_dump(mode="json")})
        return None if value is None else BusinessBenchmarkSummaryV1.model_validate(value)

    def generic_benchmark_quality_evidence(self, job_id: UUID, attempt: int):
        value = self._call("generic_benchmark_quality_evidence", {"job_id": str(job_id), "attempt": attempt})
        return None if value is None else GenericBenchmarkQualityEvidenceV1.model_validate(value)

    def bundle(self, batch_id: str) -> dict[str, object]:
        value = self._call("bundle", {"batch_id": batch_id})
        if not isinstance(value, dict):
            raise RuntimeError("Factory WorkBatch response is invalid")
        return value

    def active_n8n_work_batch_id(self, job_id: UUID) -> str | None:
        value = self._call("active_n8n_work_batch_id", {"job_id": str(job_id)})
        if value is not None and not isinstance(value, str):
            raise RuntimeError("Factory n8n response is invalid")
        return value

    def capability_n8n_work_batch(self, job_id: UUID) -> str | None:
        return self.active_n8n_work_batch_id(job_id)


class GatewayFactoryForgeRpcClient:
    """Forge control port; the child never receives the Minibook credential."""

    def __init__(self, *, gateway: GatewayFactoryStoreRpcClient) -> None:
        self._gateway = gateway

    async def submit(self, dispatch):
        payload = {
            "job": dispatch.job.model_dump(mode="json", by_alias=True),
            "action": dispatch.action.model_dump(mode="json"),
            "role": None if dispatch.role is None else dispatch.role.value,
            "lease": None if dispatch.lease is None else dispatch.lease.model_dump(mode="json", by_alias=True),
        }
        value = self._gateway._call("forge_submit", {"job_id": str(dispatch.job.job_id), "dispatch": payload})
        try:
            return CreationSubmissionReceipt.model_validate(value)
        except ValueError:
            return CreationResultV1.model_validate(value)

    async def status(self, creation_job_id: UUID) -> CreationProgressV1:
        return CreationProgressV1.model_validate(self._gateway._call("forge_status", {"creation_job_id": str(creation_job_id)}))

    async def result(self, creation_job_id: UUID) -> CreationResultV1:
        return CreationResultV1.model_validate(self._gateway._call("forge_result", {"creation_job_id": str(creation_job_id)}))

    async def wait_for_result(self, creation_job_id: UUID) -> CreationResultV1:
        for _ in range(120):
            progress = await self.status(creation_job_id)
            if progress.status in {"succeeded", "failed", "blocked", "cancelled"}:
                return await self.result(creation_job_id)
        raise RuntimeError("Factory Forge result is unavailable")


__all__ = ["GatewayFactoryForgeRpcClient", "GatewayFactoryStoreRpcClient"]
