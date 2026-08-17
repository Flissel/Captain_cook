"""Authenticated isolated process for the verified Factory composition."""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.state_machine import FactoryActionKind
from gateway.capability_assembly_bootstrap import (
    materialize_capability_assembly_factory_runtime,
    plan_capability_assembly_factory_runtime_from_environment,
)
from gateway.factory_gateway_store_client import GatewayFactoryStoreRpcClient
from agenten.agent_runtime.production_bootstrap import read_verified_repository_bytes


class FactoryProcessExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_name: Literal["captain.factory-process-execution.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    job_id: UUID
    correlation_id: UUID
    input_id: str = Field(min_length=1, max_length=255)
    attempt: int = Field(ge=1, le=5)
    operation: Literal["run"]
    request_id: UUID
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=0)
    maximum_dispatches: int = Field(default=12, ge=1, le=32)
    stop_before_action: FactoryActionKind | None = None


class FactoryProcessExecutor:
    def __init__(self, *, composition: object,
                 gateway_store: GatewayFactoryStoreRpcClient) -> None:
        if not callable(getattr(composition, "run", None)):
            raise ValueError("Factory process composition is invalid")
        self._composition = composition
        self._gateway_store = gateway_store

    async def execute(self, request: FactoryProcessExecutionRequest) -> dict[str, object]:
        with self._gateway_store.bind(
            job_id=request.job_id, correlation_id=request.correlation_id,
            input_id=request.input_id, attempt=request.attempt,
            expected_revision=request.expected_revision,
        ):
            result = await self._composition.run(
                request.job_id,
                maximum_dispatches=request.maximum_dispatches,
                stop_before_action=request.stop_before_action,
            )
        dump = getattr(result, "model_dump", None)
        value = dump(mode="json", by_alias=True) if callable(dump) else {
            "status": str(getattr(result, "status", "unavailable")),
        }
        return {
            "schema": "captain.factory-process-result.v1",
            "request_id": str(request.request_id),
            "job_id": str(request.job_id),
            "result": value,
        }


def create_capability_factory_process_app(
    *, executor: FactoryProcessExecutor, factory_runtime_token: str,
) -> FastAPI:
    if len(factory_runtime_token) < 16:
        raise ValueError("Factory runtime credential is invalid")
    app = FastAPI()

    def require_token(authorization: str | None) -> None:
        if authorization is None or not secrets.compare_digest(
            authorization, "Bearer " + factory_runtime_token
        ):
            raise HTTPException(status_code=401, detail="Factory process authentication failed")

    @app.get("/healthz")
    def health(authorization: str | None = Header(default=None)) -> dict[str, str]:
        require_token(authorization)
        return {"status": "ready", "authority": "factory-runtime"}

    @app.post("/v1/execute")
    async def execute(
        request: FactoryProcessExecutionRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        require_token(authorization)
        try:
            return await executor.execute(request)
        except Exception:
            raise HTTPException(status_code=409, detail="Factory process execution is unavailable") from None

    return app


class _UnusedPrivateCatalog:
    def put(self, *_args, **_kwargs):
        raise RuntimeError("Factory child private catalog mutation is forbidden")
    def get(self, *_args, **_kwargs):
        raise RuntimeError("Factory child private catalog read is unavailable")


def create_capability_factory_process_app_from_environment(
    *, environment: Mapping[str, str] | None = None,
    repository_root: Path | None = None,
) -> FastAPI:
    env = dict(os.environ if environment is None else environment)
    root = (repository_root or Path.cwd()).resolve(strict=True)
    token = env.get("CAPTAIN_FACTORY_RUNTIME_TOKEN", "").strip()
    gateway_url = env.get("CAPTAIN_GATEWAY_URL", "").strip().rstrip("/")
    factory_url = env.get("CAPTAIN_FACTORY_RUNTIME_URL", "").strip().rstrip("/")
    parsed = urlsplit(factory_url)
    if (
        len(token) < 16 or not gateway_url.startswith("http://127.0.0.1:")
        or parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
        or parsed.port is None or parsed.path not in {"", "/"}
    ):
        raise RuntimeError("Factory process configuration is unavailable")
    code_digest = env.get("CAPTAIN_FACTORY_RUNTIME_CODE_SHA256", "").strip()
    try:
        read_verified_repository_bytes(
            "gateway/capability_factory_process_app.py",
            expected_sha256=code_digest,
            expected_repository_revision=env[
                "CAPTAIN_CAPABILITY_ASSEMBLY_REPOSITORY_REVISION"
            ],
            repository_root=root,
        )
    except Exception:
        raise RuntimeError("Factory process configuration is unavailable") from None
    artifact_root = (root / env["CAPTAIN_CAPABILITY_ARTIFACT_ROOT"]).resolve()
    authority_root = (root / env["CAPTAIN_CAPABILITY_AUTHORITY_ROOT"]).resolve()
    plan = plan_capability_assembly_factory_runtime_from_environment(
        repository_root=root, environment=env, artifact_root=artifact_root,
        authority_root=authority_root,
        repository_revision=env["CAPTAIN_CAPABILITY_ASSEMBLY_REPOSITORY_REVISION"],
    )
    store = GatewayFactoryStoreRpcClient(
        gateway_url=gateway_url, factory_runtime_token=token,
    )
    runtime = materialize_capability_assembly_factory_runtime(
        plan, store=store, private_benchmark_catalog=_UnusedPrivateCatalog(),
        clock=lambda: datetime.now(timezone.utc),
        use_factory_process_client=False,
    )
    return create_capability_factory_process_app(
        executor=FactoryProcessExecutor(composition=runtime.factory, gateway_store=store),
        factory_runtime_token=token,
    )


def main() -> None:
    import uvicorn
    env = dict(os.environ)
    parsed = urlsplit(env.get("CAPTAIN_FACTORY_RUNTIME_URL", ""))
    if parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise RuntimeError("Factory process configuration is unavailable")
    uvicorn.run(
        create_capability_factory_process_app_from_environment(environment=env),
        host="127.0.0.1", port=parsed.port,
    )


if __name__ == "__main__":
    main()


__all__ = [
    "FactoryProcessExecutionRequest", "FactoryProcessExecutor",
    "create_capability_factory_process_app",
    "create_capability_factory_process_app_from_environment",
]
