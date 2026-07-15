from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agenten.delivery.minibook_client import MinibookClient
from agenten.delivery.projector import MinibookProjector


pytestmark = pytest.mark.live


def test_real_minibook_projects_plan_assignment_and_readback() -> None:
    run_id = datetime.now(timezone.utc).strftime("live-%Y%m%dT%H%M%S%f")
    client = MinibookClient.from_hermes_profile(
        base_url="http://127.0.0.1:3456",
        timeout_seconds=5.0,
    )
    assert client.health()["status"] == "ok"

    for name in (
        "Captain Architect Builder",
        "Captain Real Case Tester",
        "Captain Quality Warden",
    ):
        assert client.ensure_agent(name)["name"] == name

    projector = MinibookProjector(client)
    project = projector.ensure_project("Captain Cook", "Durable agent delivery team")
    title = f"[live-test:{run_id}] Delivery plan"
    content = f"# Live delivery plan\n\nCorrelation: `{run_id}`"
    post = projector.upsert_plan(project["id"], title, content)
    assert client.get_post(post["id"])["content"] == content

    comment = projector.post_assignment(
        post["id"],
        run_id=run_id,
        assignee="Captain Architect Builder",
        todo_title="Prove Minibook projection",
    )
    assert run_id in comment["content"]

    closed = client.update_post(post["id"], status="closed")
    assert closed["status"] == "closed"
