from __future__ import annotations

from typing import Any

from .minibook_client import MinibookClient


class MinibookProjector:
    def __init__(self, client: MinibookClient) -> None:
        self.client = client

    def ensure_project(self, name: str, description: str) -> dict[str, Any]:
        existing = next((item for item in self.client.list_projects() if item["name"] == name), None)
        return existing or self.client.create_project(name, description)

    def upsert_plan(self, project_id: str, title: str, content: str) -> dict[str, Any]:
        existing = next(
            (post for post in self.client.list_posts(project_id) if post["title"] == title),
            None,
        )
        if existing:
            return self.client.update_post(
                existing["id"], content=content, status="open", pin_order=0,
                tags=["captain-delivery-plan"],
            )
        post = self.client.create_post(
            project_id, title=title, content=content, tags=["captain-delivery-plan"]
        )
        return self.client.update_post(post["id"], pin_order=0)

    def post_assignment(
        self, post_id: str, *, run_id: str, assignee: str, todo_title: str
    ) -> dict[str, Any]:
        return self.client.create_comment(
            post_id,
            f"Assignment `{run_id}` → **{assignee}**: {todo_title}",
        )
