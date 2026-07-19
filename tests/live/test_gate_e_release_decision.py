from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest

from agenten.delivery.gateway_client import GatewayDeliveryClient
from agenten.delivery.minibook_client import MinibookClient
from agenten.delivery.minibook_events import MinibookProjectionEvent
from agenten.delivery.projection_cursor import ProjectionCursorStore
from agenten.delivery.projector import MinibookProjector
from gateway.contracts import DeliveryEventEnvelope


pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[2]
POLICY_VERSION = "captain-ready-to-use.v1"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"required live gate: {name} is unavailable")
    return value


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_gateway(base_url: str) -> None:
    for _ in range(60):
        try:
            if httpx.get(f"{base_url}/healthz", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    pytest.fail("required live gate: ephemeral Gateway did not become ready")


def _start_gateway(*, dsn: str, captain_token: str, worker_token: str, port: int) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment.update(
        {
            "LEDGER_DSN": dsn,
            "CAPTAIN_GATEWAY_TOKEN": captain_token,
            "WORKER_GATEWAY_TOKEN": worker_token,
            "GATEWAY_PORT": str(port),
        }
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "gateway.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "error",
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _minibook_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    env_path = ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, separator, candidate = line.partition("=")
            if separator and key == name and candidate.strip():
                return candidate.strip()
    pytest.fail(f"required live gate: {name} is unavailable")


@pytest.mark.asyncio
async def test_gate_e_records_one_gateway_decision_and_acknowledged_minibook_projection(
    tmp_path: Path,
) -> None:
    """Release one candidate only after its three real provider runs are stored."""

    dsn = _required("TEST_MARIADB_DSN")
    project_id = _required("CAPTAIN_RELEASE_PROJECT_ID")
    run_id = _required("CAPTAIN_RELEASE_RUN_ID")

    captain_token = secrets.token_urlsafe(32)
    worker_token = secrets.token_urlsafe(32)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    gateway = _start_gateway(
        dsn=dsn,
        captain_token=captain_token,
        worker_token=worker_token,
        port=port,
    )
    try:
        _wait_for_gateway(base_url)
        async with httpx.AsyncClient(timeout=30) as http:
            captain = GatewayDeliveryClient(base_url, captain_token, http)
            before = await http.get(
                f"{base_url}/v1/projects/{project_id}/runs/{run_id}/release",
                headers={"Authorization": f"Bearer {captain_token}"},
            )
            before.raise_for_status()
            release = before.json()
            assert release["status"] == "ready"
            assert len(release["clean_e2e_run_ids"]) == 3

            decision = await captain.record_release_decision(
                project_id=project_id,
                run_id=run_id,
                policy_version=POLICY_VERSION,
            )
            assert decision.payload.decision == "accepted"

            events = await captain.delivery_events(project_id=project_id, run_id=run_id)
            artifact = next(event for event in reversed(events) if event.event_type == "artifact_built")
            assert artifact.trace.artifact_id is not None
            artifact_digest = artifact.payload.sha256

            client = MinibookClient(
                _minibook_value("MINIBOOK_BACKEND_URL"),
                _minibook_value("MINIBOOK_API_KEY"),
                projection_api_key=_minibook_value("MINIBOOK_PROJECTION_API_KEY"),
            )
            try:
                projection = MinibookProjectionEvent.model_validate(
                    {
                        "schema": "captain.minibook-projection.v2",
                        "event_id": str(uuid4()),
                        "correlation_id": str(uuid4()),
                        "causation_id": str(decision.event_id),
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                        "producer": "captain-gateway",
                        "subject_id": f"subject:{uuid4()}",
                        "subject_version": 1,
                        "event_type": "validation.recorded",
                        "payload": {
                            "view": "validation",
                            "template_id": "runtime_validation_recorded",
                            "status_id": "validated",
                            "batch_id": f"batch:{uuid4()}",
                            "batch_version": 1,
                            "actor_role_id": "captain_planner",
                            "artifact_digest": f"sha256:{artifact_digest}",
                        },
                    }
                )
                projected = MinibookProjector(
                    client,
                    ProjectionCursorStore(tmp_path / "release-projection-cursor.db"),
                ).project(projection)
                assert projected.outcome == "projected"
            finally:
                client.close()

            mirror = DeliveryEventEnvelope.model_validate(
                {
                    "event_id": uuid4(),
                    "event_type": "registry_mirror",
                    "occurred_at": datetime.now(timezone.utc),
                    "actor": "captain-gateway",
                    "trace": {
                        "project_id": project_id,
                        "run_id": run_id,
                        "trace_id": f"release-mirror:{decision.event_id}",
                        "artifact_id": artifact.trace.artifact_id,
                    },
                    "payload": {
                        "event_type": "registry_mirror",
                        "capability_id": f"release:{project_id}",
                        "capability_version": POLICY_VERSION,
                        "outcome": "mirrored",
                    },
                }
            )
            await captain.append_delivery_event(mirror)
            final_events = await captain.delivery_events(project_id=project_id, run_id=run_id)
            assert [event.event_type for event in final_events].count("e2e_run") == 3
            assert any(
                event.event_type == "release_decision"
                and event.payload.decision == "accepted"
                for event in final_events
            )
            assert any(
                event.event_type == "registry_mirror"
                and event.payload.outcome == "mirrored"
                for event in final_events
            )
    finally:
        gateway.terminate()
        try:
            gateway.wait(timeout=15)
        except subprocess.TimeoutExpired:
            gateway.kill()
            gateway.wait(timeout=15)
