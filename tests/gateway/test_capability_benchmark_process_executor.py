from __future__ import annotations

import asyncio
import hashlib
import json
import traceback
from dataclasses import replace
from urllib.request import Request
from uuid import UUID, uuid5

import pytest
from pydantic import SecretStr

from agenten.agent_runtime.contracts import ArtifactRef
from gateway.capability_benchmark_process_executor import (
    GatewayHttpBenchmarkProcessTransport,
    GatewayIsolatedBenchmarkExecutorClient,
    _RejectBenchmarkRedirects,
)
from gateway.capability_generic_benchmark import (
    GenericBenchmarkCategory,
    GenericBenchmarkExecutionRequest,
    GenericExecutionCaseView,
    GenericPrivateCaseRef,
    generic_execution_request_digest,
)
from gateway.capability_live_pipeline import CapabilityLiveAuthorityUnavailable


_NS = UUID("166b2b95-b6d5-5737-a2f2-f377b46004b8")


def _id(label: str) -> UUID:
    return uuid5(_NS, label)


def _ref(label: str) -> ArtifactRef:
    digest = hashlib.sha256(label.encode()).hexdigest()
    return ArtifactRef(
        uri=f"artifact://sha256/{digest}", sha256=digest,
        media_type="application/json",
    )


def _request(label: str = "request") -> GenericBenchmarkExecutionRequest:
    case_ref = GenericPrivateCaseRef(
        case_id="case-1234567890abcdef",
        category=GenericBenchmarkCategory.NORMAL,
        assertion_ids=("captain-private-assertion",),
        case_digest="a" * 64,
    )
    execution_case = GenericExecutionCaseView(
        case_ref=case_ref,
        setup="Redacted customer setup",
        action="Perform the bounded action",
        opaque_source_refs=("b" * 64,),
    )
    nonce = _id(label)
    values = dict(
        job_id=_id("job"), capability_id="generic_capability",
        input_ref=_ref("input"), case_ref=case_ref, team_kind="team",
        model_id="fixed-model", model_policy_digest="c" * 64,
        execution_case=execution_case, request_nonce=nonce,
    )
    return GenericBenchmarkExecutionRequest(
        request_digest=generic_execution_request_digest(**values), **values,
    )


def _response(payload: dict[str, object]) -> bytes:
    return json.dumps({
        "schema": "captain.capability-benchmark-execution-response.v1",
        "request_nonce": payload["request_nonce"],
        "request_digest": payload["request_digest"],
        "agent_count": 2,
        "outcome_ref": _ref("outcome").model_dump(mode="json"),
        "message_event_refs": [_ref("message").model_dump(mode="json")],
        "handoff_event_refs": [_ref("handoff").model_dump(mode="json")],
        "tool_calls": [{
            "tool_key": "captain_tool",
            "call_ref": _ref("call").model_dump(mode="json"),
            "lease_ref": _ref("lease").model_dump(mode="json"),
            "receipt_ref": _ref("receipt").model_dump(mode="json"),
        }],
        "rationale_ref": _ref("rationale").model_dump(mode="json"),
        "cost_usd": "0.10", "latency_ms": 10,
        "trace_id": str(_id("trace-" + str(payload["request_nonce"]))),
        "run_id": str(_id("run-" + str(payload["request_nonce"]))),
    }, sort_keys=True, separators=(",", ":")).encode()


class _Transport:
    def __init__(self, mode: str = "valid") -> None:
        self.mode = mode
        self.requests: list[bytes] = []
        self.replayed: bytes | None = None

    async def post(self, endpoint: str, body: bytes) -> bytes:
        del endpoint
        self.requests.append(body)
        if self.mode == "failure":
            raise RuntimeError("provider-secret-body-C:/private/runtime")
        payload = json.loads(body)
        if self.mode == "echo":
            return body
        if self.mode == "replay" and self.replayed is not None:
            return self.replayed
        response = _response(payload)
        if self.mode == "wrong_nonce":
            value = json.loads(response)
            value["request_nonce"] = str(_id("wrong-nonce"))
            response = json.dumps(value).encode()
        if self.mode == "wrong_digest":
            value = json.loads(response)
            value["request_digest"] = "d" * 64
            response = json.dumps(value).encode()
        if self.mode == "fabricated_predicates":
            value = json.loads(response)
            value["expected_predicates"] = ["stolen"]
            response = json.dumps(value).encode()
        if self.mode == "replay":
            self.replayed = response
        return response


def _client(transport: _Transport) -> GatewayIsolatedBenchmarkExecutorClient:
    return GatewayIsolatedBenchmarkExecutorClient(
        gateway_store=object(), authority_id=_id("isolated-runtime"),
        endpoint="https://benchmark-runtime.invalid/v1/execute",
        transport=transport,
        allowed_hosts=("benchmark-runtime.invalid",),
    )


def test_isolated_executor_sends_only_canonical_execution_dto() -> None:
    transport = _Transport()
    request = _request()

    evidence = asyncio.run(_client(transport).execute(request))

    payload = json.loads(transport.requests[0])
    serialized = transport.requests[0].decode()
    assert set(payload["execution_case"]) == {"setup", "action", "opaque_source_refs"}
    assert all(item not in serialized for item in (
        "expected_predicates", "private_holdout", "repository", "evaluator",
        "captain-private-assertion",
    ))
    assert evidence.request_nonce == request.request_nonce
    assert evidence.request_digest == request.request_digest


@pytest.mark.parametrize(
    "mode", ("echo", "fabricated_predicates", "wrong_nonce", "wrong_digest")
)
def test_isolated_executor_rejects_malicious_or_misbound_response(mode: str) -> None:
    with pytest.raises(CapabilityLiveAuthorityUnavailable):
        asyncio.run(_client(_Transport(mode)).execute(_request()))


def test_isolated_executor_rejects_replayed_response() -> None:
    transport = _Transport("replay")
    client = _client(transport)
    asyncio.run(client.execute(_request("first")))

    with pytest.raises(
        CapabilityLiveAuthorityUnavailable, match="changed request binding"
    ):
        asyncio.run(client.execute(_request("second")))


def test_isolated_executor_rejects_mutated_request_before_transport() -> None:
    transport = _Transport()
    original = _request()
    mutated = replace(
        original,
        execution_case=replace(original.execution_case, action="mutated action"),
    )

    with pytest.raises(
        CapabilityLiveAuthorityUnavailable, match="request binding is invalid"
    ):
        asyncio.run(_client(transport).execute(mutated))
    assert transport.requests == []


def test_isolated_executor_redacts_endpoint_failure() -> None:
    secret = "provider-secret-body-C:/private/runtime"
    with pytest.raises(CapabilityLiveAuthorityUnavailable) as captured:
        asyncio.run(_client(_Transport("failure")).execute(_request()))

    rendered = "".join(traceback.format_exception(captured.value))
    assert secret not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is not None
    assert captured.value.__suppress_context__ is True
    assert "isolated benchmark runtime is unavailable" in rendered


def test_production_http_binding_and_auth_configuration_are_immutable() -> None:
    caller_secret = SecretStr("unit-test-runtime-token")
    transport = GatewayHttpBenchmarkProcessTransport(
        bearer_token=caller_secret
    )
    client = GatewayIsolatedBenchmarkExecutorClient(
        gateway_store=object(), authority_id=_id("immutable-runtime"),
        endpoint="http://127.0.0.1:8765/v1/execute", transport=transport,
    )

    assert client.uses_production_transport() is True
    assert "unit-test-runtime-token" not in repr(client)
    original_digest = transport.configuration_digest()
    object.__setattr__(caller_secret, "_secret_value", "caller-mutated-secret")
    assert transport.configuration_digest() == original_digest
    assert transport._authorization_header().endswith("runtime-token")
    assert "caller-mutated-secret" not in transport._authorization_header()
    assert "unit-test-runtime-token" not in repr(transport)
    with pytest.raises(AttributeError, match="immutable"):
        client._transport = _Transport()  # type: ignore[misc]
    with pytest.raises(AttributeError, match="immutable"):
        transport._timeout_seconds = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("endpoint", "allowed"),
    (
        ("https://not-allowed.invalid/v1/execute", ()),
        ("http://benchmark-runtime.invalid/v1/execute", ("benchmark-runtime.invalid",)),
    ),
)
def test_production_client_rejects_untrusted_endpoint(
    endpoint: str, allowed: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        GatewayIsolatedBenchmarkExecutorClient(
            gateway_store=object(), authority_id=_id("endpoint-runtime"),
            endpoint=endpoint,
            transport=GatewayHttpBenchmarkProcessTransport(
                bearer_token=SecretStr("unit-test-runtime-token")
            ),
            allowed_hosts=allowed,
        )


@pytest.mark.parametrize("status", (301, 302, 303, 307, 308))
def test_http_transport_never_forwards_authorization_across_redirect(
    status: int,
) -> None:
    original = Request(
        "http://127.0.0.1:8765/v1/execute", data=b"{}", method="POST",
        headers={"Authorization": "test-auth"},
    )

    redirected = _RejectBenchmarkRedirects().redirect_request(
        original, None, status, "redirect", {},
        "https://attacker.invalid/collect",
    )

    assert redirected is None
