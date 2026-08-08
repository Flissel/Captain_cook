"""Digest-bound loading for production runtime adapter ports."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agenten.agent_runtime.ports import (
    ArtifactPort,
    CodexExecutionPort,
    HermesPlannerPort,
)

if TYPE_CHECKING:
    from agenten.agent_runtime.runtime_entrypoint import RuntimeEntrypointSettings


_MANIFEST_SCHEMA = "captain.runtime-adapters.v1"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class RuntimeAdapterManifestError(ValueError):
    """A runtime adapter manifest or its referenced module is not trustworthy."""


class RuntimeAdapterManifest(BaseModel):
    """Immutable description of one digest-bound adapter factory module."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_name: Literal["captain.runtime-adapters.v1"] = Field(alias="schema")
    module_path: str = Field(min_length=1)
    factory_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    module_sha256: str = Field(pattern=_SHA256_PATTERN)


@dataclass(frozen=True, slots=True)
class RuntimeAdapterContext:
    """Non-authoritative local context supplied to the verified factory."""

    repository_root: Path
    artifact_root: Path
    environ: Mapping[str, str] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class RuntimeAdapterBinding:
    hermes: HermesPlannerPort
    codex: CodexExecutionPort
    artifacts: ArtifactPort


@dataclass(frozen=True, slots=True)
class RuntimeBootstrap:
    settings: "RuntimeEntrypointSettings"
    binding: RuntimeAdapterBinding


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_required_file(path: Path, *, description: str) -> bytes:
    try:
        if not path.is_file():
            raise RuntimeAdapterManifestError(f"{description} file is missing")
        return path.read_bytes()
    except RuntimeAdapterManifestError:
        raise
    except OSError:
        raise RuntimeAdapterManifestError(f"{description} file could not be read") from None


def _require_sha256(value: str, *, description: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise RuntimeAdapterManifestError(f"{description} digest is invalid")
    return value


def _resolve_scoped_path(
    value: str | Path,
    *,
    repository_root: Path,
    description: str,
) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = repository_root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        raise RuntimeAdapterManifestError(f"{description} path could not be resolved") from None
    runtime_root = (repository_root / ".captain-cook" / "runtime-adapters").resolve(
        strict=False
    )
    if not (
        resolved.is_relative_to(repository_root)
        or resolved.is_relative_to(runtime_root)
    ):
        raise RuntimeAdapterManifestError(
            f"{description} path is outside the allowed repository/runtime roots"
        )
    return resolved


def _parse_manifest(content: bytes) -> RuntimeAdapterManifest:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeAdapterManifestError("runtime adapter manifest is invalid") from None
    if not isinstance(document, dict) or document.get("schema") != _MANIFEST_SCHEMA:
        raise RuntimeAdapterManifestError("unsupported runtime adapter manifest schema")
    try:
        return RuntimeAdapterManifest.model_validate(document)
    except ValidationError:
        raise RuntimeAdapterManifestError("runtime adapter manifest is invalid") from None


def _load_verified_module(path: Path, content: bytes, digest: str) -> ModuleType:
    path_identity = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    module_name = f"_captain_runtime_adapters_{digest}_{path_identity}"
    module = ModuleType(module_name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[module_name] = module
    try:
        code = compile(content, str(path), "exec", dont_inherit=True)
        exec(code, module.__dict__)
    except Exception:
        sys.modules.pop(module_name, None)
        raise RuntimeAdapterManifestError("runtime adapter module import failed") from None
    return module


def _validate_binding(value: object) -> RuntimeAdapterBinding:
    if not isinstance(value, RuntimeAdapterBinding):
        raise RuntimeAdapterManifestError(
            "runtime adapter factory returned an invalid binding"
        )
    required_ports = (
        ("HermesPlannerPort", value.hermes, HermesPlannerPort),
        ("CodexExecutionPort", value.codex, CodexExecutionPort),
        ("ArtifactPort", value.artifacts, ArtifactPort),
    )
    for name, adapter, port in required_ports:
        if not isinstance(adapter, port):
            raise RuntimeAdapterManifestError(
                f"runtime adapter binding does not implement {name}"
            )
    return value


def load_runtime_adapters(
    manifest_path: str | Path,
    *,
    expected_sha256: str,
    repository_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeAdapterBinding:
    """Validate exact manifest/module bytes, then invoke and verify their factory."""

    requested_manifest = Path(manifest_path)
    if repository_root is None:
        requested_manifest = requested_manifest.resolve(strict=False)
        root = requested_manifest.parent
    else:
        root = repository_root.resolve(strict=False)
    resolved_manifest = _resolve_scoped_path(
        requested_manifest,
        repository_root=root,
        description="runtime adapter manifest",
    )
    manifest_content = _read_required_file(
        resolved_manifest,
        description="runtime adapter manifest",
    )
    expected_manifest_digest = _require_sha256(
        expected_sha256,
        description="runtime adapter manifest",
    )
    if not hmac.compare_digest(_sha256(manifest_content), expected_manifest_digest):
        raise RuntimeAdapterManifestError("runtime adapter manifest digest mismatch")

    manifest = _parse_manifest(manifest_content)
    module_path = _resolve_scoped_path(
        manifest.module_path,
        repository_root=root,
        description="runtime adapter module",
    )
    module_content = _read_required_file(
        module_path,
        description="runtime adapter module",
    )
    if not hmac.compare_digest(_sha256(module_content), manifest.module_sha256):
        raise RuntimeAdapterManifestError("runtime adapter module digest mismatch")

    module = _load_verified_module(module_path, module_content, manifest.module_sha256)
    factory = getattr(module, manifest.factory_name, None)
    if not callable(factory):
        raise RuntimeAdapterManifestError("runtime adapter factory is missing")
    source = os.environ if environ is None else environ
    context = RuntimeAdapterContext(
        repository_root=root,
        artifact_root=root / ".captain-cook" / "artifacts" / "sha256",
        environ=MappingProxyType(dict(source)),
    )
    try:
        binding = factory(context)
    except Exception:
        raise RuntimeAdapterManifestError("runtime adapter factory failed") from None
    return _validate_binding(binding)


def load_runtime_adapters_from_env(
    *,
    repository_root: Path,
    environ: Mapping[str, str] | None = None,
) -> RuntimeAdapterBinding:
    """Load the digest-bound adapter binding named by the process environment."""

    source = os.environ if environ is None else environ
    required = (
        "CAPTAIN_RUNTIME_ADAPTER_MANIFEST",
        "CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256",
    )
    missing = [
        name
        for name in required
        if not isinstance(source.get(name), str) or not source[name].strip()
    ]
    if missing:
        raise RuntimeAdapterManifestError(
            f"missing required runtime adapter settings: {', '.join(missing)}"
        )
    return load_runtime_adapters(
        source["CAPTAIN_RUNTIME_ADAPTER_MANIFEST"],
        expected_sha256=source["CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256"],
        repository_root=repository_root,
        environ=source,
    )
