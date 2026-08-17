"""GatewayStore implementation of the authenticated Factory runtime RPC."""

from __future__ import annotations

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
from gateway.factory_process_rpc import (
    FactoryGatewayRpcCommand,
    factory_gateway_request_sha256,
)
from gateway.store import GatewayStore


class GatewayStoreFactoryRpcAuthority:
    """Execute only the exact repository operations required by Factory V3."""

    def __init__(self, *, store: GatewayStore, forge: object | None = None,
                 n8n: object | None = None) -> None:
        self._store = store
        self._forge = forge
        self._n8n = n8n

    def read_receipt(
        self, command: FactoryGatewayRpcCommand,
    ) -> dict[str, object] | None:
        return self._store.factory_runtime_rpc_receipt(
            command.idempotency_key,
            request_id=command.request_id,
            job_id=command.job_id,
            correlation_id=command.correlation_id,
            operation=command.operation,
            request_sha256=factory_gateway_request_sha256(command),
        )

    def execute(self, command: FactoryGatewayRpcCommand) -> dict[str, object]:
        self._require_current_scope(command)
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
        request_sha256 = factory_gateway_request_sha256(command)
        return self._store.record_factory_runtime_rpc_receipt(
            idempotency_key=command.idempotency_key,
            request_id=command.request_id,
            job_id=command.job_id,
            correlation_id=command.correlation_id,
            operation=command.operation,
            request_sha256=request_sha256,
            response=response,
        )

    def _require_current_scope(self, command: FactoryGatewayRpcCommand) -> None:
        if command.operation == "record_factory_job":
            job = parse_factory_job(_mapping(command.payload, "job"))
            if (
                job.job_id != command.job_id
                or job.correlation_id != command.correlation_id
                or job.required_capability != command.input_id
                or command.attempt != 1
            ):
                raise ValueError("Factory Gateway authority binding changed")
            return
        projection = self._store.factory_job(command.job_id)
        job = projection.job
        if (
            job.correlation_id != command.correlation_id
            or job.required_capability != command.input_id
            or projection.projection.attempt != command.attempt
        ):
            raise ValueError("Factory Gateway authority binding changed")

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
            self._require_nested_resource(command, block)
            return self._store.record_factory_block(block)
        if operation == "record_factory_lease":
            lease = FactoryLease.model_validate(_mapping(payload, "lease"))
            _require_job(command, lease.job_id, lease.correlation_id)
            self._require_nested_resource(command, lease)
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
            self._require_nested_resource(command, artifact)
            return self._store.record_factory_workflow_artifact(artifact)
        if operation == "factory_budget":
            return self._store.factory_budget(command.job_id)
        if operation == "runtime_operation":
            value = self._store.runtime_operation(UUID(str(payload["command_id"])))
            job = self._store.factory_job(command.job_id).job
            runtime_command = getattr(value, "command", None)
            result = getattr(value, "result", None)
            if (
                value is None
                or getattr(runtime_command, "correlation_id", None)
                != command.correlation_id
                or getattr(runtime_command, "subject_id", None)
                != job.required_capability
                or (
                    result is not None
                    and (
                        getattr(result, "correlation_id", None)
                        != command.correlation_id
                        or getattr(result, "subject_id", None)
                        != job.required_capability
                    )
                )
            ):
                raise ValueError("Factory Gateway authority binding changed")
            return value
        if operation == "business_benchmark_summary_by_artifact":
            reference = ArtifactRef.model_validate(_mapping(payload, "artifact_ref"))
            if reference not in self._job_artifact_refs(command.job_id):
                raise ValueError("Factory Gateway authority binding changed")
            return self._store.business_benchmark_summary_by_artifact(reference)
        if operation == "generic_benchmark_quality_evidence":
            return self._store.generic_benchmark_quality_evidence(
                command.job_id, command.attempt
            )
        if operation == "bundle":
            batch_id = payload.get("batch_id")
            if not isinstance(batch_id, str) or not batch_id:
                raise ValueError("Factory WorkBatch binding is invalid")
            if self._store.capability_n8n_work_batch(command.job_id) != batch_id:
                raise ValueError("Factory Gateway authority binding changed")
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
            persisted = self._store.factory_job(command.job_id).job
            if dispatch.job != persisted or dispatch.action.attempt != command.attempt:
                raise ValueError("Factory Gateway authority binding changed")
            if dispatch.action.job_id not in {None, command.job_id}:
                raise ValueError("Factory Gateway authority binding changed")
            if dispatch.lease is not None:
                self._require_nested_resource(command, dispatch.lease)
            return asyncio.run(self._forge.submit(dispatch))
        if operation in {"forge_status", "forge_result"}:
            if self._forge is None:
                raise ValueError("Factory Forge authority is unavailable")
            creation_job_id = UUID(str(payload["creation_job_id"]))
            if creation_job_id not in self._job_creation_ids(command.job_id):
                raise ValueError("Factory Gateway authority binding changed")
            method = getattr(self._forge, operation.removeprefix("forge_"))
            return asyncio.run(method(creation_job_id))
        raise ValueError("Factory Gateway operation is unavailable")

    def _require_nested_resource(
        self, command: FactoryGatewayRpcCommand, resource: object,
    ) -> None:
        projection = self._store.factory_job(command.job_id)
        job = projection.job
        if (
            getattr(resource, "job_id", None) != command.job_id
            or getattr(resource, "correlation_id", None) != command.correlation_id
            or getattr(resource, "subject_version", None) != job.subject_version
            or getattr(resource, "attempt", None) != command.attempt
        ):
            raise ValueError("Factory Gateway authority binding changed")
        occurred_at = getattr(resource, "occurred_at", None)
        deadline_at = getattr(job, "deadline_at", None)
        if occurred_at is not None and (
            occurred_at < job.occurred_at
            or (deadline_at is not None and occurred_at >= deadline_at)
        ):
            raise ValueError("Factory Gateway authority binding changed")
        issued_at = getattr(resource, "issued_at", None)
        expires_at = getattr(resource, "expires_at", None)
        if issued_at is not None and (
            issued_at < job.occurred_at
            or expires_at is None
            or (deadline_at is not None and expires_at > deadline_at)
        ):
            raise ValueError("Factory Gateway authority binding changed")

    def _job_artifact_refs(self, job_id: UUID) -> tuple[ArtifactRef, ...]:
        projection = self._store.factory_job(job_id)
        references: list[ArtifactRef] = []
        for block in projection.blocks:
            references.extend((*block.artifact_refs, *block.evidence_refs))
        for artifact in self._store.factory_workflow_artifacts(job_id):
            reference = getattr(artifact, "artifact_ref", None)
            if isinstance(reference, ArtifactRef):
                references.append(reference)
            references.extend(
                item
                for item in getattr(artifact, "evidence_refs", ())
                if isinstance(item, ArtifactRef)
            )
        return tuple(dict.fromkeys(references))

    def _job_creation_ids(self, job_id: UUID) -> tuple[UUID, ...]:
        identities: list[UUID] = []
        for artifact in self._store.factory_workflow_artifacts(job_id):
            for owner_name in ("build_assignment", "build_receipt"):
                owner = getattr(artifact, owner_name, None)
                identity = getattr(owner, "creation_job_id", None)
                if isinstance(identity, UUID):
                    identities.append(identity)
        return tuple(dict.fromkeys(identities))


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
