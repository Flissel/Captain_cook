#!/usr/bin/env python3
"""Build the local, digest-bound runtime adapter manifest without secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


_SCHEMA = "captain.runtime-adapters.v1"
_DEFAULT_ADAPTER_MODULE = Path(
    "agenten/agent_runtime/captain_production_adapters.py"
)
_MANIFEST_NAME = "captain-runtime-adapters.json"


class ManifestGenerationError(ValueError):
    """The current checkout cannot safely produce a runtime adapter manifest."""


@dataclass(frozen=True, slots=True)
class GeneratedManifest:
    """Non-secret metadata needed by the runtime bootstrap."""

    manifest_path: Path
    manifest_sha256: str
    module_sha256: str


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_bytes(*, module_path: Path, module_sha256: str) -> bytes:
    document = {
        "factory_name": "create_runtime_adapters",
        "module_path": module_path.as_posix(),
        "module_sha256": module_sha256,
        "schema": _SCHEMA,
    }
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def _relative_module_path(value: Path, *, repository_root: Path) -> Path:
    candidate = value if not value.is_absolute() else value.resolve(strict=False)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(repository_root)
        except ValueError as exc:
            raise ManifestGenerationError(
                "runtime adapter module is outside the repository"
            ) from exc
    if candidate.is_absolute() or candidate == Path(".") or ".." in candidate.parts:
        raise ManifestGenerationError("runtime adapter module path is unsafe")
    return candidate


def _committed_bytes(*, repository_root: Path, relative_path: Path) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository_root), "show", f"HEAD:{relative_path.as_posix()}"],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManifestGenerationError(
            "committed runtime adapter bytes are unavailable"
        ) from exc
    if result.returncode != 0:
        raise ManifestGenerationError("committed runtime adapter bytes are unavailable")
    return result.stdout


def _write_if_needed(path: Path, content: bytes, *, check_only: bool) -> None:
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        if check_only:
            raise ManifestGenerationError("runtime adapter manifest is missing") from None
        existing = None
    except OSError as exc:
        raise ManifestGenerationError("runtime adapter manifest cannot be read") from exc
    if existing == content:
        return
    if check_only:
        raise ManifestGenerationError("runtime adapter manifest does not match current bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ManifestGenerationError("runtime adapter manifest cannot be written") from exc


def generate_manifest(
    *,
    repository_root: Path,
    adapter_module: Path = _DEFAULT_ADAPTER_MODULE,
    check_only: bool = False,
) -> GeneratedManifest:
    """Return a manifest only when its module equals the committed source bytes."""

    root = repository_root.resolve(strict=True)
    relative_module = _relative_module_path(adapter_module, repository_root=root)
    module_path = root / relative_module
    try:
        checked_out_bytes = module_path.read_bytes()
    except OSError as exc:
        raise ManifestGenerationError("runtime adapter module is missing") from exc
    committed_bytes = _committed_bytes(
        repository_root=root,
        relative_path=relative_module,
    )
    if checked_out_bytes != committed_bytes:
        raise ManifestGenerationError(
            "runtime adapter module does not match committed bytes"
        )
    module_digest = _sha256(committed_bytes)
    manifest_bytes = _canonical_bytes(
        module_path=relative_module,
        module_sha256=module_digest,
    )
    manifest_path = root / ".captain-cook" / "runtime-adapters" / _MANIFEST_NAME
    _write_if_needed(manifest_path, manifest_bytes, check_only=check_only)
    return GeneratedManifest(
        manifest_path=manifest_path,
        manifest_sha256=_sha256(manifest_bytes),
        module_sha256=module_digest,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="generate a digest-bound Captain runtime adapter manifest"
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--adapter-module", type=Path, default=_DEFAULT_ADAPTER_MODULE)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Generate or validate one local manifest and print only its public metadata."""

    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        generated = generate_manifest(
            repository_root=args.repository_root,
            adapter_module=args.adapter_module,
            check_only=args.check,
        )
    except (ManifestGenerationError, OSError) as exc:
        print(f"runtime adapter manifest error: {exc}", file=sys.stderr)
        return 1
    print(f"manifest_path={generated.manifest_path}")
    print(f"manifest_sha256={generated.manifest_sha256}")
    print(f"module_sha256={generated.module_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
