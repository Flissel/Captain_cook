"""Frozen contracts for generic production authority assemblies.

An authority assembly binds one parsed repository-owned ``TO_BE_BUILT.md``
request to exactly one digest-pinned adapter per authority role. The
assembly is deterministic: the same input bytes and adapter set always
produce the same assembly ID and byte-identical canonical JSON, so no ID
is ever invented outside the content it names.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agenten.agent_factory.input_contracts import FactoryInputDocumentV2

SHA256_PATTERN = r"^[a-f0-9]{64}$"
REPOSITORY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$"
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"

AuthorityAdapterRole = Literal["captain", "gateway", "runtime", "minibook", "n8n"]
ADAPTER_ROLE_ORDER: tuple[AuthorityAdapterRole, ...] = (
    "captain",
    "gateway",
    "runtime",
    "minibook",
    "n8n",
)


class AuthorityAssemblyError(ValueError):
    """Raised when an authority assembly cannot be constructed fail-closed."""


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthorityAdapterRefV1(_FrozenContract):
    role: AuthorityAdapterRole
    artifact_uri: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    version: str = Field(pattern=VERSION_PATTERN)

    @field_validator("artifact_uri")
    @classmethod
    def require_opaque_artifact_uri(cls, value: str) -> str:
        if not value.startswith("artifact://"):
            raise ValueError("adapter artifact URI must use the artifact:// scheme")
        return value


class AuthorityAssemblyV1(_FrozenContract):
    schema_name: Literal["captain.authority-assembly.v1"] = (
        "captain.authority-assembly.v1"
    )
    assembly_id: str = Field(pattern=SHA256_PATTERN)
    source_repository: str = Field(pattern=REPOSITORY_PATTERN)
    input_sha256: str = Field(pattern=SHA256_PATTERN)
    adapters: tuple[AuthorityAdapterRefV1, ...] = Field(
        min_length=len(ADAPTER_ROLE_ORDER), max_length=len(ADAPTER_ROLE_ORDER)
    )


def _canonical_payload(assembly_body: dict[str, object]) -> bytes:
    return json.dumps(
        assembly_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _ordered_adapters(
    adapters: Iterable[AuthorityAdapterRefV1],
) -> tuple[AuthorityAdapterRefV1, ...]:
    by_role: dict[AuthorityAdapterRole, AuthorityAdapterRefV1] = {}
    for adapter in adapters:
        if adapter.role in by_role and by_role[adapter.role] != adapter:
            raise AuthorityAssemblyError(
                f"conflicting adapters for role {adapter.role}"
            )
        by_role[adapter.role] = adapter
    missing = [role for role in ADAPTER_ROLE_ORDER if role not in by_role]
    if missing:
        raise AuthorityAssemblyError(f"missing adapter for role {missing[0]}")
    return tuple(by_role[role] for role in ADAPTER_ROLE_ORDER)


def assemble_production_authority(
    document: FactoryInputDocumentV2,
    adapters: Iterable[AuthorityAdapterRefV1],
    *,
    source_repository: str,
) -> AuthorityAssemblyV1:
    ordered = _ordered_adapters(adapters)
    body = {
        "schema_name": "captain.authority-assembly.v1",
        "source_repository": source_repository,
        "input_sha256": document.input_ref.sha256,
        "adapters": [adapter.model_dump(mode="json") for adapter in ordered],
    }
    assembly_id = hashlib.sha256(_canonical_payload(body)).hexdigest()
    try:
        return AuthorityAssemblyV1(
            assembly_id=assembly_id,
            source_repository=source_repository,
            input_sha256=document.input_ref.sha256,
            adapters=ordered,
        )
    except ValueError as error:
        raise AuthorityAssemblyError(str(error)) from error


def canonical_assembly_bytes(assembly: AuthorityAssemblyV1) -> bytes:
    return _canonical_payload(assembly.model_dump(mode="json"))
