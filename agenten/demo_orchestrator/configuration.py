"""Local secret initialization and adapters for real D10/D11 exports."""

from __future__ import annotations

import hashlib
import importlib
import os
import secrets
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from .contracts import CANONICAL_INPUT_PATHS


class DemoConfigurationError(RuntimeError):
    """The live demo is not safe to initialize or operate."""


class ProductionManifestLoader(Protocol):
    def __call__(self, *, environment: Mapping[str, str]) -> object: ...


@dataclass(frozen=True, slots=True)
class AuthorityHealthBinding:
    service: str
    health_url: str
    token_name: str


@dataclass(frozen=True, slots=True)
class OperatorServiceControl:
    service: Literal["gateway", "runtime", "provider_proxy", "benchmark", "minibook", "n8n"]
    start_argv: tuple[str, ...]
    stop_argv: tuple[str, ...]
    restart_argv: tuple[str, ...]
    authenticated_health_url: str
    authenticated_health_credential_env: str
    authenticated_health_header: Literal["Authorization", "X-N8N-API-KEY"]


@dataclass(frozen=True, slots=True)
class OperatorProcessBootstrap:
    services: tuple[OperatorServiceControl, ...]


@dataclass(frozen=True, slots=True)
class RuntimeRestartControl:
    service: Literal["runtime"]
    restart_argv: tuple[str, ...]
    authenticated_health_url: str
    authenticated_health_credential_env: str
    authenticated_health_header: Literal["Authorization"]


PRIVATE_ENV_RELATIVE = Path(".captain-cook/live-demo/private/demo.env")
STATE_ROOT_RELATIVE = Path(".captain-cook/live-demo")
GENERATED_SECRET_NAMES = (
    "CAPTAIN_GATEWAY_TOKEN",
    "WORKER_GATEWAY_TOKEN",
    "CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN",
    "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN",
    "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
    "CAPTAIN_RUNTIME_TOKEN",
    "MINIBOOK_API_KEY",
    "MINIBOOK_PROJECTION_API_KEY",
)


@dataclass(frozen=True, slots=True)
class DemoConfiguration:
    repository_root: Path
    state_root: Path
    production_manifest: object
    repository_revision: str
    canonical_input_digests: Mapping[str, str]
    environment: Mapping[str, str]

    @property
    def bundle_sha256(self) -> str:
        return str(getattr(self.production_manifest, "manifest_sha256"))

    @property
    def gateway_base_url(self) -> str:
        return str(getattr(getattr(self.production_manifest, "gateway"), "base_url"))

    @property
    def provider_context_reference_header(self) -> str:
        return str(
            getattr(self.production_manifest, "provider_context_reference_header")
        )

    def health_bindings(self) -> tuple[AuthorityHealthBinding, ...]:
        names = ("gateway", "provider_proxy", "runtime", "benchmark", "minibook")
        return tuple(
            AuthorityHealthBinding(
                service=service,
                health_url=str(
                    getattr(getattr(self.production_manifest, service), "health_url")
                ),
                token_name=str(
                    getattr(
                        getattr(self.production_manifest, service), "credential_env"
                    )
                ),
            )
            for service in names
        )

    def authority_token(self, name: str) -> str:
        value = self.environment.get(name, "").strip()
        if not value:
            raise DemoConfigurationError(f"required authority is unavailable: {name}")
        return value

    def require_private_benchmark_catalog(self) -> object:
        try:
            module = importlib.import_module(
                "agenten.agent_factory.private_benchmark_catalog"
            )
            catalog_type = getattr(module, "VerifiedFilePrivateBenchmarkCatalog")
        except (ImportError, AttributeError):
            raise DemoConfigurationError(
                "D10 private benchmark catalog capability is unavailable"
            ) from None
        names = (
            "CAPTAIN_PRIVATE_BENCHMARK_ROOT",
            "CAPTAIN_PRIVATE_BENCHMARK_MANIFEST_PATH",
            "CAPTAIN_PRIVATE_BENCHMARK_MANIFEST_SHA256",
        )
        if any(not self.environment.get(name, "").strip() for name in names):
            raise DemoConfigurationError(
                "D10 private benchmark catalog configuration is incomplete"
            )
        try:
            catalog = catalog_type.open(
                private_root=Path(self.environment[names[0]]),
                manifest_path=Path(self.environment[names[1]]),
                expected_sha256=self.environment[names[2]],
                expected_input_sha256s=tuple(
                    self.canonical_input_digests[path]
                    for path in CANONICAL_INPUT_PATHS
                ),
                forbidden_roots=(self.repository_root / ".git",),
            )
            catalog.revalidate()
        except Exception:
            raise DemoConfigurationError(
                "D10 private benchmark catalog validation failed"
            ) from None
        return catalog

    def require_minibook_evidence_readback(self) -> str:
        value = getattr(
            self.production_manifest, "minibook_evidence_url_template", None
        )
        if not isinstance(value, str) or value.count("{assembly_id}") != 1:
            raise DemoConfigurationError(
                "D11 Minibook assembly evidence readback capability is unavailable"
            )
        return value

    def require_operator_process_bootstrap(self) -> OperatorProcessBootstrap:
        expected = ("gateway", "runtime", "provider_proxy", "benchmark", "minibook", "n8n")
        controls: list[OperatorServiceControl] = []
        for service in expected:
            binding = getattr(self.production_manifest, service, None)
            try:
                control = OperatorServiceControl(
                    service=service,  # type: ignore[arg-type]
                    start_argv=tuple(binding.start_argv),
                    stop_argv=tuple(binding.stop_argv),
                    restart_argv=tuple(binding.restart_argv),
                    authenticated_health_url=str(binding.authenticated_health_url),
                    authenticated_health_credential_env=str(
                        binding.authenticated_health_credential_env
                    ),
                    authenticated_health_header=str(binding.authenticated_health_header),  # type: ignore[arg-type]
                )
            except (AttributeError, TypeError):
                raise DemoConfigurationError(
                    "D11 operator process bootstrap is unavailable"
                ) from None
            prefix = ("python", "-m", "gateway.capability_process_operator", "--service", service)
            if (
                control.start_argv != (*prefix, "start")
                or control.stop_argv != (*prefix, "stop")
                or control.restart_argv != (*prefix, "restart")
                or not control.authenticated_health_url.startswith(("http://", "https://"))
                or control.authenticated_health_header not in {"Authorization", "X-N8N-API-KEY"}
                or not control.authenticated_health_credential_env
            ):
                raise DemoConfigurationError(
                    "D11 operator process bootstrap is invalid"
                )
            if service == "n8n" and control.authenticated_health_header != "X-N8N-API-KEY":
                raise DemoConfigurationError("D11 operator process bootstrap is invalid")
            if service != "n8n" and control.authenticated_health_header != "Authorization":
                raise DemoConfigurationError("D11 operator process bootstrap is invalid")
            controls.append(control)
        return OperatorProcessBootstrap(services=tuple(controls))

    def require_runtime_restart_control(self) -> RuntimeRestartControl:
        runtime = self.require_operator_process_bootstrap().services[1]
        return RuntimeRestartControl(
            service="runtime",
            restart_argv=runtime.restart_argv,
            authenticated_health_url=runtime.authenticated_health_url,
            authenticated_health_credential_env=runtime.authenticated_health_credential_env,
            authenticated_health_header="Authorization",
        )


def initialize_private_environment(
    repository_root: Path,
    *,
    reporter: Callable[[str], None] = print,
) -> Path:
    repository = repository_root.resolve(strict=True)
    private_env = repository / PRIVATE_ENV_RELATIVE
    private_env.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_env(private_env) if private_env.exists() else {}
    root = _read_env(repository / ".env")
    updated = dict(existing)
    for name in GENERATED_SECRET_NAMES:
        if not updated.get(name, "").strip() and not root.get(name, "").strip():
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
    production_manifest_loader: ProductionManifestLoader | None = None,
) -> DemoConfiguration:
    repository = repository_root.resolve(strict=True)
    private = _read_env(repository / PRIVATE_ENV_RELATIVE)
    root = _read_env(repository / ".env")
    overlap = set(private) & set(root)
    if overlap:
        raise DemoConfigurationError(
            "root and private demo environments contain overlapping authority names"
        )
    environment = {**root, **private}
    loader = production_manifest_loader or _resolve_d11_loader()
    try:
        manifest = loader(environment=environment)
    except DemoConfigurationError:
        raise
    except Exception:
        raise DemoConfigurationError(
            "D11 production bundle configuration is incomplete"
        ) from None
    _validate_d11_manifest(manifest)
    reader = revision_reader or _git_revision
    revision = reader(repository)
    digests = {
        relative: _sha256(_resolve_relative_file(repository, relative))
        for relative in CANONICAL_INPUT_PATHS
    }
    if len(set(digests.values())) != 3:
        raise DemoConfigurationError("canonical demo inputs must have distinct bytes")
    values = [environment.get(name, "") for name in GENERATED_SECRET_NAMES]
    if any(not value.strip() for value in values):
        raise DemoConfigurationError("private demo authorities are incomplete")
    if len(values) != len(set(values)):
        raise DemoConfigurationError("private demo authorities must be distinct")
    return DemoConfiguration(
        repository_root=repository,
        state_root=repository / STATE_ROOT_RELATIVE,
        production_manifest=manifest,
        repository_revision=revision,
        canonical_input_digests=digests,
        environment=environment,
    )


def _resolve_d11_loader() -> ProductionManifestLoader:
    try:
        module = importlib.import_module("gateway.capability_assembly_bootstrap")
        loader = getattr(module, "load_capability_production_bundle_manifest")
    except (ImportError, AttributeError):
        raise DemoConfigurationError(
            "D11 operator process bootstrap is unavailable"
        ) from None
    if not callable(loader):
        raise DemoConfigurationError("D11 operator process bootstrap is unavailable")
    return loader


def _validate_d11_manifest(manifest: object) -> None:
    if (
        getattr(manifest, "schema_name", None)
        != "captain.capability-production-bundle-manifest.v1"
        or getattr(manifest, "provider_supervisor_mode", None)
        != "dynamic_gateway_context_v1"
        or getattr(manifest, "provider_context_reference_header", None)
        != "X-Captain-Provider-Context-Ref"
    ):
        raise DemoConfigurationError("D11 production bundle manifest is invalid")
    digest = getattr(manifest, "manifest_sha256", None)
    if not isinstance(digest, str) or len(digest) != 64:
        raise DemoConfigurationError("D11 production bundle manifest is invalid")
    provider_digests = tuple(
        getattr(manifest, name, None)
        for name in (
            "provider_policy_sha256", "provider_price_card_sha256",
            "provider_context_sha256", "provider_proxy_executable_sha256",
            "provider_proxy_code_sha256", "provider_paid_command_sha256",
        )
    )
    if (
        any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in provider_digests
        )
        or getattr(manifest, "provider_hard_ceiling_enforced", None) is not True
    ):
        raise DemoConfigurationError("D11 production bundle manifest is invalid")
    expected = {
        "gateway": ("CAPTAIN_GATEWAY_TOKEN", "/v1/capability-assemblies/health", None),
        "benchmark": (
            "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN",
            "/healthz",
            "/v1/execute",
        ),
        "runtime": ("CAPTAIN_RUNTIME_TOKEN", "/health", None),
        "provider_proxy": (
            "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
            "/v1/healthz",
            None,
        ),
        "minibook": ("MINIBOOK_API_KEY", "/health", None),
    }
    for name, (credential_env, health_suffix, execute_suffix) in expected.items():
        binding = getattr(manifest, name, None)
        base_url = getattr(binding, "base_url", None)
        health_url = getattr(binding, "health_url", None)
        execute_url = getattr(binding, "execute_url", None)
        if (
            getattr(binding, "service", None) != name
            or getattr(binding, "authentication", None) != "bearer"
            or getattr(binding, "credential_env", None) != credential_env
            or not isinstance(base_url, str)
            or health_url != base_url.rstrip("/") + health_suffix
            or (
                execute_url
                != (
                    None
                    if execute_suffix is None
                    else base_url.rstrip("/") + execute_suffix
                )
            )
        ):
            raise DemoConfigurationError("D11 production bundle manifest is invalid")
    n8n = getattr(manifest, "n8n", None)
    if (
        getattr(n8n, "service", None) != "n8n"
        or getattr(n8n, "authentication", None) != "x-n8n-api-key"
        or getattr(n8n, "credential_env", None) != "CAPTAIN_N8N_API_KEY"
        or getattr(n8n, "authenticated_health_credential_env", None)
        != "CAPTAIN_N8N_API_KEY"
        or getattr(n8n, "authenticated_health_header", None) != "X-N8N-API-KEY"
    ):
        raise DemoConfigurationError("D11 production bundle manifest is invalid")


def _resolve_relative_file(repository: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    path = repository.joinpath(*pure.parts).resolve(strict=True)
    try:
        path.relative_to(repository)
    except ValueError:
        raise DemoConfigurationError("canonical input escaped repository") from None
    if not path.is_file():
        raise DemoConfigurationError(f"canonical input is unavailable: {relative}")
    return path


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
    "AuthorityHealthBinding",
    "DemoConfiguration",
    "DemoConfigurationError",
    "initialize_private_environment",
    "load_demo_configuration",
]
