"""Agent lifecycle records: registration, re-versioning and retirement."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gateway.agent_lifecycle import (
    AGENT_CAPABILITY_GRANTED_BLOCK_TYPE,
    AGENT_CAPABILITY_REVOKED_BLOCK_TYPE,
    AGENT_REGISTERED_BLOCK_TYPE,
    AGENT_RETIRED_BLOCK_TYPE,
    AgentCapabilityGrantRequest,
    AgentLedgerError,
    AgentProjection,
    AgentRegisteredEvent,
    AgentRetireRequest,
    project_agent,
)
from gateway.app import create_app
from gateway.settings import GatewaySettings
from gateway.store import GatewayStore, require_generic_block_type

SPEC = "a" * 64


def registered(index: int, version: str = "1", agent_id: str = "openclaw") -> dict[str, Any]:
    return {
        "index": index,
        "parent_index": None,
        "block_type": AGENT_REGISTERED_BLOCK_TYPE,
        "data": {"agent_id": agent_id, "version": version, "owner": "captain", "spec_sha256": SPEC},
        "status": "registered",
    }


def retired(index: int, reason: str = "captain_retired", agent_id: str = "openclaw") -> dict[str, Any]:
    return {
        "index": index,
        "parent_index": None,
        "block_type": AGENT_RETIRED_BLOCK_TYPE,
        "data": {"agent_id": agent_id, "reason": reason},
        "status": "retired",
    }


def test_registration_requires_a_valid_agent_id_and_spec_digest() -> None:
    with pytest.raises(ValidationError):
        AgentRegisteredEvent(agent_id="Not Valid", version="1", owner="captain", spec_sha256=SPEC)
    with pytest.raises(ValidationError):
        AgentRegisteredEvent(agent_id="openclaw", version="1", owner="captain", spec_sha256="xyz")
    with pytest.raises(ValidationError):
        AgentRetireRequest.model_validate({"reason": "bored"})


def test_unknown_agent_has_no_projection() -> None:
    assert project_agent([registered(1, agent_id="other")], "openclaw") is None


def test_latest_registration_is_the_current_version() -> None:
    projection = project_agent([registered(1), registered(5, version="2")], "openclaw")

    assert projection is not None
    assert projection.status == "registered"
    assert projection.version == "2"
    assert projection.registration_count == 2


def test_retirement_is_terminal() -> None:
    projection = project_agent([registered(1), retired(2)], "openclaw")

    assert projection is not None
    assert projection.status == "retired"
    assert projection.retire_reason == "captain_retired"
    with pytest.raises(AgentLedgerError, match="after agent_retired"):
        project_agent([registered(1), retired(2), registered(3, version="2")], "openclaw")


def test_lifecycle_rules_reject_inconsistent_ledgers() -> None:
    with pytest.raises(AgentLedgerError, match="before agent_registered"):
        project_agent([retired(1)], "openclaw")
    with pytest.raises(AgentLedgerError, match="registered twice"):
        project_agent([registered(1), registered(2)], "openclaw")


def test_projection_follows_ledger_order_not_list_order() -> None:
    projection = project_agent([registered(7, version="2"), registered(3)], "openclaw")

    assert projection is not None and projection.version == "2"


@pytest.mark.parametrize("block_type", [AGENT_REGISTERED_BLOCK_TYPE, AGENT_RETIRED_BLOCK_TYPE])
def test_agent_records_cannot_be_written_through_generic_blocks(block_type: str) -> None:
    with pytest.raises(HTTPException) as refused:
        require_generic_block_type(block_type)

    assert refused.value.status_code == 422


# --- HTTP routes (fake store; the MariaDB store is covered in test_gateway.py) ---

CAPTAIN_TOKEN = "captain-lifecycle-test-token"
WORKER_TOKEN = "worker-lifecycle-test-token"
AGENT_TOKEN = "openclaw-lifecycle-test-token"


def _settings() -> GatewaySettings:
    return GatewaySettings.model_validate(
        {
            "ledger_dsn": "mysql://ignored:ignored@127.0.0.1:3306/ignored",
            "captain_gateway_token": CAPTAIN_TOKEN,
            "worker_gateway_token": WORKER_TOKEN,
            "captain_runtime_url": "https://runtime.test",
            "captain_runtime_token": "runtime-lifecycle-test-token",
            "worker_agent_token_sha256": {
                "openclaw": hashlib.sha256(AGENT_TOKEN.encode()).hexdigest()
            },
        }
    )


class FakeAgentStore:
    require_active_agent = GatewayStore.require_active_agent

    def __init__(self, projection: AgentProjection | None = None) -> None:
        self.projection = projection
        self.registered: list[AgentRegisteredEvent] = []
        self.claims: list[str | None] = []
        self.granted: list[tuple[str, AgentCapabilityGrantRequest]] = []

    def agent(self, agent_id: str) -> AgentProjection | None:
        return self.projection

    def register_agent(self, event: AgentRegisteredEvent) -> tuple[dict[str, Any], bool]:
        replayed = event in self.registered
        self.registered.append(event)
        return {"data": event.model_dump(mode="json")}, replayed

    def claim(self, batch_id: str, agent_id: str | None) -> dict[str, str | int]:
        self.claims.append(agent_id)
        return {"claim_token": "t", "claim_id": "c", "fencing_token": 1}

    def grant_agent_capability(
        self, agent_id: str, request: AgentCapabilityGrantRequest
    ) -> tuple[dict[str, Any], bool]:
        self.granted.append((agent_id, request))
        data = {"agent_id": agent_id, **request.model_dump(mode="json")}
        return {"data": data}, len(self.granted) > 1


def _client(store: FakeAgentStore) -> TestClient:
    return TestClient(create_app(settings=_settings(), gateway_store=store))  # type: ignore[arg-type]


def _projection(status: str) -> AgentProjection:
    return AgentProjection(
        agent_id="openclaw",
        status=status,  # type: ignore[arg-type]
        version="1",
        owner="captain",
        spec_sha256=SPEC,
        registration_count=1,
        retire_reason="captain_retired" if status == "retired" else None,
    )


REGISTRATION = {"agent_id": "openclaw", "version": "1", "owner": "captain", "spec_sha256": SPEC}


def test_only_captain_registers_and_replay_answers_200() -> None:
    store = FakeAgentStore()
    client = _client(store)

    worker = client.post("/agents", json=REGISTRATION, headers={"Authorization": f"Bearer {WORKER_TOKEN}"})
    created = client.post("/agents", json=REGISTRATION, headers={"Authorization": f"Bearer {CAPTAIN_TOKEN}"})
    replayed = client.post("/agents", json=REGISTRATION, headers={"Authorization": f"Bearer {CAPTAIN_TOKEN}"})

    assert worker.status_code == 403
    assert (created.status_code, replayed.status_code) == (201, 200)
    assert created.json() == REGISTRATION


def test_registration_requires_an_agent_that_holds_a_token() -> None:
    store = FakeAgentStore()

    response = _client(store).post(
        "/agents",
        json={**REGISTRATION, "agent_id": "ghost"},
        headers={"Authorization": f"Bearer {CAPTAIN_TOKEN}"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "agent has no registered token"
    assert store.registered == []


def test_retired_agent_token_cannot_claim() -> None:
    store = FakeAgentStore(_projection("retired"))

    response = _client(store).post(
        "/batches/batch-1/claim", headers={"Authorization": f"Bearer {AGENT_TOKEN}"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "agent is retired"
    assert store.claims == []


def test_registered_and_unregistered_agents_still_claim() -> None:
    for projection in (_projection("registered"), None):
        store = FakeAgentStore(projection)

        response = _client(store).post(
            "/batches/batch-1/claim", headers={"Authorization": f"Bearer {AGENT_TOKEN}"}
        )

        assert response.status_code == 200
        assert store.claims == ["openclaw"]


def test_unknown_agent_reads_404() -> None:
    response = _client(FakeAgentStore()).get(
        "/agents/openclaw", headers={"Authorization": f"Bearer {CAPTAIN_TOKEN}"}
    )

    assert response.status_code == 404


# --- capability grants ------------------------------------------------------

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def granted(
    index: int,
    grant_id: str = "grant-1",
    tags: tuple[str, ...] = ("python", "n8n-builder"),
    expires: datetime | None = None,
) -> dict[str, Any]:
    return {
        "index": index,
        "parent_index": None,
        "block_type": AGENT_CAPABILITY_GRANTED_BLOCK_TYPE,
        "data": {
            "agent_id": "openclaw",
            "grant_id": grant_id,
            "capability_tags": list(tags),
            "expires_at": (expires or NOW + timedelta(hours=1)).isoformat(),
        },
        "status": "granted",
    }


def revoked(index: int, grant_id: str = "grant-1") -> dict[str, Any]:
    return {
        "index": index,
        "parent_index": None,
        "block_type": AGENT_CAPABILITY_REVOKED_BLOCK_TYPE,
        "data": {"agent_id": "openclaw", "grant_id": grant_id, "reason": "captain_revoked"},
        "status": "revoked",
    }


def test_grant_request_needs_tags_and_an_aware_expiry() -> None:
    with pytest.raises(ValidationError):
        AgentCapabilityGrantRequest(grant_id="grant-1", capability_tags=(), expires_at=NOW)
    with pytest.raises(ValidationError):
        AgentCapabilityGrantRequest.model_validate(
            {"grant_id": "grant-1", "capability_tags": ["python"], "expires_at": "2026-11-09T12:00:00"}
        )


def test_active_grant_must_cover_every_batch_tag() -> None:
    projection = project_agent([registered(1), granted(2)], "openclaw")
    assert projection is not None

    assert projection.covers(("python",), now=NOW)
    assert projection.covers(("n8n-builder", "python"), now=NOW)
    assert projection.covers((), now=NOW)
    assert not projection.covers(("python", "deploy"), now=NOW)


def test_one_grant_must_cover_all_tags_not_the_union() -> None:
    projection = project_agent(
        [registered(1), granted(2, tags=("python",)), granted(3, "grant-2", tags=("deploy",))],
        "openclaw",
    )
    assert projection is not None

    assert not projection.covers(("python", "deploy"), now=NOW)


def test_expired_or_revoked_grants_cover_nothing() -> None:
    expired = project_agent([registered(1), granted(2, expires=NOW)], "openclaw")
    withdrawn = project_agent([registered(1), granted(2), revoked(3)], "openclaw")

    assert expired is not None and not expired.covers(("python",), now=NOW)
    assert withdrawn is not None and not withdrawn.covers(("python",), now=NOW)
    assert withdrawn.grants[0].revoked is True


def test_grant_rules_reject_inconsistent_ledgers() -> None:
    with pytest.raises(AgentLedgerError, match="before agent_registered"):
        project_agent([granted(1)], "openclaw")
    with pytest.raises(AgentLedgerError, match="granted twice"):
        project_agent([registered(1), granted(2), granted(3)], "openclaw")
    with pytest.raises(AgentLedgerError, match="unknown grant"):
        project_agent([registered(1), revoked(2)], "openclaw")
    with pytest.raises(AgentLedgerError, match="revoked twice"):
        project_agent([registered(1), granted(2), revoked(3), revoked(4)], "openclaw")


def test_only_captain_grants_capabilities_and_replay_answers_200() -> None:
    store = FakeAgentStore(_projection("registered"))
    client = _client(store)
    body = {
        "grant_id": "grant-1",
        "capability_tags": ["python", "deploy"],
        "expires_at": "2026-11-10T12:00:00+00:00",
    }

    worker = client.post(
        "/agents/openclaw/grants", json=body, headers={"Authorization": f"Bearer {AGENT_TOKEN}"}
    )
    created = client.post(
        "/agents/openclaw/grants", json=body, headers={"Authorization": f"Bearer {CAPTAIN_TOKEN}"}
    )
    replayed = client.post(
        "/agents/openclaw/grants", json=body, headers={"Authorization": f"Bearer {CAPTAIN_TOKEN}"}
    )

    assert worker.status_code == 403
    assert (created.status_code, replayed.status_code) == (201, 200)
    assert created.json()["capability_tags"] == ["deploy", "python"]
    assert [agent for agent, _ in store.granted] == ["openclaw", "openclaw"]


def test_assignment_route_is_captain_only_and_needs_a_token_holding_agent() -> None:
    class AssigningStore(FakeAgentStore):
        def __init__(self) -> None:
            super().__init__()
            self.assigned: list[tuple[str, str]] = []

        def assign(self, batch_id: str, agent_id: str) -> tuple[dict[str, Any], bool]:
            self.assigned.append((batch_id, agent_id))
            return {"data": {"batch_id": batch_id, "agent_id": agent_id}}, False

    store = AssigningStore()
    client = _client(store)

    worker = client.post(
        "/batches/batch-1/assign",
        json={"agent_id": "openclaw"},
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
    )
    ghost = client.post(
        "/batches/batch-1/assign",
        json={"agent_id": "ghost"},
        headers={"Authorization": f"Bearer {CAPTAIN_TOKEN}"},
    )
    created = client.post(
        "/batches/batch-1/assign",
        json={"agent_id": "openclaw"},
        headers={"Authorization": f"Bearer {CAPTAIN_TOKEN}"},
    )

    assert (worker.status_code, ghost.status_code, created.status_code) == (403, 422, 201)
    assert store.assigned == [("batch-1", "openclaw")]
