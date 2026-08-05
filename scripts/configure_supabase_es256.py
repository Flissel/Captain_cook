#!/usr/bin/env python3
"""Safely enable asymmetric Supabase Auth sessions on an existing Compose stack.

The script is dry-run by default. With ``--apply`` it uses Supabase's official
key generator without forwarding its output, stores private signing material
only in the Auth service environment file, validates Compose, and recreates
only the four JWT-aware services. It never changes database data or volumes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit


AUTH_ENV = ".env.captain-supabase-auth"
VERIFY_ENV = ".env.captain-supabase-verify"
SERVICES = (
    "supabase-auth",
    "supabase-rest",
    "supabase-realtime",
    "supabase-storage",
)


def _service_span(lines: list[str], service: str) -> tuple[int, int]:
    marker = f"  {service}:"
    matches = [index for index, line in enumerate(lines) if line.rstrip() == marker]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {service} service")
    start = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith("  ") and not line.startswith("    ") and line.strip():
            end = index
            break
    return start, end


def _attach_env_file(text: str, service: str, filename: str) -> str:
    lines = text.splitlines(keepends=True)
    start, end = _service_span(lines, service)
    body = "".join(lines[start:end])
    if filename in body:
        return text
    image_indexes = [
        index
        for index in range(start + 1, end)
        if lines[index].startswith("    image:")
    ]
    if len(image_indexes) != 1:
        raise ValueError(f"expected exactly one image for {service}")
    insertion = image_indexes[0] + 1
    lines[insertion:insertion] = [f"    env_file:\n", f"      - ./{filename}\n"]
    return "".join(lines)


def transform_compose(text: str) -> str:
    """Return an idempotent Compose update with least-privilege env files."""

    transformed = _attach_env_file(text, "supabase-auth", AUTH_ENV)
    for service in SERVICES[1:]:
        transformed = _attach_env_file(transformed, service, VERIFY_ENV)

    lines = transformed.splitlines(keepends=True)
    start, end = _service_span(lines, "supabase-rest")
    matches = [
        index
        for index in range(start, end)
        if re.match(r"^\s{6}PGRST_JWT_SECRET:\s*", lines[index])
    ]
    if len(matches) > 1:
        raise ValueError("expected at most one legacy PostgREST JWT setting")
    if matches:
        del lines[matches[0]]
    return "".join(lines)


def validate_generated_keys(jwt_keys: str, jwt_jwks: str) -> None:
    private_keys = json.loads(jwt_keys)
    public_document = json.loads(jwt_jwks)
    if not isinstance(private_keys, list) or not isinstance(public_document, dict):
        raise ValueError("generated Supabase keys have an invalid shape")
    public_keys = public_document.get("keys")
    if not isinstance(public_keys, list):
        raise ValueError("generated Supabase JWKS has an invalid shape")
    if any(isinstance(key, dict) and "d" in key for key in public_keys):
        raise ValueError("public JWKS contains private material")
    signing = [
        key
        for key in private_keys
        if isinstance(key, dict)
        and key.get("kty") == "EC"
        and key.get("alg") == "ES256"
        and key.get("crv") == "P-256"
        and isinstance(key.get("d"), str)
    ]
    verifying = [
        key
        for key in public_keys
        if isinstance(key, dict)
        and key.get("kty") == "EC"
        and key.get("alg") == "ES256"
        and key.get("crv") == "P-256"
        and key.get("kid") in {candidate.get("kid") for candidate in signing}
    ]
    if len(signing) != 1 or len(verifying) != 1:
        raise ValueError("generated Supabase keys lack one matching ES256 pair")


def _atomic_private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_scoped_env_files(
    directory: Path,
    *,
    jwt_keys: str,
    jwt_jwks: str,
    jwt_issuer: str,
) -> tuple[Path, Path]:
    validate_generated_keys(jwt_keys, jwt_jwks)
    parsed_issuer = urlsplit(jwt_issuer)
    if (
        parsed_issuer.scheme != "https"
        or not parsed_issuer.hostname
        or parsed_issuer.username is not None
        or parsed_issuer.password is not None
        or parsed_issuer.path != "/auth/v1"
        or parsed_issuer.query
        or parsed_issuer.fragment
    ):
        raise ValueError("Supabase JWT issuer must be a canonical HTTPS /auth/v1 URL")
    auth_path = directory / AUTH_ENV
    verify_path = directory / VERIFY_ENV
    _atomic_private_write(
        auth_path,
        f"GOTRUE_JWT_KEYS={jwt_keys}\nGOTRUE_JWT_ISSUER={jwt_issuer}\n",
    )
    _atomic_private_write(
        verify_path,
        "".join(
            (
                f"PGRST_JWT_SECRET={jwt_jwks}\n",
                f"API_JWT_JWKS={jwt_jwks}\n",
                f"JWT_JWKS={jwt_jwks}\n",
            )
        ),
    )
    return auth_path, verify_path


def _legacy_jwt_secret(compose_text: str) -> str:
    matches = re.findall(r"^\s{6}GOTRUE_JWT_SECRET:\s*([^\s#]+)\s*$", compose_text, re.M)
    if len(matches) != 1 or not matches[0] or matches[0].startswith("${"):
        raise ValueError("expected one literal legacy Auth JWT secret")
    return matches[0]


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _run(args: Sequence[str], *, cwd: Path) -> None:
    subprocess.run(
        list(args),
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def apply_migration(
    compose_path: Path,
    generator_path: Path,
    *,
    jwt_issuer: str,
) -> dict[str, object]:
    compose_path = compose_path.resolve(strict=True)
    generator_path = generator_path.resolve(strict=True)
    directory = compose_path.parent
    original = compose_path.read_text(encoding="utf-8")
    transformed = transform_compose(original)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_directory = directory / ".captain-backups" / timestamp
    backup_directory.mkdir(parents=True, exist_ok=False)
    os.chmod(backup_directory, 0o700)
    backup_path = backup_directory / compose_path.name
    shutil.copy2(compose_path, backup_path)
    os.chmod(backup_path, 0o600)

    with tempfile.TemporaryDirectory(prefix="captain-supabase-es256-", dir=directory) as name:
        generation_directory = Path(name)
        os.chmod(generation_directory, 0o700)
        _atomic_private_write(
            generation_directory / ".env",
            f"JWT_SECRET={_legacy_jwt_secret(original)}\n",
        )
        (generation_directory / "docker-compose.yml").write_text(
            "services:\n  placeholder:\n    image: scratch\n",
            encoding="utf-8",
        )
        try:
            _run(("sh", str(generator_path), "--update-env"), cwd=generation_directory)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("Supabase key generation failed") from exc
        generated = _read_env(generation_directory / ".env")
        try:
            jwt_keys = generated["JWT_KEYS"]
            jwt_jwks = generated["JWT_JWKS"]
        except KeyError as exc:
            raise RuntimeError("Supabase key generation was incomplete") from exc
        validate_generated_keys(jwt_keys, jwt_jwks)

    write_scoped_env_files(
        directory,
        jwt_keys=jwt_keys,
        jwt_jwks=jwt_jwks,
        jwt_issuer=jwt_issuer,
    )
    _atomic_private_write(compose_path, transformed)
    try:
        _run(("docker", "compose", "-f", str(compose_path), "config", "--quiet"), cwd=directory)
        _run(
            (
                "docker",
                "compose",
                "-f",
                str(compose_path),
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                *SERVICES,
            ),
            cwd=directory,
        )
    except subprocess.CalledProcessError as exc:
        shutil.copy2(backup_path, compose_path)
        raise RuntimeError("Supabase Compose validation or recreation failed; compose restored") from exc

    return {
        "status": "applied",
        "services": list(SERVICES),
        "backup": str(backup_path),
        "secrets_emitted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--generator", type=Path, required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    compose = args.compose.resolve(strict=True)
    generator = args.generator.resolve(strict=True)
    transformed = transform_compose(compose.read_text(encoding="utf-8"))
    _legacy_jwt_secret(compose.read_text(encoding="utf-8"))
    parsed_issuer = urlsplit(args.issuer)
    if (
        parsed_issuer.scheme != "https"
        or not parsed_issuer.hostname
        or parsed_issuer.username is not None
        or parsed_issuer.password is not None
        or parsed_issuer.path != "/auth/v1"
        or parsed_issuer.query
        or parsed_issuer.fragment
    ):
        parser.error("--issuer must be a canonical HTTPS /auth/v1 URL")
    if not args.apply:
        result: dict[str, object] = {
            "status": "dry_run",
            "would_change": transformed != compose.read_text(encoding="utf-8"),
            "services": list(SERVICES),
            "generator": generator.name,
            "secrets_emitted": False,
        }
    else:
        result = apply_migration(compose, generator, jwt_issuer=args.issuer)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
