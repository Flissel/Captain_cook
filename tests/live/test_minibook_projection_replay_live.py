from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread
import time
from typing import Iterator
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest

from agenten.delivery.minibook_client import MinibookClient
from agenten.delivery.minibook_events import MinibookProjectionEvent
from agenten.delivery.projection_cursor import ProjectionCursorStore
from agenten.delivery.projector import MinibookProjector
from scripts.rebuild_minibook_projection import (
    CaptainProjectionFeed,
    consume_incremental_projection,
)


pytestmark = pytest.mark.live

REPOSITORY_ROOT = Path(__file__).parents[2]
MINIBOOK_ROOT = REPOSITORY_ROOT / "minibook"
FIXTURE = (
    REPOSITORY_ROOT
    / "tests"
    / "fixtures"
    / "contracts"
    / "minibook_projection.v1.json"
)
FORBIDDEN = ("token", "password", "secret", "holdout", "prompt", "transcript")


@contextmanager
def projection_feed(
    events: list[MinibookProjectionEvent],
) -> Iterator[str]:
    documents = [
        event.model_dump(mode="json", by_alias=True) for event in events
    ]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if urlparse(self.path).path != "/api/v1/projections/minibook/events":
                self.send_error(404)
                return
            body = json.dumps(
                {"events": documents, "cursor": "live-page-1", "has_more": False}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def independent_minibook(tmp_path: Path) -> Iterator[str]:
    service_root = tmp_path / "minibook-service"
    shutil.copytree(
        MINIBOOK_ROOT,
        service_root,
        ignore=shutil.ignore_patterns(
            "frontend",
            "swarm",
            "__pycache__",
            "*.db",
        ),
    )
    database_path = tmp_path / "minibook-live.db"
    probe = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = probe.server_address[1]
    probe.server_close()
    (service_root / "config.yaml").write_text(
        "\n".join(
            (
                f'hostname: "127.0.0.1:{port}"',
                f'public_url: "http://127.0.0.1:{port}"',
                f"port: {port}",
                f'database: "{database_path.as_posix()}"',
            )
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(tmp_path / "unavailable-hermes"),
            "CODEX_HOME": str(tmp_path / "unavailable-codex"),
            "DOCKER_HOST": "tcp://127.0.0.1:1",
            "FORGE_URL": "http://127.0.0.1:1",
            "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:1",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=service_root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"independent Minibook exited with {process.returncode}")
            try:
                response = httpx.get(f"{base_url}/health", timeout=0.5)
                if response.status_code == 200:
                    assert response.json()["status"] == "ok"
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("independent Minibook health endpoint did not become ready")
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_live_public_http_replay_restart_and_rebuild_without_other_packages(
    tmp_path: Path,
) -> None:
    template = MinibookProjectionEvent.model_validate(
        json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    )
    correlation_id = uuid4()
    event = template.model_copy(
        update={
            "event_id": uuid4(),
            "correlation_id": correlation_id,
            "subject_id": f"live-runtime-{correlation_id}",
            "payload": template.payload.model_copy(
                update={"public_title": f"Live runtime {correlation_id}"}
            ),
        }
    )

    with independent_minibook(tmp_path) as minibook_url, projection_feed([event]) as feed_url:
        assert httpx.get(f"{minibook_url}/health").json()["status"] == "ok"
        registration = httpx.post(
            f"{minibook_url}/api/v1/agents",
            json={"name": f"CaptainLiveProjector_{uuid4().hex}"},
        )
        registration.raise_for_status()
        client = MinibookClient(minibook_url, registration.json()["api_key"])
        feed = CaptainProjectionFeed(feed_url, token="live-test-only")
        try:
            cursor_path = tmp_path / "projection-cursor.db"
            first_store = ProjectionCursorStore(cursor_path)
            first = MinibookProjector(client, first_store)
            first_results = consume_incremental_projection(
                feed,
                first,
                first_store,
                apply=True,
            )
            assert first_results[0].outcome == "projected"
            assert first_store.get_feed_cursor() == "live-page-1"

            restarted_store = ProjectionCursorStore(cursor_path)
            restarted = MinibookProjector(client, restarted_store)
            restarted_results = consume_incremental_projection(
                feed,
                restarted,
                restarted_store,
                apply=True,
            )
            assert restarted_results[0].outcome == "duplicate"
            project = restarted.ensure_projection_project()
            posts = client.list_posts(project["id"])
            assert len(posts) == 1

            client.update_post(posts[0]["id"], content="unsafe operator drift")
            assert restarted.reconcile([event]).modified_event_ids == (
                str(event.event_id),
            )
            assert restarted.reconcile([event], apply=True).changes_applied == 1
            assert restarted.reconcile([event], apply=True).total_changes == 0

            canaries = (
                f"Bearer live-canary-{uuid4().hex}",
                f"raw transcript: live-canary-{uuid4().hex}",
                f"complete log: HOLDOUT_CANARY_{uuid4().hex}",
                f"artifact C:\\Users\\Operator\\private-{uuid4().hex}.txt",
                f"artifact /srv/captain/private-{uuid4().hex}.txt",
                f"file:///var/tmp/private-{uuid4().hex}.txt",
            )
            for canary in canaries:
                unsafe_event = event.model_copy(
                    update={
                        "event_id": uuid4(),
                        "payload": event.payload.model_copy(
                            update={"evidence_summary": canary}
                        ),
                    }
                )
                with pytest.raises(ValueError):
                    restarted.project(unsafe_event)

            post = client.get_post(posts[0]["id"])
            comments = client.list_comments(posts[0]["id"])
            search = client.search_posts(
                project_id=project["id"], query=str(correlation_id)
            )
            public_readback = json.dumps(
                {"post": post, "comments": comments, "search": search}
            ).lower()
            assert not any(term in public_readback for term in FORBIDDEN)
            assert not any(canary.lower() in public_readback for canary in canaries)
        finally:
            feed.close()
            client.close()
