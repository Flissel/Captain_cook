"""Authenticated, typed Factory-runtime to Gateway sole-writer boundary."""

from __future__ import annotations

import secrets
import json
import base64
import hashlib
import hmac
from typing import Literal, Protocol
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator


_FACTORY_STORE_OPERATIONS = frozenset(
    {
        "record_factory_job",
        "factory_job",
        "record_factory_block",
        "record_factory_lease",
        "factory_skill_assignment",
        "factory_workflow_artifacts",
        "record_factory_workflow_artifact",
        "factory_budget",
        "runtime_operation",
        "business_benchmark_summary_by_artifact",
        "generic_benchmark_quality_evidence",
        "bundle",
        "active_n8n_work_batch_id",
        "forge_submit",
        "forge_status",
        "forge_result",
    }
)


class FactoryGatewayRpcCommand(BaseModel):
    """One exact Factory operation authorized against a Gateway revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["captain.factory-gateway-rpc-command.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    operation: str = Field(min_length=1, max_length=64)
    request_id: UUID
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    job_id: UUID
    correlation_id: UUID
    input_id: str = Field(min_length=1, max_length=255)
    attempt: int = Field(ge=1)
    expected_revision: int = Field(ge=0)
    gateway_capability: str = Field(min_length=32, max_length=4096)
    payload: dict[str, object]

    @model_validator(mode="after")
    def _bounded_payload(self) -> "FactoryGatewayRpcCommand":
        try:
            size = len(json.dumps(
                self.payload, sort_keys=True, separators=(",", ":")
            ).encode())
        except (TypeError, ValueError):
            raise ValueError("Factory Gateway payload is invalid") from None
        if size > 1_048_576:
            raise ValueError("Factory Gateway payload is too large")
        return self


class FactoryGatewayRpcResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["captain.factory-gateway-rpc-result.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    request_id: UUID
    job_id: UUID
    revision: int = Field(ge=0)
    result: dict[str, object]


class FactoryGatewayRpcAuthority(Protocol):
    def read_receipt(
        self, command: FactoryGatewayRpcCommand,
    ) -> dict[str, object] | None: ...

    def execute(self, command: FactoryGatewayRpcCommand) -> dict[str, object]: ...


class FactoryGatewayRpcService:
    """Validate envelope authority before invoking the durable Gateway adapter."""

    def __init__(
        self, *, authority: FactoryGatewayRpcAuthority, capability_key: str,
    ) -> None:
        if len(capability_key) < 24:
            raise ValueError("Factory Gateway capability authority is invalid")
        self._authority = authority
        self._capability_key = capability_key

    def execute(self, command: FactoryGatewayRpcCommand) -> FactoryGatewayRpcResult:
        if command.operation not in _FACTORY_STORE_OPERATIONS:
            raise ValueError("Factory Gateway operation is unavailable")
        _verify_factory_gateway_capability(
            command=command, capability_key=self._capability_key,
        )
        payload_job_id = command.payload.get("job_id")
        if payload_job_id is not None and str(payload_job_id) != str(command.job_id):
            raise ValueError("Factory Gateway job binding changed")
        replay = self._authority.read_receipt(command)
        if replay is not None:
            result = FactoryGatewayRpcResult.model_validate(replay)
            if result.request_id != command.request_id or result.job_id != command.job_id:
                raise ValueError("Factory Gateway replay binding changed")
            return result
        return FactoryGatewayRpcResult.model_validate(self._authority.execute(command))


def issue_factory_gateway_capability(
    *, capability_key: str, job_id: UUID | str, correlation_id: UUID | str,
    input_id: str, attempt: int, allowed_operations: tuple[str, ...],
) -> str:
    """Issue an opaque Gateway-only signed capability for one Factory scope."""

    if len(capability_key) < 24 or not input_id or attempt < 1:
        raise ValueError("Factory Gateway capability authority is invalid")
    operations = tuple(sorted(set(allowed_operations)))
    if not operations or any(item not in _FACTORY_STORE_OPERATIONS for item in operations):
        raise ValueError("Factory Gateway capability operation is invalid")
    payload = json.dumps(
        {
            "schema": "captain.factory-gateway-capability.v1",
            "job_id": str(UUID(str(job_id))),
            "correlation_id": str(UUID(str(correlation_id))),
            "input_id": input_id,
            "attempt": attempt,
            "allowed_operations": operations,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(
        capability_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256,
    ).hexdigest()
    return encoded + "." + signature


def factory_gateway_request_sha256(command: FactoryGatewayRpcCommand) -> str:
    """Digest the canonical complete request, including revision and payload."""

    return hashlib.sha256(
        command.model_dump_json(by_alias=True).encode("utf-8")
    ).hexdigest()


def _verify_factory_gateway_capability(
    *, command: FactoryGatewayRpcCommand, capability_key: str,
) -> None:
    try:
        encoded, signature = command.gateway_capability.rsplit(".", 1)
        expected = hmac.new(
            capability_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256,
        ).hexdigest()
        if not secrets.compare_digest(signature, expected):
            raise ValueError
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
    except Exception:
        raise ValueError("Factory Gateway capability is invalid") from None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "captain.factory-gateway-capability.v1"
        or payload.get("job_id") != str(command.job_id)
        or payload.get("correlation_id") != str(command.correlation_id)
        or payload.get("input_id") != command.input_id
        or payload.get("attempt") != command.attempt
        or not isinstance(payload.get("allowed_operations"), list)
        or command.operation not in payload["allowed_operations"]
    ):
        raise ValueError("Factory Gateway capability binding changed")


def create_factory_gateway_rpc_router(
    *,
    service: FactoryGatewayRpcService,
    factory_runtime_token: str,
) -> FastAPI:
    """Create the narrow Gateway-side API without exposing other roles."""

    if len(factory_runtime_token) < 16:
        raise ValueError("Factory runtime credential is invalid")
    app = FastAPI()

    @app.post("/v1/factory-runtime/store")
    def execute_factory_store_operation(
        command: FactoryGatewayRpcCommand,
        authorization: str | None = Header(default=None),
    ) -> FactoryGatewayRpcResult:
        if authorization is None or not secrets.compare_digest(
            authorization, "Bearer " + factory_runtime_token
        ):
            raise HTTPException(
                status_code=401, detail="Factory runtime authentication failed"
            )
        try:
            return service.execute(command)
        except Exception:
            raise HTTPException(
                status_code=409, detail="Factory Gateway operation is unavailable"
            ) from None

    return app


__all__ = [
    "FactoryGatewayRpcCommand",
    "FactoryGatewayRpcResult",
    "FactoryGatewayRpcService",
    "create_factory_gateway_rpc_router",
    "factory_gateway_request_sha256",
    "issue_factory_gateway_capability",
]
