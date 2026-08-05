"""Immutable metadata for content-addressed Gitea template releases."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


_REPOSITORY_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_PINNED_REVISION = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_SHA256 = r"^[0-9a-f]{64}$"


def validate_safe_https_url(value: str, *, label: str) -> str:
    """Reject URL forms that can hide credentials or change request semantics."""

    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} must not contain C0 control characters")
    if value != value.strip():
        raise ValueError(f"{label} must be a canonical HTTPS URL")
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        raise ValueError(f"{label} must be a canonical HTTPS URL") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.hostname.encode("idna").decode("ascii") != parts.hostname
        or port is not None and not 1 <= port <= 65535
    ):
        raise ValueError(f"{label} must be a safe HTTPS URL")
    return value


class GiteaTemplateReleaseV1(BaseModel):
    """One immutable Gitea file release selected by repository commit digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(min_length=3, max_length=257)
    revision: str = Field(pattern=_PINNED_REVISION)
    path: str = Field(min_length=1, max_length=1024)
    contents_url: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=_SHA256)

    @field_validator(
        "repository",
        "revision",
        "path",
        "contents_url",
        "sha256",
        mode="before",
    )
    @classmethod
    def reject_c0_control_characters(cls, value: object) -> object:
        if isinstance(value, str) and any(ord(character) < 32 for character in value):
            raise ValueError("release metadata must not contain C0 control characters")
        return value

    @field_validator("repository")
    @classmethod
    def require_owner_and_repository(cls, value: str) -> str:
        components = value.split("/")
        if len(components) != 2 or any(
            component in {".", ".."} or not _REPOSITORY_COMPONENT.fullmatch(component)
            for component in components
        ):
            raise ValueError("repository must be a safe owner/name pair")
        return value

    @field_validator("path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        components = value.split("/")
        if (
            value != value.strip()
            or value.startswith("/")
            or "\\" in value
            or any(
                component in {"", ".", ".."} or not _PATH_COMPONENT.fullmatch(component)
                for component in components
            )
        ):
            raise ValueError("template path must be a safe relative path")
        return value

    @field_validator("contents_url")
    @classmethod
    def require_safe_contents_url(cls, value: str) -> str:
        validate_safe_https_url(value, label="contents_url")
        if urlsplit(value).path in {"", "/"}:
            raise ValueError("contents_url must identify a template")
        return value
