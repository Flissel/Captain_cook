"""The in-process benchmark tool broker enforces the leased operation itself."""

import hashlib
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from gateway.capability_benchmark_execution_service import GatewayBenchmarkToolBrokerSession
from gateway.capability_generic_benchmark import GenericAuthorizedToolCall
from gateway.capability_live_pipeline import CapabilityLiveAuthorityUnavailable


def _ref(sha: str) -> object:
    from agenten.agent_runtime.contracts import ArtifactRef

    return ArtifactRef(uri=f"artifact://sha256/{sha}", sha256=sha, media_type="application/json")


class _Registry:
    digest = "d" * 64

    def __init__(self) -> None:
        self.calls: list[str] = []

    def require_declared(self, tool_keys: tuple[str, ...]) -> None:
        del tool_keys

    def execute(self, *, tool_key: str, operation: str, arguments: object) -> dict[str, object]:
        del tool_key, arguments
        self.calls.append(operation)
        return {"ok": True}


def _session(registry: _Registry) -> GatewayBenchmarkToolBrokerSession:
    authorization = GenericAuthorizedToolCall(
        tool_key="policy_lookup",
        operation="lookup",
        call_id="call-1",
        lease_id="lease-1",
        call_ref=_ref("a" * 64),
        lease_ref=_ref("b" * 64),
        scoped_token=SecretStr("scoped-token-0123456789"),
    )
    artifacts = SimpleNamespace(
        put=lambda payload, media_type: _ref(hashlib.sha256(payload).hexdigest())
    )
    return GatewayBenchmarkToolBrokerSession(
        registry=registry, artifacts=artifacts, authorizations=(authorization,)
    )


def test_operation_outside_the_lease_never_reaches_the_tool() -> None:
    registry = _Registry()

    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="lease is invalid"):
        _session(registry).execute(
            tool_key="policy_lookup", call_id="call-1", lease_id="lease-1",
            operation="delete_everything", arguments={},
        )

    assert registry.calls == []


def test_leased_operation_runs_once() -> None:
    registry = _Registry()
    session = _session(registry)

    session.execute(
        tool_key="policy_lookup", call_id="call-1", lease_id="lease-1",
        operation="lookup", arguments={},
    )

    assert registry.calls == ["lookup"]
