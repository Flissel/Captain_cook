"""Digest-bound loading for production runtime adapter ports."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import TYPE_CHECKING, BinaryIO, Literal

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
_REVISION_PATTERN = r"^[0-9a-f]{40}$"


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


def _canonical_repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _normalize_requested_path(value: str | Path, *, repository_root: Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = repository_root / candidate
    try:
        return Path(os.path.abspath(candidate))
    except OSError:
        raise RuntimeAdapterManifestError(
            "runtime adapter path could not be normalized"
        ) from None


def _require_sha256(value: str, *, description: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise RuntimeAdapterManifestError(f"{description} digest is invalid")
    return value


def _require_authorized_path(
    path: Path,
    *,
    repository_root: Path,
    description: str,
) -> Path:
    runtime_root = repository_root / ".captain-cook" / "runtime-adapters"
    if not (
        path.is_relative_to(repository_root)
        or path.is_relative_to(runtime_root)
    ):
        raise RuntimeAdapterManifestError(
            f"{description} path is outside the allowed repository/runtime roots"
        )
    return path


def _windows_final_path_for_open_file(stream: BinaryIO) -> Path:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    get_final_path = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    ).GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_final_path(
        msvcrt.get_osfhandle(stream.fileno()),
        buffer,
        len(buffer),
        0,
    )
    if length == 0 or length >= len(buffer):
        raise RuntimeAdapterManifestError(
            "runtime adapter file final path could not be verified"
        )
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _final_path_for_open_file(
    stream: BinaryIO,
    *,
    requested_path: Path,
) -> Path:
    if os.name == "nt":
        return _windows_final_path_for_open_file(stream)
    proc_path = Path("/proc/self/fd") / str(stream.fileno())
    try:
        final_path = os.readlink(proc_path)
    except OSError:
        raise RuntimeAdapterManifestError(
            "runtime adapter file final path could not be verified"
        ) from None
    if final_path.endswith(" (deleted)"):
        raise RuntimeAdapterManifestError(
            "runtime adapter file final path could not be verified"
        )
    del requested_path
    return Path(final_path)


def _read_authorized_file(
    value: str | Path,
    *,
    repository_root: Path,
    description: str,
) -> tuple[Path, bytes]:
    requested_path = _normalize_requested_path(
        value,
        repository_root=repository_root,
    )
    _require_authorized_path(
        requested_path,
        repository_root=repository_root,
        description=description,
    )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(requested_path, flags)
    except FileNotFoundError:
        raise RuntimeAdapterManifestError(f"{description} file is missing") from None
    except OSError:
        raise RuntimeAdapterManifestError(
            f"{description} file could not be opened"
        ) from None

    try:
        stream = os.fdopen(descriptor, "rb", closefd=True)
    except Exception:
        os.close(descriptor)
        raise RuntimeAdapterManifestError(
            f"{description} file could not be opened"
        ) from None
    with stream:
        try:
            final_path = _final_path_for_open_file(
                stream,
                requested_path=requested_path,
            )
        except RuntimeAdapterManifestError:
            raise
        except Exception:
            raise RuntimeAdapterManifestError(
                f"{description} final path could not be verified"
            ) from None
        _require_authorized_path(
            final_path,
            repository_root=repository_root,
            description=description,
        )
        try:
            content = stream.read()
        except OSError:
            raise RuntimeAdapterManifestError(
                f"{description} file could not be read"
            ) from None
    return final_path, content


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


def _require_committed_repository_bytes(
    path: Path,
    content: bytes,
    *,
    repository_root: Path,
    expected_repository_revision: str | None = None,
) -> None:
    """Require the opened regular-file bytes to equal both HEAD and the index."""

    try:
        relative_path = path.relative_to(repository_root).as_posix()
    except ValueError:
        raise RuntimeAdapterManifestError(
            "runtime adapter module is not repository-owned"
        ) from None

    git_environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }

    git_marker = repository_root / ".git"
    try:
        if git_marker.is_dir():
            expected_git_dir = git_marker.resolve(strict=True)
        elif git_marker.is_file():
            git_marker_lines = git_marker.read_text(encoding="utf-8").splitlines()
            if len(git_marker_lines) != 1 or not git_marker_lines[0].startswith(
                "gitdir: "
            ):
                raise RuntimeAdapterManifestError(
                    "runtime adapter repository identity could not be verified"
                )
            expected_git_dir = Path(git_marker_lines[0].removeprefix("gitdir: "))
            if not expected_git_dir.is_absolute():
                expected_git_dir = repository_root / expected_git_dir
            expected_git_dir = expected_git_dir.resolve(strict=True)
        else:
            raise RuntimeAdapterManifestError(
                "runtime adapter repository identity could not be verified"
            )
    except (OSError, UnicodeError):
        raise RuntimeAdapterManifestError(
            "runtime adapter repository identity could not be verified"
        ) from None

    def git_output(
        *arguments: str,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> bytes:
        try:
            result = subprocess.run(
                ["git", "-C", str(repository_root), *arguments],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=git_environment,
            )
        except OSError:
            raise RuntimeAdapterManifestError(
                "runtime adapter module committed bytes could not be verified"
            ) from None
        if result.returncode not in allowed_returncodes:
            raise RuntimeAdapterManifestError(
                "runtime adapter module is not committed and repository-owned"
            )
        return result.stdout

    def resolved_git_path(output: bytes) -> Path:
        try:
            return Path(os.fsdecode(output.strip())).resolve(strict=True)
        except (OSError, ValueError):
            raise RuntimeAdapterManifestError(
                "runtime adapter repository identity could not be verified"
            ) from None

    actual_repository_root = resolved_git_path(
        git_output("rev-parse", "--show-toplevel")
    )
    actual_git_dir = resolved_git_path(
        git_output("rev-parse", "--absolute-git-dir")
    )
    if (
        actual_repository_root != repository_root
        or actual_git_dir != expected_git_dir
    ):
        raise RuntimeAdapterManifestError(
            "runtime adapter repository identity does not match "
            "the expected repository"
        )

    initial_revision_bytes = git_output("rev-parse", "HEAD").strip()
    try:
        initial_revision = initial_revision_bytes.decode("ascii")
    except UnicodeDecodeError:
        raise RuntimeAdapterManifestError(
            "runtime adapter repository revision could not be verified"
        ) from None
    if re.fullmatch(_REVISION_PATTERN, initial_revision) is None:
        raise RuntimeAdapterManifestError(
            "runtime adapter repository revision could not be verified"
        )
    if (
        expected_repository_revision is not None
        and not hmac.compare_digest(initial_revision, expected_repository_revision)
    ):
        raise RuntimeAdapterManifestError("repository revision mismatch")

    external_attributes = git_output(
        "config",
        "--get",
        "core.attributesfile",
        allowed_returncodes=(0, 1),
    )
    if external_attributes.strip():
        raise RuntimeAdapterManifestError(
            "runtime adapter external Git attributes are not allowed"
        )
    git_attributes_path = Path(
        os.fsdecode(git_output("rev-parse", "--git-path", "info/attributes").strip())
    )
    if not git_attributes_path.is_absolute():
        git_attributes_path = repository_root / git_attributes_path
    try:
        if git_attributes_path.is_file() and git_attributes_path.read_bytes().strip():
            raise RuntimeAdapterManifestError(
                "runtime adapter external Git attributes are not allowed"
            )
    except OSError:
        raise RuntimeAdapterManifestError(
            "runtime adapter Git attributes could not be verified"
        ) from None

    attribute_output = git_output(
        "check-attr",
        "-z",
        "filter",
        "working-tree-encoding",
        "ident",
        "--",
        relative_path,
    )
    attribute_parts = attribute_output.split(b"\0")
    if attribute_parts and attribute_parts[-1] == b"":
        attribute_parts.pop()
    if len(attribute_parts) % 3 != 0:
        raise RuntimeAdapterManifestError(
            "runtime adapter Git attributes could not be verified"
        )
    for index in range(0, len(attribute_parts), 3):
        attribute_name = attribute_parts[index + 1]
        attribute_value = attribute_parts[index + 2]
        if attribute_value not in {b"unspecified", b"unset"}:
            raise RuntimeAdapterManifestError(
                "runtime adapter Git attribute "
                f"{os.fsdecode(attribute_name)} is not allowed"
            )

    dirty_attributes = git_output(
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        ".gitattributes",
        ":(glob)**/.gitattributes",
    )
    if dirty_attributes.strip():
        raise RuntimeAdapterManifestError(
            "runtime adapter Git attributes are dirty"
        )

    head_content = git_output("show", f"{initial_revision}:{relative_path}")
    index_blob = git_output("rev-parse", f":{relative_path}").strip()
    if re.fullmatch(rb"[0-9a-f]{40,64}", index_blob) is None:
        raise RuntimeAdapterManifestError(
            "runtime adapter index identity could not be verified"
        )
    index_content = git_output("cat-file", "blob", os.fsdecode(index_blob))

    def normalize_eol(value: bytes) -> bytes:
        normalized = value.replace(b"\r\n", b"\n")
        if b"\r" in normalized:
            raise RuntimeAdapterManifestError(
                "runtime adapter module contains unsupported line endings"
            )
        return normalized

    normalized_content = normalize_eol(content)
    if not (
        hmac.compare_digest(normalized_content, normalize_eol(head_content))
        and hmac.compare_digest(normalized_content, normalize_eol(index_content))
    ):
        raise RuntimeAdapterManifestError(
            "runtime adapter module is dirty or differs from committed bytes"
        )
    final_revision = git_output("rev-parse", "HEAD").strip()
    final_index_blob = git_output("rev-parse", f":{relative_path}").strip()
    if not (
        hmac.compare_digest(final_revision, initial_revision_bytes)
        and hmac.compare_digest(final_index_blob, index_blob)
    ):
        raise RuntimeAdapterManifestError("repository revision mismatch")


def load_verified_repository_module(
    module_path: str | Path,
    *,
    expected_sha256: str,
    repository_root: Path,
) -> ModuleType:
    """Single-open a repo-confined module and execute only clean HEAD bytes."""

    root = repository_root.resolve(strict=True)
    path, content = _read_authorized_file(
        module_path,
        repository_root=root,
        description="runtime adapter module",
    )
    expected_digest = _require_sha256(
        expected_sha256,
        description="runtime adapter module",
    )
    if not hmac.compare_digest(_sha256(content), expected_digest):
        raise RuntimeAdapterManifestError("runtime adapter module digest mismatch")
    _require_committed_repository_bytes(path, content, repository_root=root)
    return _load_verified_module(path, content, expected_digest)


def read_verified_repository_bytes(
    file_path: str | Path,
    *,
    expected_sha256: str,
    expected_repository_revision: str,
    repository_root: Path,
) -> tuple[Path, bytes]:
    """Single-open one digest-pinned file and attest it to clean HEAD/index bytes."""

    root = repository_root.resolve(strict=True)
    if re.fullmatch(_REVISION_PATTERN, expected_repository_revision) is None:
        raise RuntimeAdapterManifestError("repository revision is invalid")
    requested_path = _normalize_requested_path(file_path, repository_root=root)
    path, content = _read_authorized_file(
        file_path,
        repository_root=root,
        description="repository input",
    )
    if os.path.normcase(os.path.abspath(path)) != os.path.normcase(
        os.path.abspath(requested_path)
    ):
        raise RuntimeAdapterManifestError(
            "repository input link or final path mismatch"
        )
    expected_digest = _require_sha256(
        expected_sha256,
        description="repository input",
    )
    if not hmac.compare_digest(_sha256(content), expected_digest):
        raise RuntimeAdapterManifestError("repository input digest mismatch")

    _require_committed_repository_bytes(
        path,
        content,
        repository_root=root,
        expected_repository_revision=expected_repository_revision,
    )
    return path, content


def _validate_binding(value: object) -> RuntimeAdapterBinding:
    if not isinstance(value, RuntimeAdapterBinding):
        raise RuntimeAdapterManifestError(
            "runtime adapter factory returned an invalid binding"
        )
    required_methods = (
        ("HermesPlannerPort", value.hermes, "plan", 2),
        ("HermesPlannerPort", value.hermes, "design_agent", 2),
        ("CodexExecutionPort", value.codex, "start", 2),
        ("CodexExecutionPort", value.codex, "resume", 2),
        ("CodexExecutionPort", value.codex, "status", 2),
        ("CodexExecutionPort", value.codex, "cancel", 2),
        ("CodexExecutionPort", value.codex, "heartbeat", 2),
        ("ArtifactPort", value.artifacts, "require", 1),
    )
    for port_name, adapter, method_name, argument_count in required_methods:
        qualified_name = f"{port_name}.{method_name}"
        try:
            method = getattr(adapter, method_name)
        except Exception:
            raise RuntimeAdapterManifestError(
                f"runtime adapter binding does not implement {qualified_name}"
            ) from None
        if not callable(method):
            raise RuntimeAdapterManifestError(
                f"runtime adapter binding {qualified_name} must be callable"
            )
        if not inspect.iscoroutinefunction(method):
            raise RuntimeAdapterManifestError(
                f"runtime adapter binding {qualified_name} must be async"
            )
        try:
            inspect.signature(method).bind(*([object()] * argument_count))
        except (TypeError, ValueError):
            raise RuntimeAdapterManifestError(
                f"runtime adapter binding {qualified_name} has an invalid signature"
            ) from None
    return value


def load_runtime_adapters(
    manifest_path: str | Path,
    *,
    expected_sha256: str,
    repository_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeAdapterBinding:
    """Validate exact manifest/module bytes, then invoke and verify their factory."""

    root = (
        _canonical_repository_root()
        if repository_root is None
        else repository_root.resolve(strict=False)
    )
    _, manifest_content = _read_authorized_file(
        manifest_path,
        repository_root=root,
        description="runtime adapter manifest",
    )
    expected_manifest_digest = _require_sha256(
        expected_sha256,
        description="runtime adapter manifest",
    )
    if not hmac.compare_digest(_sha256(manifest_content), expected_manifest_digest):
        raise RuntimeAdapterManifestError("runtime adapter manifest digest mismatch")

    manifest = _parse_manifest(manifest_content)
    module_path, module_content = _read_authorized_file(
        manifest.module_path,
        repository_root=root,
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
