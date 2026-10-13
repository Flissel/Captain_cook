"""Loopback-only Responses-compatible HTTP surface for Codex CLI."""

from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from .service import BudgetRejected, ProviderBudgetProxy, ProviderUsageUnavailable
from .supervisor import DynamicProviderBudgetProxySupervisor


def create_provider_proxy_app(
    *, proxy: ProviderBudgetProxy, client_token: str, runtime_finalize_token: str
) -> FastAPI:
    if not client_token.strip():
        raise ValueError("provider proxy client token must not be blank")
    if (
        not runtime_finalize_token.strip()
        or secrets.compare_digest(client_token, runtime_finalize_token)
    ):
        raise ValueError("runtime finalize token must be a distinct authority")
    app = FastAPI(
        title="Captain Responses Budget Proxy",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def authorize(authorization: str | None) -> None:
        expected = f"Bearer {client_token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="provider proxy authentication failed",
            )

    def authorize_runtime(authorization: str | None) -> None:
        expected = f"Bearer {runtime_finalize_token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="provider finalization authentication failed",
            )

    @app.post("/v1/responses")
    async def create_response(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Response:
        authorize(authorization)
        try:
            payload: Any = await request.json()
            if not isinstance(payload, dict):
                raise BudgetRejected("Responses payload must be an object")
            if payload.get("stream") is True:
                streamed = await proxy.create_stream(payload)
                return StreamingResponse(
                    streamed.chunks,
                    status_code=streamed.status_code,
                    media_type=streamed.headers.get("content-type", "text/event-stream"),
                )
            reply = await proxy.create(payload)
        except BudgetRejected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ProviderUsageUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        return Response(
            content=reply.body,
            status_code=reply.status_code,
            media_type=reply.headers.get("content-type", "application/json"),
        )

    @app.get("/v1/responses/{response_id}")
    async def read_response(
        response_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Response:
        authorize(authorization)
        try:
            reply = await proxy.read(response_id, request.url.query)
        except ProviderUsageUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        return Response(
            content=reply.body,
            status_code=reply.status_code,
            media_type=reply.headers.get("content-type", "application/json"),
        )

    @app.post("/v1/responses/{response_id}/cancel")
    async def cancel_response(
        response_id: str,
        authorization: str | None = Header(default=None),
    ) -> Response:
        authorize(authorization)
        reply = await proxy.cancel(response_id)
        return Response(
            content=reply.body,
            status_code=reply.status_code,
            media_type=reply.headers.get("content-type", "application/json"),
        )

    @app.post("/v1/captain/provider-settlements/finalize")
    async def finalize_provider_settlement(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize_runtime(authorization)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError
            command_id = UUID(str(payload["command_id"]))
            result_id = UUID(str(payload["result_id"]))
            return await proxy.finalize(command_id=command_id, result_id=result_id)
        except (KeyError, ValueError, BudgetRejected) as exc:
            detail = str(exc) if str(exc) else "provider finalization request is invalid"
            raise HTTPException(status_code=409, detail=detail) from None
        except ProviderUsageUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    return app


def create_dynamic_provider_proxy_app(
    *,
    supervisor: DynamicProviderBudgetProxySupervisor,
    client_token: str,
    runtime_finalize_token: str,
) -> FastAPI:
    """Responses surface selecting an exact Gateway-preflighted command context."""

    if not client_token.strip():
        raise ValueError("provider proxy client token must not be blank")
    if (
        not runtime_finalize_token.strip()
        or secrets.compare_digest(client_token, runtime_finalize_token)
    ):
        raise ValueError("runtime finalize token must be a distinct authority")
    app = FastAPI(
        title="Captain Dynamic Responses Budget Proxy",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def authorize(authorization: str | None, *, runtime: bool = False) -> None:
        token = runtime_finalize_token if runtime else client_token
        if authorization is None or not secrets.compare_digest(
            authorization, f"Bearer {token}"
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "provider finalization authentication failed"
                    if runtime
                    else "provider proxy authentication failed"
                ),
            )

    async def selected_proxy(reference: str | None) -> ProviderBudgetProxy:
        if reference is None:
            raise HTTPException(status_code=409, detail="provider authority is unavailable")
        try:
            return await supervisor.proxy_for(reference)
        except BudgetRejected:
            raise HTTPException(status_code=409, detail="provider authority is unavailable") from None

    @app.get("/v1/healthz")
    async def health(authorization: str | None = Header(default=None)) -> dict[str, str]:
        authorize(authorization)
        return {
            "schema": "captain.provider-proxy-health.v1",
            "status": "ready",
            "supervisor_mode": "dynamic_gateway_context_v1",
        }

    @app.post("/v1/responses")
    async def create_response(
        request: Request,
        authorization: str | None = Header(default=None),
        context_reference: str | None = Header(
            default=None, alias="X-Captain-Provider-Context-Ref"
        ),
    ) -> Response:
        authorize(authorization)
        proxy = await selected_proxy(context_reference)
        try:
            payload: Any = await request.json()
            if not isinstance(payload, dict):
                raise BudgetRejected("Responses payload must be an object")
            if payload.get("stream") is True:
                streamed = await proxy.create_stream(payload)
                return StreamingResponse(
                    streamed.chunks,
                    status_code=streamed.status_code,
                    media_type=streamed.headers.get("content-type", "text/event-stream"),
                )
            reply = await proxy.create(payload)
        except BudgetRejected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ProviderUsageUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        return Response(
            content=reply.body,
            status_code=reply.status_code,
            media_type=reply.headers.get("content-type", "application/json"),
        )

    @app.get("/v1/responses/{response_id}")
    async def read_response(
        response_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
        context_reference: str | None = Header(
            default=None, alias="X-Captain-Provider-Context-Ref"
        ),
    ) -> Response:
        authorize(authorization)
        proxy = await selected_proxy(context_reference)
        try:
            reply = await proxy.read(response_id, request.url.query)
        except ProviderUsageUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        return Response(
            content=reply.body,
            status_code=reply.status_code,
            media_type=reply.headers.get("content-type", "application/json"),
        )

    @app.post("/v1/responses/{response_id}/cancel")
    async def cancel_response(
        response_id: str,
        authorization: str | None = Header(default=None),
        context_reference: str | None = Header(
            default=None, alias="X-Captain-Provider-Context-Ref"
        ),
    ) -> Response:
        authorize(authorization)
        proxy = await selected_proxy(context_reference)
        reply = await proxy.cancel(response_id)
        return Response(
            content=reply.body,
            status_code=reply.status_code,
            media_type=reply.headers.get("content-type", "application/json"),
        )

    @app.post("/v1/captain/provider-settlements/finalize")
    async def finalize_provider_settlement(
        request: Request,
        authorization: str | None = Header(default=None),
        context_reference: str | None = Header(
            default=None, alias="X-Captain-Provider-Context-Ref"
        ),
    ) -> dict[str, object]:
        authorize(authorization, runtime=True)
        proxy = await selected_proxy(context_reference)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError
            return await proxy.finalize(
                command_id=UUID(str(payload["command_id"])),
                result_id=UUID(str(payload["result_id"])),
            )
        except (KeyError, ValueError, BudgetRejected) as exc:
            detail = str(exc) if str(exc) else "provider finalization request is invalid"
            raise HTTPException(status_code=409, detail=detail) from None
        except ProviderUsageUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    return app
