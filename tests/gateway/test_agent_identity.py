"""Per-agent worker identity: own tokens, claims bound to the claiming agent."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from gateway.auth import GatewayRole, current_agent_id, require_actor
from gateway.contracts import BatchProjection, project_batch
from gateway.settings import GatewayConfigurationError, GatewaySettings
from gateway.store import GatewayStore, require_claim_identity

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
CLAIM_TOKEN = "claim-secret"


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


# --- settings -------------------------------------------------------------


def test_agent_token_digests_load_from_the_environment() -> None:
    settings = GatewaySettings.from_env(environment())

    assert settings.worker_agent_token_sha256 == {
        "hermes-coder-1": sha256("agent-one-token"),
        "openclaw": sha256("agent-two-token"),
    }
    assert settings.require_agent_identity is False


def test_agent_identity_is_optional_by_default() -> None:
    values = dict(environment())
    del values["WORKER_AGENT_TOKEN_SHA256_JSON"]

    settings = GatewaySettings.from_env(values)

    assert settings.worker_agent_token_sha256 == {}


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        json.dumps(["openclaw"]),
        json.dumps({"Bad Id": sha256("x")}),
        json.dumps({"openclaw": "not-a-digest"}),
        json.dumps({"a": sha256("same"), "b": sha256("same")}),
        json.dumps({"impostor": sha256("captain-test-token")}),
        json.dumps({"impostor": sha256("worker-test-token")}),
    ],
)
def test_invalid_agent_token_digests_fail_closed(raw: str) -> None:
    with pytest.raises(GatewayConfigurationError):
        GatewaySettings.from_env(environment(WORKER_AGENT_TOKEN_SHA256_JSON=raw))


def test_required_identity_needs_registered_agents() -> None:
    values = dict(environment(GATEWAY_REQUIRE_AGENT_IDENTITY="true"))
    assert GatewaySettings.from_env(values).require_agent_identity is True

    del values["WORKER_AGENT_TOKEN_SHA256_JSON"]
    with pytest.raises(GatewayConfigurationError):
        GatewaySettings.from_env(values)


# --- authentication -------------------------------------------------------


def identity_client(settings: GatewaySettings) -> TestClient:
    app = FastAPI()
    app.state.gateway_settings = settings
    app.state.gateway_settings_lock = threading.Lock()

    @app.get("/whoami")
    def whoami(
        actor: GatewayRole = Depends(require_actor),
        agent_id: str | None = Depends(current_agent_id),
    ) -> dict[str, Any]:
        return {"role": actor.value, "agent_id": agent_id}

    return TestClient(app)


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("agent-one-token", {"role": "worker", "agent_id": "hermes-coder-1"}),
        ("agent-two-token", {"role": "worker", "agent_id": "openclaw"}),
        ("worker-test-token", {"role": "worker", "agent_id": None}),
        ("captain-test-token", {"role": "captain", "agent_id": None}),
    ],
)
def test_bearer_tokens_resolve_role_and_agent(token: str, expected: dict[str, Any]) -> None:
    client = identity_client(GatewaySettings.from_env(environment()))

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == expected


def test_unknown_token_is_still_unauthorized() -> None:
    client = identity_client(GatewaySettings.from_env(environment()))

    response = client.get("/whoami", headers={"Authorization": "Bearer nope"})

    assert response.status_code == 401


# --- claim policy ---------------------------------------------------------


def test_anonymous_claims_are_refused_when_identity_is_required() -> None:
    required = GatewaySettings.from_env(environment(GATEWAY_REQUIRE_AGENT_IDENTITY="true"))
    optional = GatewaySettings.from_env(environment())

    with pytest.raises(HTTPException) as excinfo:
        require_claim_identity(None, required_settings=required)
    assert excinfo.value.status_code == 403

    require_claim_identity("openclaw", required_settings=required)
    require_claim_identity(None, required_settings=optional)


# --- projection and claim binding -----------------------------------------


def blocks_with_claim(**claim_data: Any) -> list[dict[str, Any]]:
    return [
        {
            "index": 10,
            "parent_index": None,
            "block_type": "work_batch",
            "data": {"batch_id": "batch-1"},
            "status": "pending",
        },
        {
            "index": 11,
            "parent_index": 10,
            "block_type": "batch_claimed",
            "data": {
                "batch_id": "batch-1",
                "claim_id": "claim-1",
                "fencing_token": 1,
                "claim_token_sha256": sha256(CLAIM_TOKEN),
                "claim_expires_at": (NOW + timedelta(minutes=30)).isoformat(),
                **claim_data,
            },
            "status": "recorded",
        },
    ]


def test_projection_exposes_the_claiming_agent() -> None:
    bound = project_batch(blocks_with_claim(agent_id="openclaw"), "batch-1", now=NOW)
    legacy = project_batch(blocks_with_claim(), "batch-1", now=NOW)

    assert bound.claim_agent_id == "openclaw"
    assert legacy.claim_agent_id is None


def projection(agent_id: str | None) -> BatchProjection:
    extra = {} if agent_id is None else {"agent_id": agent_id}
    return project_batch(blocks_with_claim(**extra), "batch-1", now=NOW)


def test_bound_claim_accepts_only_its_agent() -> None:
    bound = projection("openclaw")

    GatewayStore._assert_live_claim(bound, CLAIM_TOKEN, now=NOW, agent_id="openclaw")
    for intruder in ("hermes-coder-1", None):
        with pytest.raises(HTTPException) as excinfo:
            GatewayStore._assert_live_claim(bound, CLAIM_TOKEN, now=NOW, agent_id=intruder)
        assert excinfo.value.status_code == 403
        assert excinfo.value.detail == "claim belongs to another agent"


def test_unbound_legacy_claim_keeps_token_only_checks() -> None:
    GatewayStore._assert_live_claim(projection(None), CLAIM_TOKEN, now=NOW, agent_id="openclaw")
    GatewayStore._assert_live_claim(projection(None), CLAIM_TOKEN, now=NOW, agent_id=None)
