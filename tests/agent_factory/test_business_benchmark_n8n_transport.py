from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from agenten.agent_factory.business_benchmark_n8n import (
    RenewalCommercialSnapshotV1,
    RenewalContextN8nProviderRequestV1,
    RenewalContextReadInputV1,
    _bound_idempotency_key,
)
from agenten.agent_factory.business_benchmark_n8n_transport import (
    CaptainNativeMcpRenewalContextTransport,
    RenewalContextMcpDispatchUnknownError,
    RenewalContextMcpProviderError,
)
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_runtime.contracts import ArtifactRef, RuntimeStatus
from agenten.agent_runtime.n8n_endpoint import N8nEndpoint


NOW = datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc)
WORKFLOW_ID = "renewal-workflow-42"
WORKFLOW_DIGEST = "4" * 64
WORKFLOW_REF = ArtifactRef(
    uri=f"artifact://benchmark-renewal/workflow/{WORKFLOW_DIGEST}",
    sha256=WORKFLOW_DIGEST,
    media_type="application/json",
)
UPSTREAM_TOKEN = "captain-upstream-token-must-never-escape"
BROKER_TOKEN = "captain-short-lived-broker-token-must-never-escape"


def endpoint(*, port: int = 5679, broker_port: int = 5680) -> N8nEndpoint:
    return N8nEndpoint(
        mode="captain-builder",
        api_base_url=f"http://localhost:{port}",
        webhook_base_url=f"http://localhost:{port}",
        api_key="captain-api-key-must-never-escape",
        mcp_token=UPSTREAM_TOKEN,
        mcp_broker_url=f"http://localhost:{broker_port}",
    )


def request_value() -> RenewalContextN8nProviderRequestV1:
    tool = TypedN8nTool(
        name="renewal_context_read",
        description="Read a synthetic renewal context.",
        input_schema_ref="artifact://benchmark-renewal/input/" + "1" * 64,
        output_schema_ref="artifact://benchmark-renewal/output/" + "2" * 64,
    ).opaque_reference()
    payload = RenewalContextReadInputV1(
        operation="read_renewal_context",
        idempotency_key="0" * 64,
        evidence_partition="ordinary",
        synthetic_subject_id="subject-demo1",
        commercial_snapshot=RenewalCommercialSnapshotV1(
            renewal_window="30_days",
            engagement_band="medium",
            commercial_evidence_state="complete",
            consent_state="granted",
        ),
    )
    values = {
        "job_id": UUID("93000000-0000-0000-0000-000000000001"),
        "correlation_id": UUID("93000000-0000-0000-0000-000000000002"),
        "subject_version": 3,
        "attempt": 1,
        "invocation_id": UUID("93000000-0000-0000-0000-000000000003"),
        "request_id": UUID("93000000-0000-0000-0000-000000000004"),
        "runtime_session_id": "renewal-session-1",
        "case_id": "holdout-ordinary-1",
        "case_sha256": "a" * 64,
        "workspace_ref": "workspace://benchmark-renewal/n8n",
        "tool": tool,
        "effect": "read_only",
        "effect_id": "b" * 64,
        "claim_id": UUID("93000000-0000-0000-0000-000000000005"),
        "fence": 7,
        "runtime_command_id": UUID("93000000-0000-0000-0000-000000000006"),
        "grant_id": "grant-renewal-context-1",
        "requested_idempotency_sha256": "c" * 64,
        "input_payload": payload,
    }
    bound = _bound_idempotency_key(**values)
    values["input_payload"] = payload.model_copy(
        update={"idempotency_key": bound}
    )
    return RenewalContextN8nProviderRequestV1.model_validate(values)


def output_value(request: RenewalContextN8nProviderRequestV1) -> dict[str, object]:
    return {
        "operation": "read_renewal_context",
        "idempotency_key": request.input_payload.idempotency_key,
        "status": "read",
        "facts": [
            "renewal_window.30_days",
            "engagement_band.medium",
            "commercial_evidence_state.complete",
            "consent_state.granted",
        ],
    }


def provider_payload(
    request: RenewalContextN8nProviderRequestV1,
    *,
    workflow_id: str = WORKFLOW_ID,
    execution_id: str = "execution-101",
    correlation_id: str | None = None,
    artifact_digest: str = WORKFLOW_DIGEST,
    status: str = "success",
    output: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "workflowId": workflow_id,
        "executionId": execution_id,
        "status": status,
        "captain": {
            "correlation_id": correlation_id or str(request.correlation_id),
            "artifact_digest": artifact_digest,
            "job_id": str(request.job_id),
            "invocation_id": str(request.invocation_id),
            "request_id": str(request.request_id),
            "case_id": request.case_id,
            "case_sha256": request.case_sha256,
            "runtime_command_id": str(request.runtime_command_id),
            "grant_id": request.grant_id,
            "effect_id": request.effect_id,
            "claim_id": str(request.claim_id),
            "fence": request.fence,
        },
        "output": output or output_value(request),
    }


def rpc(call_id: str, value: dict[str, object]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "result": {"structuredContent": value, "isError": False},
    }


def transport(client: httpx.AsyncClient, **kwargs) -> CaptainNativeMcpRenewalContextTransport:
    return CaptainNativeMcpRenewalContextTransport(
        client=client,
        workflow_id=WORKFLOW_ID,
        workflow_ref=WORKFLOW_REF,
        clock=lambda: NOW,
        poll_attempts=kwargs.pop("poll_attempts", 3),
        poll_delay_seconds=kwargs.pop("poll_delay_seconds", 0),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_execute_calls_exact_production_webhook_and_builds_bound_response() -> None:
    current = request_value()
    observed: list[dict[str, object]] = []

    async def handler(raw: httpx.Request) -> httpx.Response:
        assert raw.url == httpx.URL("http://localhost:5679/mcp-server/http")
        assert raw.headers["authorization"] == f"Bearer {UPSTREAM_TOKEN}"
        assert raw.headers["accept"] == "application/json, text/event-stream"
        body = json.loads(raw.content)
        observed.append(body)
        assert body["method"] == "tools/call"
        assert body["params"]["name"] == "execute_workflow"
        arguments = body["params"]["arguments"]
        assert arguments["workflowId"] == WORKFLOW_ID
        assert arguments["executionMode"] == "production"
        assert arguments["inputs"]["type"] == "webhook"
        webhook = arguments["inputs"]["webhookData"]
        assert webhook["method"] == "POST"
        assert webhook["headers"] == {}
        assert webhook["query"] == {}
        assert webhook["body"]["operation"] == "read_renewal_context"
        assert "captain" not in webhook["body"]
        return httpx.Response(200, json=rpc(body["id"], provider_payload(current)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await transport(client).execute(
            endpoint=endpoint(), request=current, timeout_seconds=1
        )

    assert len(observed) == 1
    assert result.request == current
    assert result.workflow_ref == WORKFLOW_REF
    assert result.execution.execution_id == "execution-101"
    assert result.execution.workflow_id == WORKFLOW_ID
    assert result.execution.artifact_digest == WORKFLOW_DIGEST
    assert result.execution.correlation_id == str(current.correlation_id)
    assert result.output.model_dump(mode="json") == output_value(current)
    assert result.runtime_result.command_id == current.runtime_command_id
    assert result.runtime_result.grant_id == current.grant_id
    assert result.runtime_result.correlation_id == current.correlation_id
    assert result.runtime_result.artifact_refs == (WORKFLOW_REF,)
    assert result.runtime_result.status is RuntimeStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_sse_execute_response_polls_get_execution_until_success() -> None:
    current = request_value()
    tools: list[str] = []

    async def handler(raw: httpx.Request) -> httpx.Response:
        body = json.loads(raw.content)
        name = body["params"]["name"]
        tools.append(name)
        if name == "execute_workflow":
            value = provider_payload(current, status="running")
        elif tools.count("get_execution") == 1:
            assert body["params"]["arguments"] == {
                "workflowId": WORKFLOW_ID,
                "executionId": "execution-101",
                "includeData": True,
            }
            value = provider_payload(current, status="running")
        else:
            value = provider_payload(current)
        event = json.dumps(rpc(body["id"], value), separators=(",", ":"))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f": heartbeat\n\nevent: message\ndata: {event}\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await transport(client).execute(
            endpoint=endpoint(), request=current, timeout_seconds=1
        )

    assert tools == ["execute_workflow", "get_execution", "get_execution"]
    assert result.execution.execution_id == "execution-101"


@pytest.mark.asyncio
async def test_bound_idempotency_is_sufficient_when_execution_omits_captain_echo() -> None:
    current = request_value()
    tools: list[str] = []

    async def handler(raw: httpx.Request) -> httpx.Response:
        body = json.loads(raw.content)
        name = body["params"]["name"]
        tools.append(name)
        value = {
            "workflowId": WORKFLOW_ID,
            "executionId": "execution-101",
            "status": "started" if name == "execute_workflow" else "success",
        }
        if name == "get_execution":
            value = {
                "execution": {
                    "id": "execution-101",
                    "workflowId": WORKFLOW_ID,
                    "status": "success",
                },
                "data": {
                    "resultData": {
                        "runData": {
                            "Read Synthetic Renewal Context": [
                                {
                                    "data": {
                                        "main": [
                                            [[{"json": output_value(current)}]]
                                        ]
                                    }
                                }
                            ]
                        }
                    }
                },
            }
        return httpx.Response(200, json=rpc(body["id"], value))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await transport(client).execute(
            endpoint=endpoint(), request=current, timeout_seconds=1
        )

    assert tools == ["execute_workflow", "get_execution"]
    assert result.output.idempotency_key == current.input_payload.idempotency_key


@pytest.mark.asyncio
async def test_injected_request_bound_broker_token_uses_broker_and_is_redacted() -> None:
    current = request_value()
    issued_for: list[RenewalContextN8nProviderRequestV1] = []

    def issue_broker_token(
        request: RenewalContextN8nProviderRequestV1,
    ) -> str:
        issued_for.append(request)
        return BROKER_TOKEN

    async def handler(raw: httpx.Request) -> httpx.Response:
        assert raw.url == httpx.URL("http://localhost:5680/mcp-server/http")
        assert raw.headers["authorization"] == f"Bearer {BROKER_TOKEN}"
        body = json.loads(raw.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32000, "message": f"Bearer {BROKER_TOKEN}"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        selected = transport(client, broker_token_issuer=issue_broker_token)
        assert BROKER_TOKEN not in repr(selected)
        assert UPSTREAM_TOKEN not in repr(selected)
        with pytest.raises(RenewalContextMcpProviderError) as error:
            await selected.execute(
                endpoint=endpoint(), request=current, timeout_seconds=1
            )

    assert issued_for == [current]
    assert BROKER_TOKEN not in str(error.value)
    assert UPSTREAM_TOKEN not in str(error.value)


@pytest.mark.asyncio
async def test_upstream_token_is_never_sent_to_configured_broker_without_issuer() -> None:
    current = request_value()
    urls: list[httpx.URL] = []

    async def handler(raw: httpx.Request) -> httpx.Response:
        urls.append(raw.url)
        assert raw.headers["authorization"] == f"Bearer {UPSTREAM_TOKEN}"
        body = json.loads(raw.content)
        return httpx.Response(200, json=rpc(body["id"], provider_payload(current)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await transport(client).execute(
            endpoint=endpoint(), request=current, timeout_seconds=1
        )

    assert urls == [httpx.URL("http://localhost:5679/mcp-server/http")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"workflow_id": "foreign-workflow"}, "workflow"),
        ({"correlation_id": "93000000-0000-0000-0000-000000000099"}, "correlation"),
        ({"execution_id": ""}, "execution"),
        (
            {"output": {"operation": "read_renewal_context", "idempotency_key": "f" * 64, "status": "read", "facts": ["forged.fact"]}},
            "output",
        ),
    ],
)
async def test_provider_bindings_fail_closed(
    mutation: dict[str, object], message: str
) -> None:
    current = request_value()

    async def handler(raw: httpx.Request) -> httpx.Response:
        body = json.loads(raw.content)
        value = provider_payload(current, **mutation)
        return httpx.Response(200, json=rpc(body["id"], value))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RenewalContextMcpProviderError, match=message):
            await transport(client).execute(
                endpoint=endpoint(), request=current, timeout_seconds=1
            )


@pytest.mark.asyncio
async def test_jsonrpc_id_mismatch_fails_closed() -> None:
    current = request_value()

    async def handler(raw: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rpc("foreign-call", provider_payload(current)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RenewalContextMcpProviderError, match="JSON-RPC"):
            await transport(client).execute(
                endpoint=endpoint(), request=current, timeout_seconds=1
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["jsonrpc", "tool"])
async def test_provider_errors_are_sanitized_and_never_expose_auth(kind: str) -> None:
    current = request_value()

    async def handler(raw: httpx.Request) -> httpx.Response:
        body = json.loads(raw.content)
        if kind == "jsonrpc":
            payload = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32000, "message": f"Bearer {UPSTREAM_TOKEN}"},
            }
        else:
            payload = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": f"token={UPSTREAM_TOKEN}"}],
                },
            }
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        selected = transport(client)
        assert UPSTREAM_TOKEN not in repr(selected)
        with pytest.raises(RenewalContextMcpProviderError) as error:
            await selected.execute(
                endpoint=endpoint(), request=current, timeout_seconds=1
            )

    assert UPSTREAM_TOKEN not in str(error.value)
    assert "Bearer" not in str(error.value)
    assert "token=" not in str(error.value)


@pytest.mark.asyncio
async def test_unknown_execute_dispatch_is_not_retried_and_is_sanitized() -> None:
    current = request_value()
    calls = 0

    async def handler(raw: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout(f"Bearer {UPSTREAM_TOKEN}", request=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RenewalContextMcpDispatchUnknownError) as error:
            await transport(client).execute(
                endpoint=endpoint(), request=current, timeout_seconds=1
            )

    assert calls == 1
    assert UPSTREAM_TOKEN not in str(error.value)


@pytest.mark.asyncio
async def test_poll_timeout_does_not_repeat_execute_workflow() -> None:
    current = request_value()
    tools: list[str] = []

    async def handler(raw: httpx.Request) -> httpx.Response:
        body = json.loads(raw.content)
        name = body["params"]["name"]
        tools.append(name)
        value = provider_payload(current, status="running")
        return httpx.Response(200, json=rpc(body["id"], value))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RenewalContextMcpProviderError, match="timed out"):
            await transport(client, poll_attempts=2).execute(
                endpoint=endpoint(), request=current, timeout_seconds=1
            )

    assert tools == ["execute_workflow", "get_execution", "get_execution"]


@pytest.mark.asyncio
async def test_transport_rejects_non_captain_endpoint_before_dispatch() -> None:
    current = request_value()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("unexpected dispatch"))
    ) as client:
        selected = transport(client)
        with pytest.raises(RenewalContextMcpProviderError, match="Captain"):
            await selected.execute(
                endpoint=endpoint(port=15678), request=current, timeout_seconds=1
            )


def test_transport_rejects_invalid_workflow_binding() -> None:
    client = httpx.AsyncClient()

    with pytest.raises(ValueError, match="workflow"):
        CaptainNativeMcpRenewalContextTransport(
            client=client,
            workflow_id=" ",
            workflow_ref=WORKFLOW_REF,
            clock=lambda: NOW,
        )
