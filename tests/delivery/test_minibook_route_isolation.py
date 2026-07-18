from __future__ import annotations

import ast
from pathlib import Path
import sys
from typing import Any, Iterator
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from agenten.delivery.minibook_client import MinibookClient
from agenten.delivery.minibook_events import MinibookProjectionEvent
from agenten.delivery.projection_cursor import ProjectionCursorStore
from agenten.delivery.projector import MinibookProjector


REPOSITORY_ROOT = Path(__file__).parents[2]
MINIBOOK_ROOT = REPOSITORY_ROOT / "minibook"
MAIN_PATH = MINIBOOK_ROOT / "src" / "main.py"
FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "contracts"
    / "minibook_projection.v2.json"
)
PROJECTION_API_KEY = "projection-route-scope-test-only"
PROJECTION_PROJECT_ID = "captain-runtime-projection-v2"


@pytest.fixture
def isolated_projection_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, MinibookClient, Any]]:
    sys.path.insert(0, str(MINIBOOK_ROOT))
    from src import main as minibook_main

    monkeypatch.setenv("MINIBOOK_PROJECTION_API_KEY", PROJECTION_API_KEY)
    minibook_main.DB_PATH = str(tmp_path / "minibook.db")
    minibook_main.SessionLocal = None
    with TestClient(minibook_main.app) as http:
        registration = http.post(
            "/api/v1/agents",
            json={"name": f"ProjectionAdversary_{uuid4().hex}"},
        )
        assert registration.status_code == 200
        client = MinibookClient(
            "http://127.0.0.1",
            registration.json()["api_key"],
            projection_api_key=PROJECTION_API_KEY,
            client=http,
        )
        yield http, client, minibook_main


def _event() -> MinibookProjectionEvent:
    import json

    document = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    return MinibookProjectionEvent.model_validate(document)


def _seed_reserved_state(
    *,
    case: str,
    minibook_main: Any,
) -> str | None:
    from src.models import AgentRegistry, GitHubWebhook, Webhook

    with minibook_main.SessionLocal() as db:
        if case == "delete_webhook":
            webhook = Webhook(
                project_id=PROJECTION_PROJECT_ID,
                url="https://example.test/stale-hook",
            )
            db.add(webhook)
            db.commit()
            return str(webhook.id)
        if case in {"delete_github_webhook", "github_receiver"}:
            config = GitHubWebhook(
                project_id=PROJECTION_PROJECT_ID,
                secret="route-isolation-fixture-only",
            )
            db.add(config)
            db.commit()
            return str(config.id)
        if case == "registry_link":
            entry = AgentRegistry(team_key="route-isolation", run_id=uuid4().hex)
            db.add(entry)
            db.commit()
            return str(entry.id)
    return None


@pytest.mark.parametrize(
    "case",
    [
        "create_project_name",
        "join",
        "member_role",
        "create_post",
        "patch_post",
        "comment",
        "create_webhook",
        "delete_webhook",
        "create_github_webhook",
        "delete_github_webhook",
        "github_receiver",
        "roles",
        "plan",
        "admin_project",
        "admin_member_role",
        "admin_member_delete",
        "registry_link",
    ],
)
def test_every_ordinary_mutation_route_rejects_reserved_projection_project(
    case: str,
    tmp_path: Path,
    isolated_projection_api: tuple[TestClient, MinibookClient, Any],
) -> None:
    http, client, minibook_main = isolated_projection_api
    projector = MinibookProjector(client, ProjectionCursorStore(tmp_path / "cursor.db"))
    event = _event()
    projected = projector.project(event)
    assert projected.outcome == "projected"
    assert projected.post_id is not None
    seeded_id = _seed_reserved_state(case=case, minibook_main=minibook_main)
    headers = client._headers
    service_agent_id = "captain-projection-service-v2"

    if case == "create_project_name":
        response = http.post(
            "/api/v1/projects",
            headers=headers,
            json={"name": "Captain Runtime Projection", "description": "spoof"},
        )
    elif case == "join":
        response = http.post(
            f"/api/v1/projects/{PROJECTION_PROJECT_ID}/join",
            headers=headers,
            json={"role": "owner"},
        )
    elif case == "member_role":
        response = http.patch(
            f"/api/v1/projects/{PROJECTION_PROJECT_ID}/members/{service_agent_id}",
            headers=headers,
            json={"role": "attacker"},
        )
    elif case == "create_post":
        response = http.post(
            f"/api/v1/projects/{PROJECTION_PROJECT_ID}/posts",
            headers=headers,
            json={
                "title": "Injected",
                "content": "arbitrary projection prose",
                "tags": ["captain-projection:v2", "captain-event:spoof"],
            },
        )
    elif case == "patch_post":
        response = http.patch(
            f"/api/v1/posts/{projected.post_id}",
            headers=headers,
            json={
                "title": "Overwritten",
                "content": "arbitrary projection prose",
                "tags": ["spoof"],
            },
        )
    elif case == "comment":
        response = http.post(
            f"/api/v1/posts/{projected.post_id}/comments",
            headers=headers,
            json={"content": "arbitrary projection comment"},
        )
    elif case == "create_webhook":
        response = http.post(
            f"/api/v1/projects/{PROJECTION_PROJECT_ID}/webhooks",
            headers=headers,
            json={"url": "https://example.test/hook", "events": ["new_post"]},
        )
    elif case == "delete_webhook":
        assert seeded_id is not None
        response = http.delete(f"/api/v1/webhooks/{seeded_id}", headers=headers)
    elif case == "create_github_webhook":
        response = http.post(
            f"/api/v1/projects/{PROJECTION_PROJECT_ID}/github-webhook",
            headers=headers,
            json={
                "secret": "ordinary-route-secret",
                "events": ["issues"],
                "labels": [],
            },
        )
    elif case == "delete_github_webhook":
        response = http.delete(
            f"/api/v1/projects/{PROJECTION_PROJECT_ID}/github-webhook",
            headers=headers,
        )
    elif case == "github_receiver":
        response = http.post(
            f"/api/v1/github-webhook/{PROJECTION_PROJECT_ID}",
            headers={
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": "sha256=invalid",
            },
            json={"action": "opened"},
        )
    elif case == "roles":
        response = http.put(
            f"/api/v1/projects/{PROJECTION_PROJECT_ID}/roles",
            headers=headers,
            json={"owner": "arbitrary projection prose"},
        )
    elif case == "plan":
        response = http.put(
            f"/api/v1/projects/{PROJECTION_PROJECT_ID}/plan",
            headers=headers,
            params={"title": "Injected plan", "content": "arbitrary prose"},
        )
    elif case == "admin_project":
        response = http.patch(
            f"/api/v1/admin/projects/{PROJECTION_PROJECT_ID}",
            headers=headers,
            json={"primary_lead_agent_id": ""},
        )
    elif case == "admin_member_role":
        response = http.patch(
            f"/api/v1/admin/projects/{PROJECTION_PROJECT_ID}/members/{service_agent_id}",
            headers=headers,
            json={"role": "attacker"},
        )
    elif case == "admin_member_delete":
        response = http.delete(
            f"/api/v1/admin/projects/{PROJECTION_PROJECT_ID}/members/{service_agent_id}",
            headers=headers,
        )
    elif case == "registry_link":
        assert seeded_id is not None
        response = http.put(
            f"/api/v1/registry/{seeded_id}/status",
            headers=headers,
            json={
                "status": "candidate",
                "community_project_id": PROJECTION_PROJECT_ID,
            },
        )
    else:  # pragma: no cover - exhaustive test table
        raise AssertionError(case)

    assert response.status_code == 403, (case, response.status_code, response.text)


def test_non_reserved_project_mutations_remain_available(
    isolated_projection_api: tuple[TestClient, MinibookClient, Any],
) -> None:
    http, client, _ = isolated_projection_api
    project = client.create_project("Ordinary project", "control")
    post = client.create_post(
        project["id"],
        title="Ordinary post",
        content="ordinary content",
        tags=["ordinary"],
    )

    assert client.update_post(post["id"], content="updated")["content"] == "updated"
    assert client.create_comment(post["id"], "ordinary comment")["content"] == (
        "ordinary comment"
    )
    assert http.put(
        f"/api/v1/projects/{project['id']}/roles",
        json={"reviewer": "Reviews"},
    ).status_code == 200
    assert http.put(
        f"/api/v1/projects/{project['id']}/plan",
        params={"title": "Plan", "content": "ordinary plan"},
    ).status_code == 200
    assert http.post(
        f"/api/v1/projects/{project['id']}/webhooks",
        headers=client._headers,
        json={"url": "https://example.test/hook", "events": ["new_post"]},
    ).status_code == 200
    assert http.post(
        f"/api/v1/projects/{project['id']}/github-webhook",
        headers=client._headers,
        json={"secret": "ordinary-control", "events": ["issues"], "labels": []},
    ).status_code == 200


def test_projector_retirement_is_scoped_structured_and_canonical(
    tmp_path: Path,
    isolated_projection_api: tuple[TestClient, MinibookClient, Any],
) -> None:
    http, client, _ = isolated_projection_api
    event = _event()
    projector = MinibookProjector(client, ProjectionCursorStore(tmp_path / "cursor.db"))
    result = projector.project(event)
    assert result.post_id is not None
    scoped_headers = {"Authorization": f"Bearer {PROJECTION_API_KEY}"}

    retired = http.post(
        f"/api/v1/projects/{PROJECTION_PROJECT_ID}/projection-posts/"
        f"{result.post_id}/retire",
        headers=scoped_headers,
        json={"reason": "duplicate"},
    )
    tampered = http.post(
        f"/api/v1/projects/{PROJECTION_PROJECT_ID}/projection-posts/"
        f"{result.post_id}/retire",
        headers=scoped_headers,
        json={"reason": "duplicate", "content": "arbitrary prose"},
    )
    arbitrary_reason = http.post(
        f"/api/v1/projects/{PROJECTION_PROJECT_ID}/projection-posts/"
        f"{result.post_id}/retire",
        headers=scoped_headers,
        json={"reason": "operator-prose"},
    )
    ordinary = http.post(
        f"/api/v1/projects/{PROJECTION_PROJECT_ID}/projection-posts/"
        f"{result.post_id}/retire",
        headers=client._headers,
        json={"reason": "duplicate"},
    )

    assert retired.status_code == 200
    assert retired.json()["status"] == "closed"
    assert retired.json()["title"] == "Retired Captain projection"
    assert retired.json()["content"] == (
        "- **State:** Retired\n- **Reason:** Duplicate projection"
    )
    assert retired.json()["tags"] == [
        "captain-projection-retired:v2",
        "captain-retired:duplicate",
    ]
    assert tampered.status_code == 422
    assert arbitrary_reason.status_code == 422
    assert ordinary.status_code == 403

    repaired = http.put(
        f"/api/v1/projects/{PROJECTION_PROJECT_ID}/projection-post",
        headers=scoped_headers,
        json=event.model_dump(mode="json", by_alias=True),
    )
    assert repaired.status_code == 200
    assert repaired.json()["status"] == "open"
    assert repaired.json()["title"] == "[plan.requested] Runtime planning requested"


def test_projection_client_retirement_uses_scoped_bearer_and_reason_enum() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "retired"}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = MinibookClient(
            "https://minibook.example",
            "ordinary-agent-key",
            projection_api_key="projection-scope-key",
            client=http,
        )
        client.retire_projection_post(
            PROJECTION_PROJECT_ID,
            "projection-post",
            reason="orphaned",
        )

    assert requests[0].method == "POST"
    assert requests[0].url.path == (
        f"/api/v1/projects/{PROJECTION_PROJECT_ID}/projection-posts/"
        "projection-post/retire"
    )
    assert requests[0].headers["Authorization"] == "Bearer projection-scope-key"
    assert requests[0].read().decode("utf-8") == '{"reason":"orphaned"}'


def test_mutating_route_inventory_requires_reserved_project_classification() -> None:
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    mutation_methods = {"post", "put", "patch", "delete"}
    routes: dict[str, tuple[str, str]] = {}
    functions: dict[str, ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        functions[node.name] = node
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            target = decorator.func
            if not (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "app"
                and target.attr in mutation_methods
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
            ):
                continue
            routes[node.name] = (target.attr.upper(), decorator.args[0].value)

    guarded = {
        "create_project": "_forbid_reserved_projection_name",
        "join_project": "_forbid_reserved_projection_project",
        "update_member_role": "_forbid_reserved_projection_project",
        "create_post": "_forbid_reserved_projection_project",
        "update_post": "_forbid_reserved_projection_project",
        "create_comment": "_forbid_reserved_projection_project",
        "create_webhook": "_forbid_reserved_projection_project",
        "delete_webhook": "_forbid_reserved_projection_project",
        "create_github_webhook": "_forbid_reserved_projection_project",
        "delete_github_webhook": "_forbid_reserved_projection_project",
        "receive_github_webhook": "_forbid_reserved_projection_project",
        "set_role_descriptions": "_forbid_reserved_projection_project",
        "set_plan": "_forbid_reserved_projection_project",
        "admin_update_project": "_forbid_reserved_projection_project",
        "admin_update_member_role": "_forbid_reserved_projection_project",
        "admin_remove_member": "_forbid_reserved_projection_project",
        "update_registry_status": "_forbid_reserved_projection_project",
    }
    projector_only = {
        "upsert_projection_project",
        "upsert_projection_post",
        "retire_projection_post",
    }
    global_mutations = {
        "register_agent",
        "heartbeat",
        "mark_read",
        "mark_all_read",
        "create_question",
        "answer_question",
        "register_agent_team",
        "add_improvement",
    }
    assert set(routes) == set(guarded) | projector_only | global_mutations

    for function_name, required_call in guarded.items():
        call_names = {
            call.func.id
            for call in ast.walk(functions[function_name])
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        assert required_call in call_names, function_name
