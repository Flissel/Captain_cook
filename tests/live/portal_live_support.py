"""Fail-closed helpers for the explicitly opted-in portal live gate."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import UUID

import httpx


OPT_IN_NAME = "CAPTAIN_PORTAL_LIVE_E2E"
LOOPBACK_NAME = "CAPTAIN_PORTAL_LIVE_ALLOW_LOOPBACK"
MAX_RESPONSE_BYTES = 128 * 1024
REQUEST_TIMEOUT_SECONDS = 5.0

REQUIRED_ENVIRONMENT = (
    "CAPTAIN_PORTAL_LIVE_BASE_URL",
    "CAPTAIN_PORTAL_LIVE_ORG_A_ACCESS_TOKEN",
    "CAPTAIN_PORTAL_LIVE_ORG_B_ACCESS_TOKEN",
    "CAPTAIN_PORTAL_LIVE_ORG_A_ID",
    "CAPTAIN_PORTAL_LIVE_ORG_B_ID",
    "CAPTAIN_PORTAL_LIVE_CAPTAIN_TOKEN",
    "CAPTAIN_PORTAL_LIVE_JOB_ID",
    "CAPTAIN_PORTAL_LIVE_CORRELATION_ID",
    "CAPTAIN_PORTAL_LIVE_BEARER_ALIAS",
    "CAPTAIN_PORTAL_LIVE_BEARER_CREDENTIAL_ID",
    "CAPTAIN_PORTAL_LIVE_OAUTH_ALIAS",
    "CAPTAIN_PORTAL_LIVE_OAUTH_CREDENTIAL_ID",
    "CAPTAIN_PORTAL_LIVE_OAUTH_CLIENT_ID",
    "CAPTAIN_PORTAL_LIVE_OAUTH_AUTH_URL",
    "CAPTAIN_PORTAL_LIVE_OAUTH_CALLBACK_URL",
    "CAPTAIN_PORTAL_LIVE_N8N_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_GITEA_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_SUPABASE_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_MINIBOOK_HEALTH_URL",
)


class PortalLiveConfigurationError(ValueError):
    """Raised before networking when the disposable live group is unsafe."""


class PortalLiveResponseError(RuntimeError):
    """Fixed-message response error which never includes a provider body."""


@dataclass(frozen=True)
class PortalLiveConfig:
    base_url: str
    org_a_access_token: str
    org_b_access_token: str
    org_a_id: str
    org_b_id: str
    captain_token: str
    job_id: UUID
    correlation_id: UUID
    bearer_alias: str
    bearer_credential_id: str
    oauth_alias: str
    oauth_credential_id: str
    oauth_client_id: str
    oauth_auth_url: str
    oauth_callback_url: str
    n8n_health_url: str
    gitea_health_url: str
    supabase_health_url: str
    minibook_health_url: str

    @classmethod
    def from_environment(cls) -> "PortalLiveConfig":
        if os.environ.get(OPT_IN_NAME) != "1":
            raise PortalLiveConfigurationError(
                f"set {OPT_IN_NAME}=1 to authorize the disposable portal live gate"
            )
        missing = tuple(name for name in REQUIRED_ENVIRONMENT if not os.environ.get(name))
        if missing:
            raise PortalLiveConfigurationError(
                "missing complete portal live group: " + ", ".join(missing)
            )
        values = {name: os.environ[name] for name in REQUIRED_ENVIRONMENT}
        allow_loopback = os.environ.get(LOOPBACK_NAME) == "1"
        url_names = (
            "CAPTAIN_PORTAL_LIVE_BASE_URL",
            "CAPTAIN_PORTAL_LIVE_OAUTH_AUTH_URL",
            "CAPTAIN_PORTAL_LIVE_OAUTH_CALLBACK_URL",
            "CAPTAIN_PORTAL_LIVE_N8N_HEALTH_URL",
            "CAPTAIN_PORTAL_LIVE_GITEA_HEALTH_URL",
            "CAPTAIN_PORTAL_LIVE_SUPABASE_HEALTH_URL",
            "CAPTAIN_PORTAL_LIVE_MINIBOOK_HEALTH_URL",
        )
        for name in url_names:
            _validate_safe_url(values[name], name=name, allow_loopback=allow_loopback)
        if values["CAPTAIN_PORTAL_LIVE_ORG_A_ACCESS_TOKEN"] == values[
            "CAPTAIN_PORTAL_LIVE_ORG_B_ACCESS_TOKEN"
        ]:
            raise PortalLiveConfigurationError("portal live user tokens must be distinct")
        if values["CAPTAIN_PORTAL_LIVE_ORG_A_ID"] == values[
            "CAPTAIN_PORTAL_LIVE_ORG_B_ID"
        ]:
            raise PortalLiveConfigurationError("portal live organizations must be distinct")
        try:
            job_id = UUID(values["CAPTAIN_PORTAL_LIVE_JOB_ID"])
            correlation_id = UUID(values["CAPTAIN_PORTAL_LIVE_CORRELATION_ID"])
        except ValueError as error:
            raise PortalLiveConfigurationError(
                "portal live job and correlation identifiers must be UUIDs"
            ) from error
        return cls(
            base_url=values["CAPTAIN_PORTAL_LIVE_BASE_URL"].rstrip("/"),
            org_a_access_token=values["CAPTAIN_PORTAL_LIVE_ORG_A_ACCESS_TOKEN"],
            org_b_access_token=values["CAPTAIN_PORTAL_LIVE_ORG_B_ACCESS_TOKEN"],
            org_a_id=values["CAPTAIN_PORTAL_LIVE_ORG_A_ID"],
            org_b_id=values["CAPTAIN_PORTAL_LIVE_ORG_B_ID"],
            captain_token=values["CAPTAIN_PORTAL_LIVE_CAPTAIN_TOKEN"],
            job_id=job_id,
            correlation_id=correlation_id,
            bearer_alias=values["CAPTAIN_PORTAL_LIVE_BEARER_ALIAS"],
            bearer_credential_id=values["CAPTAIN_PORTAL_LIVE_BEARER_CREDENTIAL_ID"],
            oauth_alias=values["CAPTAIN_PORTAL_LIVE_OAUTH_ALIAS"],
            oauth_credential_id=values["CAPTAIN_PORTAL_LIVE_OAUTH_CREDENTIAL_ID"],
            oauth_client_id=values["CAPTAIN_PORTAL_LIVE_OAUTH_CLIENT_ID"],
            oauth_auth_url=values["CAPTAIN_PORTAL_LIVE_OAUTH_AUTH_URL"],
            oauth_callback_url=values["CAPTAIN_PORTAL_LIVE_OAUTH_CALLBACK_URL"],
            n8n_health_url=values["CAPTAIN_PORTAL_LIVE_N8N_HEALTH_URL"],
            gitea_health_url=values["CAPTAIN_PORTAL_LIVE_GITEA_HEALTH_URL"],
            supabase_health_url=values["CAPTAIN_PORTAL_LIVE_SUPABASE_HEALTH_URL"],
            minibook_health_url=values["CAPTAIN_PORTAL_LIVE_MINIBOOK_HEALTH_URL"],
        )


@dataclass(frozen=True)
class RedactedResponse:
    status_code: int
    payload: Mapping[str, Any]


class PortalLiveClient:
    """Bounded HTTP client which never exposes response bodies in errors."""

    def __init__(self, config: PortalLiveConfig) -> None:
        self._config = config
        self._client = httpx.Client(
            base_url=config.base_url,
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    def close(self) -> None:
        self._client.close()

    def portal(
        self,
        method: str,
        path: str,
        *,
        access_token: str,
        payload: Mapping[str, Any] | None = None,
    ) -> RedactedResponse:
        return self._request(
            self._client,
            method,
            path,
            authorization=access_token,
            payload=payload,
        )

    def health(self, url: str) -> int:
        with httpx.Client(
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as client:
            return self._request(client, "GET", url, require_json=False).status_code

    @staticmethod
    def _request(
        client: httpx.Client,
        method: str,
        url: str,
        *,
        authorization: str | None = None,
        payload: Mapping[str, Any] | None = None,
        require_json: bool = True,
    ) -> RedactedResponse:
        headers = {"Accept": "application/json"}
        if authorization is not None:
            headers["Authorization"] = f"Bearer {authorization}"
        try:
            with client.stream(method, url, headers=headers, json=payload) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise PortalLiveResponseError(
                            "portal live response exceeded the size limit"
                        )
                decoded: object = {}
                if body and require_json:
                    try:
                        decoded = json.loads(body)
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise PortalLiveResponseError(
                            "portal live response was not a JSON object"
                        ) from error
        except httpx.HTTPError as error:
            raise PortalLiveResponseError("portal live request failed") from error
        if require_json and not isinstance(decoded, dict):
            raise PortalLiveResponseError("portal live response was not a JSON object")
        return RedactedResponse(
            response.status_code,
            decoded if isinstance(decoded, dict) else {},
        )


def require_status(response: RedactedResponse, *expected: int) -> Mapping[str, Any]:
    if response.status_code not in expected:
        raise PortalLiveResponseError("portal live response had an unexpected status")
    return response.payload


def require_secret_free_surface(payload: Mapping[str, Any]) -> None:
    """Reject secret-shaped keys while allowing sanitized credential IDs."""

    forbidden = ("token", "password", "secret", "authorization", "oauth_code")

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                lowered = str(key).lower()
                if any(term in lowered for term in forbidden):
                    raise PortalLiveResponseError(
                        "portal live surface contained a secret-shaped field"
                    )
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(payload)


def _validate_safe_url(value: str, *, name: str, allow_loopback: bool) -> None:
    parsed = urlsplit(value)
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    allowed_scheme = parsed.scheme == "https" or (
        allow_loopback and loopback and parsed.scheme == "http"
    )
    if (
        not allowed_scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise PortalLiveConfigurationError(f"{name} must be a safe HTTPS URL")
