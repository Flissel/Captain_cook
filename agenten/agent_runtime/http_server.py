"""Authenticated HTTP boundary for the authoritative agent runtime service."""

from __future__ import annotations

import logging
import secrets
from typing import Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request

from agenten.agent_runtime.contracts import AgentRuntimeCommand, AgentRuntimeResult


logger = logging.getLogger(__name__)


class RuntimeCommandExecutor(Protocol):
    """Execute one already typed command through Captain's runtime authority."""

    async def execute(self, command: AgentRuntimeCommand) -> AgentRuntimeResult: ...


def create_runtime_app(
    *,
    executor: RuntimeCommandExecutor,
    token: str,
) -> FastAPI:
    """Create a thin authenticated boundary without adding lifecycle authority."""

    if not token or not token.strip() or "\r" in token or "\n" in token:
        raise ValueError("runtime token must be a non-empty HTTP credential")
    expected_authorization = f"Bearer {token}".encode("utf-8")

    async def require_runtime_token(
        authorization: str | None = Header(default=None),
    ) -> None:
        presented = authorization.encode("utf-8") if authorization is not None else b""
        if not secrets.compare_digest(presented, expected_authorization):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="runtime authentication failed",
                headers={"WWW-Authenticate": "Bearer"},
            )

    app = FastAPI()

    @app.exception_handler(RequestValidationError)
    async def invalid_runtime_command(
        _request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": "invalid runtime command"},
        )

    @app.get("/health")
    async def health(_: None = Depends(require_runtime_token)) -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/runtime/execute", response_model=AgentRuntimeResult)
    async def execute(
        command: AgentRuntimeCommand,
        _: None = Depends(require_runtime_token),
    ) -> AgentRuntimeResult:
        try:
            result = await executor.execute(command)
        except Exception:
            logger.error("Runtime command execution failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="runtime execution failed",
            ) from None
        if result.error is None:
            return result
        return result.model_copy(
            update={"error": f"{result.operation.value} execution failed"}
        )

    return app
