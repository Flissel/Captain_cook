"""Runtime results are reported only by the agent that holds the grant."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from agenten.agent_runtime.contracts import CapabilityGrant
from gateway.auth import GatewayRole
from gateway.settings import GatewaySettings
from gateway.store import (
    RuntimeResultReporter,
    require_registered_grant_holder,
    require_result_reporter,
)

ISSUED_AT = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def environment(**overrides: str) -> Mapping[str, str]:
    values = {
        "LEDGER_DSN": "mariadb://captain:database-secret@127.0.0.1/captain",
        "CAPTAIN_GATEWAY_TOKEN": "captain-test-token",
        "WORKER_GATEWAY_TOKEN": "worker-test-token",
        "CAPTAIN_RUNTIME_URL": "https://runtime.test",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-test-token",
        "WORKER_AGENT_TOKEN_SHA256_JSON": json.dumps(
            {"hermes-coder-1": sha256("agent-one-token"), "openclaw": sha256("agent-two-token")}
        ),
    }
    values.update(overrides)
    return values


def grant_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "captain.capability-grant.v1",
        "grant_id": "grant-holder-1",
        "command_id": str(uuid4()),
        "batch_id": "batch-1",
        "batch_version": 3,
        "subtask_id": "subtask-1",
        "workspace_ref": "workspace://authorized/project-1/subtask-1",
        "profile": "n8n-builder",
        "capabilities": ["codex.run", "mcp.n8n"],
        "mcp_servers": ["n8n-mcp"],
        "issued_at": ISSUED_AT.isoformat().replace("+00:00", "Z"),
        "expires_at": (ISSUED_AT + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
    }
    payload.update(overrides)
    return payload


def grant(holder: str | None) -> CapabilityGrant:
    extra = {} if holder is None else {"holder_agent_id": holder}
    return CapabilityGrant.model_validate(grant_payload(**extra))


# --- contract -------------------------------------------------------------


def test_legacy_grant_serializes_without_a_holder_key() -> None:
    payload = grant_payload()

    dumped = CapabilityGrant.model_validate(payload).model_dump(mode="json", by_alias=True)

    assert "holder_agent_id" not in dumped
    assert dumped == payload


def test_bound_grant_round_trips_its_holder() -> None:
    payload = grant_payload(holder_agent_id="openclaw")

    parsed = CapabilityGrant.model_validate(payload)

    assert parsed.holder_agent_id == "openclaw"
    assert parsed.model_dump(mode="json", by_alias=True) == payload
    assert json.loads(parsed.model_dump_json(by_alias=True))["holder_agent_id"] == "openclaw"


@pytest.mark.parametrize("holder", ["", "Bad Id", "-leading-dash", "x" * 65])
def test_malformed_holder_is_rejected(holder: str) -> None:
    with pytest.raises(ValidationError):
        CapabilityGrant.model_validate(grant_payload(holder_agent_id=holder))


# --- grant issuance -------------------------------------------------------


def test_grant_holder_must_be_a_registered_agent() -> None:
    settings = GatewaySettings.from_env(environment())

    require_registered_grant_holder(grant(None), settings=settings)
    require_registered_grant_holder(grant("openclaw"), settings=settings)
    with pytest.raises(HTTPException) as excinfo:
        require_registered_grant_holder(grant("ghost-agent"), settings=settings)
    assert excinfo.value.status_code == 422


# --- result reporting -----------------------------------------------------


def reporter(actor: GatewayRole, agent_id: str | None = None) -> RuntimeResultReporter:
    return RuntimeResultReporter(actor=actor, agent_id=agent_id)


def assert_refused(reporting: RuntimeResultReporter, held: CapabilityGrant, *, required: bool) -> None:
    with pytest.raises(HTTPException) as excinfo:
        require_result_reporter(held, reporting, require_agent_identity=required)
    assert excinfo.value.status_code == 403


def test_bound_grant_accepts_only_its_holder() -> None:
    held = grant("openclaw")

    require_result_reporter(
        held, reporter(GatewayRole.WORKER, "openclaw"), require_agent_identity=False
    )
    for intruder in (
        reporter(GatewayRole.WORKER, "hermes-coder-1"),
        reporter(GatewayRole.WORKER, None),
        reporter(GatewayRole.CAPTAIN, None),
    ):
        assert_refused(intruder, held, required=False)


def test_unbound_grant_keeps_role_only_checks() -> None:
    held = grant(None)

    for anyone in (
        reporter(GatewayRole.CAPTAIN),
        reporter(GatewayRole.WORKER),
        reporter(GatewayRole.WORKER, "openclaw"),
    ):
        require_result_reporter(held, anyone, require_agent_identity=False)


def test_required_identity_refuses_anonymous_worker_results() -> None:
    held = grant(None)

    assert_refused(reporter(GatewayRole.WORKER), held, required=True)
    require_result_reporter(held, reporter(GatewayRole.WORKER, "openclaw"), require_agent_identity=True)
    require_result_reporter(held, reporter(GatewayRole.CAPTAIN), require_agent_identity=True)


def test_gateway_internal_reporting_skips_the_reporter_check() -> None:
    require_result_reporter(grant("openclaw"), None, require_agent_identity=True)
