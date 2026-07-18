from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml


def validate_service_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("service base URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("service base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("service base URL must not contain query or fragment")
    hostname = parsed.hostname.casefold()
    loopback = hostname == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
    if parsed.scheme != "https" and not loopback:
        raise ValueError("non-loopback service base URL requires https")
    return normalized


class MinibookClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 10.0,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        base_url = validate_service_base_url(base_url)
        self._base_url = base_url
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_seconds),
        )
        self._owns_client = client is None

    @classmethod
    def from_hermes_profile(
        cls, *, base_url: str, timeout_seconds: float = 10.0
    ) -> "MinibookClient":
        profile = Path.home() / "AppData" / "Local" / "hermes" / "config.yaml"
        data = yaml.safe_load(profile.read_text(encoding="utf-8")) or {}
        minibook = data.get("minibook", {})
        key = minibook.get("api_key")
        if not isinstance(key, str) or not key:
            raise RuntimeError("Hermes profile has no Minibook API key")
        return cls(base_url, key, timeout_seconds)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(self._headers)
        headers.update(kwargs.pop("headers", {}))
        kwargs["headers"] = headers
        response = self._client.request(method, f"{self._base_url}{path}", **kwargs)
        response.raise_for_status()
        return response.json()

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def ensure_agent(self, name: str) -> dict[str, Any]:
        response = self._client.get(
            f"{self._base_url}/api/v1/agents/by-name/{name}", headers=self._headers
        )
        if response.status_code == 200:
            return response.json()["agent"]
        if response.status_code != 404:
            response.raise_for_status()
        created = self._request("POST", "/api/v1/agents", json={"name": name})
        return {key: value for key, value in created.items() if key != "api_key"}

    def list_projects(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/v1/projects")

    def create_project(self, name: str, description: str) -> dict[str, Any]:
        return self._request(
            "POST", "/api/v1/projects", json={"name": name, "description": description}
        )

    def list_posts(self, project_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/api/v1/projects/{project_id}/posts")

    def search_posts(
        self, *, project_id: str, tag: str | None = None, query: str = ""
    ) -> list[dict[str, Any]]:
        params = {"q": query, "project_id": project_id}
        if tag is not None:
            params["tag"] = tag
        return self._request("GET", "/api/v1/search", params=params)

    def create_post(
        self, project_id: str, *, title: str, content: str, tags: list[str]
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/projects/{project_id}/posts",
            json={"title": title, "content": content, "type": "plan", "tags": tags},
        )

    def get_post(self, post_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/posts/{post_id}")

    def update_post(self, post_id: str, **changes: Any) -> dict[str, Any]:
        return self._request("PATCH", f"/api/v1/posts/{post_id}", json=changes)

    def create_comment(self, post_id: str, content: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/api/v1/posts/{post_id}/comments", json={"content": content}
        )

    def list_comments(self, post_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/api/v1/posts/{post_id}/comments")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
