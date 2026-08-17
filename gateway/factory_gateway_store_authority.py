"""GatewayStore implementation of the authenticated Factory runtime RPC."""

from __future__ import annotations

import hashlib
import json
import asyncio
from typing import Any
from uuid import UUID

from agenten.agent_factory.contracts import (
    FactoryEvidenceBlock,
    FactoryLease,
    FactoryRole,
    parse_factory_job,
)
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.state_machine import FactoryAction
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.contracts import parse_factory_workflow_artifact
from gateway.factory_process_rpc import FactoryGatewayRpcCommand
from gateway.store import GatewayStore


class GatewayStoreFactoryRpcAuthority:
    """Execute only the exact repository operations required by Factory V3."""

    def __init__(self, *, store: GatewayStore, forge: object | None = None,
                 n8n: object | None = None) -> None:
        self._store = store
        self._forge = forge
        self._n8n = n8n

    def read_receipt(self, idempotency_key: str) -> dict[str, object] | None:
        return self._store.factory_runtime_rpc_receipt(idempotency_key)

    def execute(self, command: FactoryGatewayRpcCommand) -> dict[str, object]:
        current_revision = self._revision(command.job_id)
        if (
            command.expected_revision != current_revision
            and not self._effect_already_persisted(command)
        ):
            raise ValueError("Factory Gateway revision changed")
        value = self._dispatch(command)
        revision = self._revision(command.job_id)
        response: dict[str, object] = {
            "schema": "captain.factory-gateway-rpc-result.v1",
            "request_id": str(command.request_id),
            "job_id": str(command.job_id),
            "revision": revision,
            "result": {"value": _json_value(value)},
        }
        request_sha256 = hashlib.sha256(
            command.model_dump_json(by_alias=True).encode("utf-8")
        ).hexdigest()
        return self._store.record_factory_runtime_rpc_receipt(
            idempotency_key=command.idempotency_key,
            request_id=command.request_id,
            job_id=command.job_id,
            correlation_id=command.correlation_id,
            operation=command.operation,
            request_sha256=request_sha256,
            response=response,
        )

    def _effect_already_persisted(self, command: FactoryGatewayRpcCommand) -> bool:
        """Close effect-before-RPC-receipt crashes from authoritative readback."""

        try:
            projection = self._store.factory_job(command.job_id)
            if command.operation == "record_factory_job":
                return projection.job == parse_factory_job(
                    _mapping(command.payload, "job")
                )
            if command.operation == "record_factory_block":
                return FactoryEvidenceBlock.model_validate(
                    _mapping(command.payload, "block")
                ) in projection.blocks
            if command.operation == "record_factory_lease":
                return FactoryLease.model_validate(
                    _mapping(command.payload, "lease")
                ) in projection.leases
            if command.operation == "record_factory_workflow_artifact":
                artifact = parse_factory_workflow_artifact(
                    _mapping(command.payload, "artifact")
                )
                return artifact in self._store.factory_workflow_artifacts(
                    command.job_id
                )
        except Exception:
            return False
        return False

    def _revision(self, job_id) -> int:
        try:
            projection = self._store.factory_job(job_id)
        except Exception:
            return 0
        return (
            1
            + len(projection.blocks)
            + len(projection.leases)
            + len(self._store.factory_workflow_artifacts(job_id))
        )

    def _dispatch(self, command: FactoryGatewayRpcCommand) -> object:
        payload = command.payload
        operation = command.operation
        if operation == "record_factory_job":
            job = parse_factory_job(_mapping(payload, "job"))
            _require_job(command, job.job_id, job.correlation_id)
            return self._store.record_factory_job(job)
        if operation == "factory_job":
            return self._store.factory_job(command.job_id)
        if operation == "record_factory_block":
            block = FactoryEvidenceBlock.model_validate(_mapping(payload, "block"))
            _require_job(command, block.job_id, block.correlation_id)
            return self._store.record_factory_block(block)
        if operation == "record_factory_lease":
            lease = FactoryLease.model_validate(_mapping(payload, "lease"))
            _require_job(command, lease.job_id, lease.correlation_id)
            return self._store.record_factory_lease(lease)
        if operation == "factory_skill_assignment":
            return self._store.factory_skill_assignment(
                command.job_id, FactorySkillStep(str(payload["step"]))
            )
        if operation == "factory_workflow_artifacts":
            return self._store.factory_workflow_artifacts(command.job_id)
        if operation == "record_factory_workflow_artifact":
            artifact = parse_factory_workflow_artifact(_mapping(payload, "artifact"))
            _require_job(command, artifact.job_id, artifact.correlation_id)
            return self._store.record_factory_workflow_artifact(artifact)
        if operation == "factory_budget":
            return self._store.factory_budget(command.job_id)
        if operation == "runtime_operation":
            return self._store.runtime_operation(UUID(str(payload["command_id"])))
        if operation == "business_benchmark_summary_by_artifact":
            return self._store.business_benchmark_summary_by_artifact(
                ArtifactRef.model_validate(_mapping(payload, "artifact_ref"))
            )
        if operation == "generic_benchmark_quality_evidence":
            return self._store.generic_benchmark_quality_evidence(
                command.job_id, command.attempt
            )
        if operation == "bundle":
            batch_id = payload.get("batch_id")
            if not isinstance(batch_id, str) or not batch_id:
                raise ValueError("Factory WorkBatch binding is invalid")
            return self._store.bundle(batch_id)
        if operation == "active_n8n_work_batch_id":
            if self._n8n is None:
                raise ValueError("Factory n8n authority is unavailable")
            return self._n8n.active_n8n_work_batch_id(command.job_id)
        if operation == "forge_submit":
            if self._forge is None:
                raise ValueError("Factory Forge authority is unavailable")
            dispatch = _factory_dispatch(_mapping(payload, "dispatch"))
            _require_job(command, dispatch.job.job_id, dispatch.job.correlation_id)
            return asyncio.run(self._forge.submit(dispatch))
        if operation in {"forge_status", "forge_result"}:
            if self._forge is None:
                raise ValueError("Factory Forge authority is unavailable")
            creation_job_id = UUID(str(payload["creation_job_id"]))
            method = getattr(self._forge, operation.removeprefix("forge_"))
            return asyncio.run(method(creation_job_id))
        raise ValueError("Factory Gateway operation is unavailable")


def _mapping(payload: dict[str, object], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError("Factory Gateway payload is invalid")
    return value


def _require_job(command, job_id, correlation_id) -> None:
    if job_id != command.job_id or correlation_id != command.correlation_id:
        raise ValueError("Factory Gateway authority binding changed")


def _factory_dispatch(payload: dict[str, Any]) -> FactoryDispatch:
    role = payload.get("role")
    lease = payload.get("lease")
    return FactoryDispatch(
        job=parse_factory_job(_mapping(payload, "job")),
        action=FactoryAction.model_validate(_mapping(payload, "action")),
        role=None if role is None else FactoryRole(str(role)),
        lease=None if lease is None else FactoryLease.model_validate(lease),
    )


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)  # type: ignore[union-attr]
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return json.loads(json.dumps(value, default=str))


__all__ = ["GatewayStoreFactoryRpcAuthority"]
