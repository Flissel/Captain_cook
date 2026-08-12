"""Digest-pinned Captain/Gateway/Runtime/Minibook/n8n authority adapter bundle.

The bundle pins exactly one adapter artifact per authority role by SHA-256,
mirroring the digest verification already used for Gitea templates. Loading
goes through the shared TOCTOU-safe single-open read; a digest mismatch
names only the failing role, never any content.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agenten.agent_factory.authority_assembly_contracts import (
    ADAPTER_ROLE_ORDER,
    AuthorityAdapterRefV1,
)
from agenten.agent_factory.single_open import (
    SingleOpenError,
    sha256_of_verified_read,
)

MAX_BUNDLE_BYTES = 1024 * 1024
MAX_ADAPTER_ARTIFACT_BYTES = 16 * 1024 * 1024

BUNDLE_SOURCE_PATHS: dict[str, Path] = {
    "captain": Path("agenten/orchestration/pipeline.py"),
    "gateway": Path("gateway/app.py"),
    "runtime": Path("agenten/agent_runtime/http_server.py"),
    "minibook": Path("scripts/rebuild_minibook_projection.py"),
    "n8n": Path("agenten/agent_runtime/n8n_mcp_broker_server.py"),
}


class AuthorityAdapterBundleError(ValueError):
    """Raised when an adapter bundle cannot be built or verified fail-closed."""


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PinnedAuthorityAdapterV1(_FrozenContract):
    ref: AuthorityAdapterRefV1
    source_path: str = Field(min_length=1)


class AuthorityAdapterBundleV1(_FrozenContract):
    schema_name: Literal["captain.authority-adapter-bundle.v1"] = (
        "captain.authority-adapter-bundle.v1"
    )
    adapters: tuple[PinnedAuthorityAdapterV1, ...] = Field(
        min_length=1, max_length=len(ADAPTER_ROLE_ORDER)
    )


def _canonical_bundle_bytes(bundle: AuthorityAdapterBundleV1) -> bytes:
    return json.dumps(
        bundle.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_complete_roles(
    adapters: tuple[PinnedAuthorityAdapterV1, ...],
) -> None:
    seen = [entry.ref.role for entry in adapters]
    for index, role in enumerate(ADAPTER_ROLE_ORDER):
        if index >= len(seen) or seen[index] != role:
            raise AuthorityAdapterBundleError(
                f"bundle is missing or misorders the {role} adapter"
            )


def pin_adapter_bundle(
    source_paths: dict[str, Path],
) -> AuthorityAdapterBundleV1:
    entries: list[PinnedAuthorityAdapterV1] = []
    for role in ADAPTER_ROLE_ORDER:
        source = source_paths.get(role)
        if source is None:
            raise AuthorityAdapterBundleError(
                f"bundle is missing or misorders the {role} adapter"
            )
        try:
            _, digest = sha256_of_verified_read(
                source, maximum_size=MAX_ADAPTER_ARTIFACT_BYTES
            )
        except SingleOpenError as error:
            raise AuthorityAdapterBundleError(
                f"cannot pin the {role} adapter fail-closed"
            ) from error
        entries.append(
            PinnedAuthorityAdapterV1(
                ref=AuthorityAdapterRefV1(
                    role=role,
                    artifact_uri=f"artifact://authority/{role}",
                    sha256=digest,
                    version="v1",
                ),
                source_path=source.as_posix(),
            )
        )
    return AuthorityAdapterBundleV1(adapters=tuple(entries))


def write_adapter_bundle(bundle: AuthorityAdapterBundleV1, target: Path) -> None:
    target.write_bytes(_canonical_bundle_bytes(bundle) + b"\n")


def load_adapter_bundle(
    source: Path,
) -> tuple[AuthorityAdapterBundleV1, str]:
    try:
        body, bundle_sha256 = sha256_of_verified_read(
            source, maximum_size=MAX_BUNDLE_BYTES
        )
    except SingleOpenError as error:
        raise AuthorityAdapterBundleError(
            "cannot load the adapter bundle fail-closed"
        ) from error
    try:
        bundle = AuthorityAdapterBundleV1.model_validate(json.loads(body))
    except (json.JSONDecodeError, ValidationError) as error:
        raise AuthorityAdapterBundleError(
            "adapter bundle payload is invalid"
        ) from error
    _require_complete_roles(bundle.adapters)
    for entry in bundle.adapters:
        source_path = Path(entry.source_path)
        try:
            _, actual = sha256_of_verified_read(
                source_path, maximum_size=MAX_ADAPTER_ARTIFACT_BYTES
            )
        except SingleOpenError as error:
            raise AuthorityAdapterBundleError(
                f"cannot verify the {entry.ref.role} adapter fail-closed"
            ) from error
        if actual != entry.ref.sha256:
            raise AuthorityAdapterBundleError(
                f"adapter digest mismatch for role {entry.ref.role}"
            )
    return bundle, bundle_sha256
