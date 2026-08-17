"""Authenticated, typed Factory-runtime to Gateway sole-writer boundary."""

from __future__ import annotations

import secrets
from typing import Literal, Protocol
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field


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
    payload: dict[str, object]


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
    def read_receipt(self, idempotency_key: str) -> dict[str, object] | None: ...

    def execute(self, command: FactoryGatewayRpcCommand) -> dict[str, object]: ...


class FactoryGatewayRpcService:
    """Validate envelope authority before invoking the durable Gateway adapter."""

    def __init__(self, *, authority: FactoryGatewayRpcAuthority) -> None:
        self._authority = authority

    def execute(self, command: FactoryGatewayRpcCommand) -> FactoryGatewayRpcResult:
        if command.operation not in _FACTORY_STORE_OPERATIONS:
            raise ValueError("Factory Gateway operation is unavailable")
        payload_job_id = command.payload.get("job_id")
        if payload_job_id is not None and str(payload_job_id) != str(command.job_id):
            raise ValueError("Factory Gateway job binding changed")
        replay = self._authority.read_receipt(command.idempotency_key)
        if replay is not None:
            result = FactoryGatewayRpcResult.model_validate(replay)
            if result.request_id != command.request_id or result.job_id != command.job_id:
                raise ValueError("Factory Gateway replay binding changed")
            return result
        return FactoryGatewayRpcResult.model_validate(self._authority.execute(command))


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
]
