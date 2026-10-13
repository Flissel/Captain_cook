"""Captain-owned trust root and local identity gate for Factory V3 live runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


_SCHEMA = "captain.factory-v3-live-authority-binding.v1"
_SIGNING_KEY_ID = "captain-factory-v3-authority-2026-01"
_PINNED_AUTHORITY_PUBLIC_KEY = base64.b64decode(
    "w8QddodSSiWNCpgja5kmRJCgWNJ6zEk4twa8fmST5dY=",
    validate=True,
)
_BINDING_PATH = Path(
    ".captain-cook/private/factory-v3-live/authority-binding.json"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+ -]{0,127}$")
_EXPECTED_SERVICE_BINDINGS = {
    "gateway": (
        "captain-gateway",
        ("factory-v3-live-authority", "action-inbox-read"),
        "captain-gateway-live-v3",
    ),
    "minibook": (
        "minibook-creation",
        ("creation-jobs-v3", "cas-readback-v1"),
        "minibook-creation-live-v3",
    ),
}
_EXPECTED_CREDENTIAL_AUDIENCES = {
    "captain_token": "captain-gateway-live-v3",
    "worker_token": "minibook-creation-live-v3",
    "creation_api_key": "minibook-creation-api-v3",
}
_EXPECTED_HERMES_CAPABILITIES = (
    "factory-v3-build",
    "released-skill-execution",
)
_CODEX_SESSION_MARKERS = (
    b'ArgumentList.Add("--sandbox")',
    b'ArgumentList.Add("--ask-for-approval")',
    b'ArgumentList.Add("never")',
    b'ArgumentList.Add("sandbox_workspace_write.network_access=false")',
    b'ArgumentList.Add("--ignore-user-config")',
    b'ArgumentList.Add("--json")',
    b"$DeadlineAt",
)


class FactoryV3LiveAuthorityError(ValueError):
    """A sanitized failure while validating the Captain authority binding."""


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _canonical_origin(value: object) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if "\\" in value or "%" in value:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname
    if (
        hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.scheme not in {"https", "http"}
        or (
            parsed.scheme == "http"
            and hostname not in {"127.0.0.1", "::1", "localhost"}
        )
        or hostname.endswith(".")
    ):
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname.casefold()
    origin = f"{parsed.scheme}://{host}"
    if port is not None:
        origin = f"{origin}:{port}"
    return origin if value.rstrip("/").casefold() == origin.casefold() else None


def _mapping(value: object, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise FactoryV3LiveAuthorityError("Factory V3 authority binding is invalid")
    return value


def _safe_text(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise FactoryV3LiveAuthorityError("Factory V3 authority binding is invalid")
    return value


@dataclass(frozen=True)
class FactoryV3ServiceBinding:
    origin: str
    health_path: str
    service: str
    version: str
    capabilities: tuple[str, ...]
    credential_audience: str


@dataclass(frozen=True)
class FactoryV3CredentialAudiences:
    captain_token: str
    worker_token: str
    creation_api_key: str


@dataclass(frozen=True)
class FactoryV3ExecutableBinding:
    sha256: str
    version: str


@dataclass(frozen=True)
class FactoryV3ExecutableBindings:
    codex: FactoryV3ExecutableBinding
    pwsh: FactoryV3ExecutableBinding
    hermes_python: FactoryV3ExecutableBinding


@dataclass(frozen=True)
class FactoryV3CodexSessionBinding:
    sha256: str
    version: str


@dataclass(frozen=True)
class FactoryV3HermesRuntimeBinding:
    runtime_sha256: str
    git_sha: str
    distribution: str
    version: str
    module_file_sha256: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class FactoryV3ReleasedSkillBinding:
    name: str
    sha256: str


@dataclass(frozen=True)
class FactoryV3RunnerAttestationBinding:
    key_id: str
    public_key: bytes


@dataclass(frozen=True)
class FactoryV3LiveAuthorityBinding:
    authority_id: str
    binding_version: int
    gateway: FactoryV3ServiceBinding
    minibook: FactoryV3ServiceBinding
    credential_audiences: FactoryV3CredentialAudiences
    executables: FactoryV3ExecutableBindings
    codex_session: FactoryV3CodexSessionBinding
    hermes_runtime: FactoryV3HermesRuntimeBinding
    released_skill: FactoryV3ReleasedSkillBinding
    runner_attestation: FactoryV3RunnerAttestationBinding
    sha256: str


def _parse_service(value: object, *, role: str) -> FactoryV3ServiceBinding:
    payload = _mapping(
        value,
        {
            "origin",
            "health_path",
            "service",
            "version",
            "capabilities",
            "credential_audience",
        },
    )
    expected_service, expected_capabilities, expected_audience = (
        _EXPECTED_SERVICE_BINDINGS[role]
    )
    origin = _canonical_origin(payload["origin"])
    capabilities = payload["capabilities"]
    if (
        origin is None
        or payload["health_path"] != "/healthz"
        or payload["service"] != expected_service
        or payload["version"] != "v3"
        or not isinstance(capabilities, Sequence)
        or isinstance(capabilities, (str, bytes, bytearray))
        or tuple(capabilities) != expected_capabilities
        or payload["credential_audience"] != expected_audience
    ):
        raise FactoryV3LiveAuthorityError("Factory V3 authority binding is invalid")
    return FactoryV3ServiceBinding(
        origin=origin,
        health_path="/healthz",
        service=expected_service,
        version="v3",
        capabilities=expected_capabilities,
        credential_audience=expected_audience,
    )


def _parse_executable(value: object) -> FactoryV3ExecutableBinding:
    payload = _mapping(value, {"sha256", "version"})
    return FactoryV3ExecutableBinding(
        sha256=_safe_text(payload["sha256"], _SHA256),
        version=_safe_text(payload["version"], _SAFE_VERSION),
    )


def parse_signed_factory_v3_live_authority(
    payload: Mapping[str, object],
    *,
    verification_public_key: bytes,
) -> FactoryV3LiveAuthorityBinding:
    """Verify and parse a canonical Ed25519-signed authority document."""

    expected_fields = {
        "schema",
        "authority_id",
        "binding_version",
        "services",
        "credential_audiences",
        "executables",
        "codex_session",
        "hermes_runtime",
        "released_skill",
        "runner_attestation",
        "signature",
    }
    try:
        document = _mapping(payload, expected_fields)
        signature_block = _mapping(
            document["signature"],
            {"algorithm", "key_id", "value_base64"},
        )
        if (
            signature_block["algorithm"] != "Ed25519"
            or signature_block["key_id"] != _SIGNING_KEY_ID
            or len(verification_public_key) != 32
        ):
            raise ValueError
        encoded_signature = signature_block["value_base64"]
        if not isinstance(encoded_signature, str):
            raise ValueError
        signature = base64.b64decode(encoded_signature, validate=True)
        if len(signature) != 64:
            raise ValueError
        unsigned = {key: value for key, value in document.items() if key != "signature"}
        canonical = _canonical_bytes(unsigned)
        Ed25519PublicKey.from_public_bytes(verification_public_key).verify(
            signature,
            canonical,
        )
        if document["schema"] != _SCHEMA:
            raise ValueError
        authority_id = _safe_text(document["authority_id"], _SAFE_NAME)
        binding_version = document["binding_version"]
        if (
            not isinstance(binding_version, int)
            or isinstance(binding_version, bool)
            or binding_version < 1
        ):
            raise ValueError
        services = _mapping(document["services"], {"gateway", "minibook"})
        gateway = _parse_service(services["gateway"], role="gateway")
        minibook = _parse_service(services["minibook"], role="minibook")
        if gateway.origin == minibook.origin:
            raise ValueError
        audiences = _mapping(
            document["credential_audiences"],
            {"captain_token", "worker_token", "creation_api_key"},
        )
        if dict(audiences) != _EXPECTED_CREDENTIAL_AUDIENCES:
            raise ValueError
        credential_audiences = FactoryV3CredentialAudiences(
            captain_token=_EXPECTED_CREDENTIAL_AUDIENCES["captain_token"],
            worker_token=_EXPECTED_CREDENTIAL_AUDIENCES["worker_token"],
            creation_api_key=_EXPECTED_CREDENTIAL_AUDIENCES["creation_api_key"],
        )
        executable_payload = _mapping(
            document["executables"],
            {"codex", "pwsh", "hermes_python"},
        )
        executables = FactoryV3ExecutableBindings(
            codex=_parse_executable(executable_payload["codex"]),
            pwsh=_parse_executable(executable_payload["pwsh"]),
            hermes_python=_parse_executable(executable_payload["hermes_python"]),
        )
        codex_payload = _mapping(document["codex_session"], {"sha256", "version"})
        if codex_payload["version"] != "captain.codex-session.v1":
            raise ValueError
        codex_session = FactoryV3CodexSessionBinding(
            sha256=_safe_text(codex_payload["sha256"], _SHA256),
            version="captain.codex-session.v1",
        )
        hermes_payload = _mapping(
            document["hermes_runtime"],
            {
                "runtime_sha256",
                "git_sha",
                "distribution",
                "version",
                "module_file_sha256",
                "capabilities",
            },
        )
        capabilities = hermes_payload["capabilities"]
        if (
            not isinstance(capabilities, Sequence)
            or isinstance(capabilities, (str, bytes, bytearray))
            or tuple(capabilities) != _EXPECTED_HERMES_CAPABILITIES
        ):
            raise ValueError
        hermes_runtime = FactoryV3HermesRuntimeBinding(
            runtime_sha256=_safe_text(hermes_payload["runtime_sha256"], _SHA256),
            git_sha=_safe_text(hermes_payload["git_sha"], _GIT_SHA),
            distribution=_safe_text(hermes_payload["distribution"], _SAFE_NAME),
            version=_safe_text(hermes_payload["version"], _SAFE_VERSION),
            module_file_sha256=_safe_text(
                hermes_payload["module_file_sha256"], _SHA256
            ),
            capabilities=_EXPECTED_HERMES_CAPABILITIES,
        )
        skill_payload = _mapping(document["released_skill"], {"name", "sha256"})
        released_skill = FactoryV3ReleasedSkillBinding(
            name=_safe_text(skill_payload["name"], _SAFE_NAME),
            sha256=_safe_text(skill_payload["sha256"], _SHA256),
        )
        runner_payload = _mapping(
            document["runner_attestation"],
            {"key_id", "public_key_base64"},
        )
        runner_key_id = _safe_text(runner_payload["key_id"], _SAFE_NAME)
        runner_encoded = runner_payload["public_key_base64"]
        if not isinstance(runner_encoded, str):
            raise ValueError
        runner_key = base64.b64decode(runner_encoded, validate=True)
        if len(runner_key) != 32:
            raise ValueError
        runner_attestation = FactoryV3RunnerAttestationBinding(
            key_id=runner_key_id,
            public_key=runner_key,
        )
    except (
        FactoryV3LiveAuthorityError,
        InvalidSignature,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        raise FactoryV3LiveAuthorityError(
            "Factory V3 authority binding is invalid"
        ) from exc
    return FactoryV3LiveAuthorityBinding(
        authority_id=authority_id,
        binding_version=binding_version,
        gateway=gateway,
        minibook=minibook,
        credential_audiences=credential_audiences,
        executables=executables,
        codex_session=codex_session,
        hermes_runtime=hermes_runtime,
        released_skill=released_skill,
        runner_attestation=runner_attestation,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def load_captain_factory_v3_live_authority(
    repository_root: Path,
) -> FactoryV3LiveAuthorityBinding | None:
    """Load only the fixed Captain-private binding using the product trust root."""

    try:
        root = repository_root.resolve(strict=True)
        candidate = root / _BINDING_PATH
        if candidate.is_symlink() or any(
            parent.is_symlink()
            for parent in candidate.parents
            if parent != root and root in parent.parents
        ):
            return None
        path = candidate.resolve(strict=True)
        path.relative_to(root)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            return None
        payload = json.loads(path.read_bytes())
        if not isinstance(payload, dict):
            return None
        return parse_signed_factory_v3_live_authority(
            payload,
            verification_public_key=_PINNED_AUTHORITY_PUBLIC_KEY,
        )
    except (
        OSError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        FactoryV3LiveAuthorityError,
    ):
        return None


def _bounded_tree_digest(directory: Path, *, require_skill: bool) -> str | None:
    ignored = {".git", ".venv", "venv", "__pycache__", ".pytest_cache"}
    try:
        root = directory.resolve(strict=True)
        if root.is_symlink() or not root.is_dir():
            return None
        paths = sorted(
            (
                path
                for path in root.rglob("*")
                if not any(part in ignored for part in path.relative_to(root).parts)
            ),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        if any(path.is_symlink() for path in paths):
            return None
        files = [path for path in paths if path.is_file()]
        if not files or len(files) > 20_000:
            return None
        if require_skill and not (root / "SKILL.md").is_file():
            return None
        total = 0
        manifest: list[dict[str, object]] = []
        for path in files:
            content = path.read_bytes()
            total += len(content)
            if total > 256 * 1024 * 1024:
                return None
            manifest.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
            )
        return hashlib.sha256(_canonical_bytes(manifest)).hexdigest()
    except OSError:
        return None


def _factory_v3_runtime_digest(directory: Path) -> str | None:
    return _bounded_tree_digest(directory, require_skill=False)


def _factory_v3_skill_digest(directory: Path) -> str | None:
    return _bounded_tree_digest(directory, require_skill=True)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _factory_v3_git_identity_ready(
    repository_root: Path,
    hermes_runtime_root: Path,
    expected_sha: str,
) -> bool:
    try:
        gitlink = subprocess.run(
            ["git", "ls-tree", "HEAD", "--", "hermes-agent"],
            cwd=repository_root,
            env=_minimal_git_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        child_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=hermes_runtime_root,
            env=_minimal_git_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        child_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=hermes_runtime_root,
            env=_minimal_git_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    fields = gitlink.stdout.strip().split()
    return bool(
        gitlink.returncode == 0
        and len(fields) >= 3
        and fields[0] == "160000"
        and hmac.compare_digest(fields[2], expected_sha)
        and child_head.returncode == 0
        and hmac.compare_digest(child_head.stdout.strip(), expected_sha)
        and child_status.returncode == 0
        and not child_status.stdout.strip()
    )


def _minimal_probe_environment() -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if os.environ.get(key)
    }
    environment.update(
        {
            "PATH": "",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_PROXY": "*",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
        }
    )
    return environment


def _minimal_git_environment() -> dict[str, str]:
    environment = _minimal_probe_environment()
    environment["PATH"] = os.environ.get("PATH", "")
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = (
        "NUL" if os.name == "nt" else "/dev/null"
    )
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _probe_exact(argv: list[str], *, cwd: Path) -> str | None:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=_minimal_probe_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    stdout = completed.stdout
    if (
        completed.returncode != 0
        or completed.stderr
        or len(stdout.encode("utf-8")) > 4096
        or not stdout.endswith("\n")
        or "\r" in stdout
        or stdout.count("\n") != 1
    ):
        return None
    return stdout[:-1]


_HERMES_PROBE = """\
import hashlib, importlib.metadata as metadata, importlib.util, json, pathlib, platform, sys
runtime_root = pathlib.Path(sys.argv[1]).resolve(strict=True)
identity = json.loads((runtime_root / "factory-v3-runtime-identity.json").read_text(encoding="utf-8"))
distribution = metadata.distribution(str(identity["distribution"]))
spec = importlib.util.find_spec("hermes_cli")
if spec is None or spec.origin is None:
    raise SystemExit(2)
module_file = pathlib.Path(spec.origin).resolve(strict=True)
module_file.relative_to(runtime_root)
payload = {
    "schema": "captain.factory-v3-hermes-runtime-probe.v1",
    "python_version": platform.python_version(),
    "distribution": distribution.metadata["Name"],
    "distribution_version": distribution.version,
    "module_file_sha256": hashlib.sha256(module_file.read_bytes()).hexdigest(),
    "capabilities": identity["capabilities"],
}
print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
"""


def verify_factory_v3_local_runtime(
    *,
    binding: FactoryV3LiveAuthorityBinding,
    repository_root: Path,
    codex_path: Path,
    pwsh_path: Path,
    codex_home: Path,
    hermes_python_path: Path,
    hermes_runtime_root: Path,
    released_skill_root: Path,
) -> bool:
    """Hash every executable/runtime first, then run three bounded identity probes."""

    try:
        root = repository_root.resolve(strict=True)
        codex = codex_path.resolve(strict=True)
        pwsh = pwsh_path.resolve(strict=True)
        home = codex_home.resolve(strict=True)
        hermes_python = hermes_python_path.resolve(strict=True)
        hermes_root = hermes_runtime_root.resolve(strict=True)
        expected_hermes_root = (root / "hermes-agent").resolve(strict=True)
        script = (root / "scripts" / "codex-session.ps1").resolve(strict=True)
        skill_root = released_skill_root.resolve(strict=True)
        skill = (skill_root / binding.released_skill.name).resolve(strict=True)
        skill.relative_to(skill_root)
        hermes_python.relative_to(hermes_root)
        paths = (codex, pwsh, home, hermes_python, hermes_root, script, skill_root, skill)
        if any(path.is_symlink() for path in paths):
            return False
        script_content = script.read_bytes()
        module_file = (hermes_root / "hermes_cli" / "__init__.py").resolve(strict=True)
        module_file.relative_to(hermes_root)
        if module_file.is_symlink():
            return False
    except (OSError, ValueError):
        return False
    try:
        codex_sha256 = _file_sha256(codex)
        pwsh_sha256 = _file_sha256(pwsh)
        hermes_python_sha256 = _file_sha256(hermes_python)
        module_file_sha256 = _file_sha256(module_file)
    except OSError:
        return False
    if (
        not codex.is_file()
        or codex.name.casefold() not in {"codex", "codex.exe"}
        or not pwsh.is_file()
        or pwsh.name.casefold() not in {"pwsh", "pwsh.exe"}
        or not home.is_dir()
        or not hermes_python.is_file()
        or hermes_python.name.casefold() not in {"python", "python.exe"}
        or hermes_root != expected_hermes_root
        or not hermes_root.is_dir()
        or not script.is_file()
        or not module_file.is_file()
        or not skill.is_dir()
        or not hmac.compare_digest(codex_sha256, binding.executables.codex.sha256)
        or not hmac.compare_digest(pwsh_sha256, binding.executables.pwsh.sha256)
        or not hmac.compare_digest(
            hermes_python_sha256, binding.executables.hermes_python.sha256
        )
        or not hmac.compare_digest(
            hashlib.sha256(script_content).hexdigest(), binding.codex_session.sha256
        )
        or not hmac.compare_digest(
            module_file_sha256, binding.hermes_runtime.module_file_sha256
        )
        or not hmac.compare_digest(
            _factory_v3_runtime_digest(hermes_root) or "",
            binding.hermes_runtime.runtime_sha256,
        )
        or not hmac.compare_digest(
            _factory_v3_skill_digest(skill) or "",
            binding.released_skill.sha256,
        )
        or not all(marker in script_content for marker in _CODEX_SESSION_MARKERS)
        or not _factory_v3_git_identity_ready(
            root,
            hermes_root,
            binding.hermes_runtime.git_sha,
        )
    ):
        return False
    codex_version = _probe_exact([str(codex), "--version"], cwd=root)
    pwsh_version = _probe_exact(
        [
            str(pwsh),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        ],
        cwd=root,
    )
    hermes_identity = _probe_exact(
        [str(hermes_python), "-I", "-c", _HERMES_PROBE, str(hermes_root)],
        cwd=root,
    )
    if (
        codex_version != binding.executables.codex.version
        or pwsh_version != binding.executables.pwsh.version
        or hermes_identity is None
    ):
        return False
    try:
        hermes_payload = json.loads(hermes_identity)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    expected_hermes = {
        "schema": "captain.factory-v3-hermes-runtime-probe.v1",
        "python_version": binding.executables.hermes_python.version,
        "distribution": binding.hermes_runtime.distribution,
        "distribution_version": binding.hermes_runtime.version,
        "module_file_sha256": binding.hermes_runtime.module_file_sha256,
        "capabilities": list(binding.hermes_runtime.capabilities),
    }
    return hermes_payload == expected_hermes


__all__ = [
    "FactoryV3CodexSessionBinding",
    "FactoryV3CredentialAudiences",
    "FactoryV3ExecutableBinding",
    "FactoryV3ExecutableBindings",
    "FactoryV3HermesRuntimeBinding",
    "FactoryV3LiveAuthorityBinding",
    "FactoryV3LiveAuthorityError",
    "FactoryV3ReleasedSkillBinding",
    "FactoryV3RunnerAttestationBinding",
    "FactoryV3ServiceBinding",
    "load_captain_factory_v3_live_authority",
    "parse_signed_factory_v3_live_authority",
    "verify_factory_v3_local_runtime",
]
