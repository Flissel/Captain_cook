"""Atomic local initialization and fail-closed D12 bundle loading."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from .contracts import CANONICAL_INPUT_PATHS, DemoBundleV1


class DemoConfigurationError(RuntimeError):
    """The live demo is not safe to initialize or start."""


PRIVATE_ENV_RELATIVE = Path(".captain-cook/live-demo/private/demo.env")
STATE_ROOT_RELATIVE = Path(".captain-cook/live-demo")
GENERATED_SECRET_NAMES = (
    "CAPTAIN_GATEWAY_TOKEN",
    "WORKER_GATEWAY_TOKEN",
    "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
    "CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN",
    "CAPTAIN_RUNTIME_TOKEN",
    "CAPTAIN_BENCHMARK_TOKEN",
    "MINIBOOK_PROJECTION_API_KEY",
)


@dataclass(frozen=True, slots=True)
class DemoConfiguration:
    repository_root: Path
    state_root: Path
    bundle: DemoBundleV1
    bundle_path: Path
    bundle_sha256: str
    environment: Mapping[str, str]

    def environment_for(self, service_name: str) -> dict[str, str]:
        service = next(
            (item for item in self.bundle.services if item.name == service_name),
            None,
        )
        if service is None:
            raise DemoConfigurationError(f"unknown demo service: {service_name}")
        missing = [name for name in service.environment if not self.environment.get(name)]
        if missing:
            raise DemoConfigurationError(
                f"{service_name} environment is missing {', '.join(sorted(missing))}"
            )
        return {name: self.environment[name] for name in service.environment}


def initialize_private_environment(
    repository_root: Path,
    *,
    reporter: Callable[[str], None] = print,
) -> Path:
    repository = repository_root.resolve(strict=True)
    private_env = repository / PRIVATE_ENV_RELATIVE
    private_env.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_env(private_env) if private_env.exists() else {}
    updated = dict(existing)
    for name in GENERATED_SECRET_NAMES:
        if not updated.get(name, "").strip():
            updated[name] = secrets.token_urlsafe(32)
    if updated != existing:
        _atomic_write_env(private_env, updated)
        reporter("[ready] missing private demo authorities initialized (values redacted)")
    else:
        reporter("[ready] private demo authorities already initialized (values redacted)")
    return private_env


def load_demo_configuration(
    repository_root: Path,
    *,
    revision_reader: Callable[[Path], str] | None = None,
) -> DemoConfiguration:
    repository = repository_root.resolve(strict=True)
    private_env_path = repository / PRIVATE_ENV_RELATIVE
    private = _read_env(private_env_path)
    root = _read_env(repository / ".env")
    overlap = set(private) & set(root)
    if overlap:
        raise DemoConfigurationError(
            "root and private demo environments contain overlapping authority names"
        )
    environment = {**root, **private}
    raw_manifest = environment.get("CAPTAIN_DEMO_BUNDLE_MANIFEST", "").strip()
    raw_digest = environment.get("CAPTAIN_DEMO_BUNDLE_MANIFEST_SHA256", "").strip()
    if not raw_manifest or not raw_digest:
        raise DemoConfigurationError("D11 sealed capability demo bundle is unavailable")
    manifest_path = Path(raw_manifest)
    if not manifest_path.is_absolute():
        manifest_path = repository / manifest_path
    manifest_path = _resolve_repository_or_private_path(
        repository, manifest_path, "bundle manifest"
    )
    actual_digest = _sha256(manifest_path)
    if actual_digest != raw_digest:
        raise DemoConfigurationError("capability demo bundle digest mismatch")
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
        json.loads(raw_text)
        bundle = DemoBundleV1.model_validate_json(raw_text)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise DemoConfigurationError("capability demo bundle is invalid") from exc
    reader = revision_reader or _git_revision
    if reader(repository) != bundle.repository_revision:
        raise DemoConfigurationError("capability demo bundle repository revision mismatch")
    _verify_bundle_files(repository, bundle)
    values = [environment.get(name, "") for name in GENERATED_SECRET_NAMES]
    if any(not value.strip() for value in values):
        raise DemoConfigurationError("private demo authorities are incomplete")
    if len(values) != len(set(values)):
        raise DemoConfigurationError("private demo authorities must be distinct")
    if not environment.get("OPENAI_API_KEY", "").strip():
        raise DemoConfigurationError("operator-supplied OPENAI_API_KEY is unavailable")
    for service in bundle.services:
        configuration = DemoConfiguration(
            repository_root=repository,
            state_root=repository / STATE_ROOT_RELATIVE,
            bundle=bundle,
            bundle_path=manifest_path,
            bundle_sha256=actual_digest,
            environment=environment,
        )
        configuration.environment_for(service.name)
    return configuration


def _verify_bundle_files(repository: Path, bundle: DemoBundleV1) -> None:
    catalog = repository.joinpath(*PurePosixPath(bundle.benchmark_catalog_path).parts)
    if not catalog.is_file():
        raise DemoConfigurationError("D10 private benchmark catalog is unavailable")
    for relative, expected in {
        **bundle.canonical_inputs,
        **bundle.artifact_digests,
        **bundle.compose_files,
        bundle.benchmark_catalog_path: bundle.benchmark_catalog_sha256,
    }.items():
        path = _resolve_relative_file(repository, relative)
        if _sha256(path) != expected:
            raise DemoConfigurationError(f"sealed file digest mismatch: {relative}")
    referenced = {
        relative for service in bundle.services for relative in service.artifact_paths
    }
    if not referenced.issubset(bundle.artifact_digests):
        raise DemoConfigurationError("service artifact is not digest-bound")
    if set(bundle.canonical_inputs) != set(CANONICAL_INPUT_PATHS):
        raise DemoConfigurationError("canonical input set changed")


def _resolve_relative_file(repository: Path, relative: str) -> Path:
    if "\\" in relative:
        raise DemoConfigurationError("bundle path must be repository-relative POSIX")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise DemoConfigurationError("bundle path must be repository-relative POSIX")
    return _resolve_repository_or_private_path(repository, repository.joinpath(*pure.parts), relative)


def _resolve_repository_or_private_path(
    repository: Path, path: Path, label: str
) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise DemoConfigurationError(f"sealed file is unavailable: {label}") from None
    try:
        resolved.relative_to(repository)
    except ValueError:
        raise DemoConfigurationError(f"sealed file escaped repository: {label}") from None
    if not resolved.is_file():
        raise DemoConfigurationError(f"sealed file is unavailable: {label}")
    return resolved


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DemoConfigurationError(f"could not read local environment: {path.name}") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name or not name.replace("_", "A").isalnum() or not name[0].isalpha():
            raise DemoConfigurationError(f"invalid local environment line in {path.name}")
        if name in values:
            raise DemoConfigurationError(f"duplicate local environment key in {path.name}")
        values[name] = value
    return values


def _atomic_write_env(path: Path, values: Mapping[str, str]) -> None:
    payload = "".join(f"{name}={value}\n" for name, value in values.items())
    descriptor, raw_temp = tempfile.mkstemp(prefix="demo-env-", suffix=".tmp", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_revision(repository: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DemoConfigurationError("repository revision is unavailable")
    return result.stdout.strip()


__all__ = [
    "DemoConfiguration",
    "DemoConfigurationError",
    "initialize_private_environment",
    "load_demo_configuration",
]
