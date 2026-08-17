"""Attested public tool registry for generic capability benchmark execution."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit

import httpx

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agenten.agent_runtime.production_bootstrap import (
    RuntimeAdapterManifestError,
    load_verified_repository_module,
    read_verified_repository_bytes,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


class GenericToolRegistryConfigurationError(ValueError):
    """The operator registry cannot authorize a generic tool effect."""


class GenericToolAdapterDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tool_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    adapter_kind: str = Field(pattern=r"^(local_readonly|http_readonly|n8n)$")
    schema_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    schema_version: int = Field(ge=1, strict=True)
    allowed_operation: str = Field(pattern=r"^(read|draft|discover)$")
    credential_env_name: str | None = Field(
        default=None, pattern=r"^CAPTAIN_[A-Z0-9_]{2,120}$"
    )
    endpoint: str | None = None
    endpoint_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    implementation_path: str | None = None
    implementation_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def require_kind_contract(self) -> "GenericToolAdapterDescriptor":
        if self.adapter_kind == "local_readonly":
            if (
                self.allowed_operation != "read"
                or self.credential_env_name is not None
                or self.endpoint is not None
                or self.endpoint_sha256 is not None
                or self.implementation_path is None
                or self.implementation_sha256 is None
            ):
                raise ValueError("local readonly tool binding is invalid")
        else:
            if (
                self.credential_env_name is None
                or self.endpoint is None
                or self.endpoint_sha256 is None
                or self.implementation_path is not None
                or self.implementation_sha256 is not None
            ):
                raise ValueError("remote tool binding is invalid")
            parsed = urlsplit(self.endpoint)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or not parsed.path.startswith("/v1/")
            ):
                raise ValueError("remote tool endpoint must be an exact loopback v1 route")
            if hashlib.sha256(self.endpoint.encode()).hexdigest() != self.endpoint_sha256:
                raise ValueError("remote tool endpoint digest does not match")
        return self


class GenericToolRegistryManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_name: str = Field(
        alias="schema", serialization_alias="schema",
        pattern=r"^captain\.generic-tool-registry\.v1$",
    )
    entries: tuple[GenericToolAdapterDescriptor, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_tools(self) -> "GenericToolRegistryManifest":
        keys = tuple(item.tool_key for item in self.entries)
        if len(keys) != len(set(keys)) or keys != tuple(sorted(keys)):
            raise ValueError("generic tool entries must be unique and sorted")
        return self


class VerifiedGenericToolRegistry:
    """Execute only modules loaded from the exact verified bytes."""

    def __init__(
        self, *, digest: str, manifest: GenericToolRegistryManifest,
        modules: Mapping[str, ModuleType], credentials: Mapping[str, str],
    ) -> None:
        self.digest = digest
        self.manifest = manifest
        self._entries = {item.tool_key: item for item in manifest.entries}
        self._modules = dict(modules)
        self._credentials = dict(credentials)

    @property
    def tool_keys(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def require_declared(self, tool_keys: tuple[str, ...]) -> None:
        unknown = tuple(key for key in tool_keys if key not in self._entries)
        if unknown:
            raise GenericToolRegistryConfigurationError(
                "unknown generic tool adapter: " + ", ".join(unknown)
            )

    def operation_for(self, tool_key: str) -> str:
        entry = self._entries.get(tool_key)
        if entry is None:
            raise GenericToolRegistryConfigurationError(
                f"unknown generic tool adapter: {tool_key}"
            )
        return entry.allowed_operation

    def execute(
        self, *, tool_key: str, operation: str,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        entry = self._entries.get(tool_key)
        if entry is None:
            raise GenericToolRegistryConfigurationError(
                f"unknown generic tool adapter: {tool_key}"
            )
        if operation != entry.allowed_operation:
            raise GenericToolRegistryConfigurationError(
                "generic tool operation is not authorized"
            )
        if entry.adapter_kind != "local_readonly":
            return self._execute_remote(entry, arguments)
        function = getattr(self._modules[tool_key], "execute_generic_tool", None)
        if not callable(function):
            raise GenericToolRegistryConfigurationError(
                "generic tool adapter constructor is invalid"
            )
        try:
            result = function({
                "schema": "captain.generic-tool-execution.v1",
                "tool_key": tool_key,
                "operation": operation,
                "arguments": dict(arguments),
            })
        except Exception:
            raise GenericToolRegistryConfigurationError(
                "generic tool adapter execution failed"
            ) from None
        expected_schema = f"{entry.schema_name}.v{entry.schema_version}"
        if not isinstance(result, dict) or result.get("schema") != expected_schema:
            raise GenericToolRegistryConfigurationError(
                "generic tool adapter returned the wrong schema"
            )
        return dict(result)

    def _execute_remote(
        self, entry: GenericToolAdapterDescriptor,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        assert entry.endpoint is not None
        assert entry.endpoint_sha256 is not None
        token = self._credentials.get(entry.tool_key, "")
        if len(token) < 16:
            raise GenericToolRegistryConfigurationError(
                "generic tool credential is unavailable"
            )
        request = {
            "schema": "captain.generic-tool-execution.v1",
            "tool_key": entry.tool_key,
            "operation": entry.allowed_operation,
            "endpoint_sha256": entry.endpoint_sha256,
            "registry_sha256": self.digest,
            "arguments": dict(arguments),
        }
        try:
            with httpx.Client(
                timeout=httpx.Timeout(10.0), follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.post(
                    entry.endpoint,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    content=json.dumps(
                        request, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode(),
                )
            if response.status_code != 200 or len(response.content) > 1024 * 1024:
                raise ValueError("status")
            result = response.json()
        except Exception:
            raise GenericToolRegistryConfigurationError(
                "remote generic tool execution failed"
            ) from None
        expected_schema = f"{entry.schema_name}.v{entry.schema_version}"
        if (
            not isinstance(result, dict)
            or set(result) != {
                "schema", "tool_key", "operation", "endpoint_sha256", "result"
            }
            or result.get("schema") != expected_schema
            or result.get("tool_key") != entry.tool_key
            or result.get("operation") != entry.allowed_operation
            or result.get("endpoint_sha256") != entry.endpoint_sha256
            or not isinstance(result.get("result"), dict)
        ):
            raise GenericToolRegistryConfigurationError(
                "remote generic tool returned the wrong typed readback"
            )
        return dict(result)


def load_verified_generic_tool_registry(
    *, repository_root: Path, relative_path: str, expected_sha256: str,
    environment: Mapping[str, str],
) -> VerifiedGenericToolRegistry:
    """Single-open and HEAD-attest the registry and every local adapter."""

    root = repository_root.resolve(strict=True)
    try:
        revision = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=root, check=True,
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if _REVISION.fullmatch(revision) is None or _SHA256.fullmatch(expected_sha256) is None:
            raise ValueError("identity")
        _, body = read_verified_repository_bytes(
            relative_path, expected_sha256=expected_sha256,
            expected_repository_revision=revision, repository_root=root,
        )
        manifest = GenericToolRegistryManifest.model_validate_json(body)
        modules: dict[str, ModuleType] = {}
        credentials: dict[str, str] = {}
        for entry in manifest.entries:
            if entry.adapter_kind == "local_readonly":
                assert entry.implementation_path is not None
                assert entry.implementation_sha256 is not None
                modules[entry.tool_key] = load_verified_repository_module(
                    entry.implementation_path,
                    expected_sha256=entry.implementation_sha256,
                    repository_root=root,
                )
            else:
                assert entry.credential_env_name is not None
                value = environment.get(entry.credential_env_name, "")
                if len(value) < 16:
                    raise ValueError("credential")
                credentials[entry.tool_key] = str(value)
    except (
        OSError, subprocess.SubprocessError, RuntimeAdapterManifestError,
        ValidationError, TypeError, ValueError,
    ):
        raise GenericToolRegistryConfigurationError(
            "generic tool registry authority is invalid"
        ) from None
    return VerifiedGenericToolRegistry(
        digest=hashlib.sha256(body).hexdigest(), manifest=manifest,
        modules=modules, credentials=credentials,
    )


__all__ = [
    "GenericToolAdapterDescriptor", "GenericToolRegistryConfigurationError",
    "GenericToolRegistryManifest", "VerifiedGenericToolRegistry",
    "load_verified_generic_tool_registry",
]
