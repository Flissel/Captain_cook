#!/usr/bin/env python3
"""Provision one disposable Supabase portal identity without emitting secrets."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 131_072


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def extract_compose_value(compose: str, key: str) -> str:
    matches = re.findall(
        rf"^\s+{re.escape(key)}:\s*([^\s#]+)\s*$",
        compose,
        re.MULTILINE,
    )
    distinct = set(matches)
    if (
        len(distinct) != 1
        or not matches[0]
        or matches[0].startswith("${")
    ):
        raise ValueError(f"expected one literal {key} mapping")
    return matches[0]


def _decode_segment(segment: str) -> Mapping[str, object]:
    padding = "=" * (-len(segment) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(segment + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Supabase session token is malformed") from exc
    if not isinstance(value, dict):
        raise ValueError("Supabase session token is malformed")
    return value


def validate_session(token: str, *, organization_id: str) -> dict[str, object]:
    segments = token.split(".")
    if len(segments) != 3:
        raise ValueError("Supabase session token is malformed")
    header = _decode_segment(segments[0])
    claims = _decode_segment(segments[1])
    metadata = claims.get("app_metadata")
    if (
        header.get("alg") != "ES256"
        or not isinstance(header.get("kid"), str)
        or not header["kid"]
        or not isinstance(claims.get("sub"), str)
        or not isinstance(metadata, dict)
        or metadata.get("organization_id") != organization_id
    ):
        raise ValueError("Supabase session does not satisfy the portal identity contract")
    return {"algorithm": "ES256", "kid_present": True}


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


def write_identity_environment(
    path: Path,
    *,
    email: str,
    password: str,
    organization_id: str,
    subject_id: str,
    anon_key: str,
) -> None:
    fields = {
        "CAPTAIN_PORTAL_DEMO_EMAIL": email,
        "CAPTAIN_PORTAL_DEMO_PASSWORD": password,
        "CAPTAIN_PORTAL_DEMO_ORGANIZATION_ID": organization_id,
        "CAPTAIN_PORTAL_DEMO_SUBJECT_ID": subject_id,
        "CAPTAIN_PORTAL_SUPABASE_ANON_KEY": anon_key,
    }
    if any("\n" in value or "\r" in value for value in fields.values()):
        raise ValueError("portal demo identity contains an unsafe value")
    _atomic_private_write(
        path,
        "".join(f"{key}={value}\n" for key, value in fields.items()),
    )


def _safe_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or parsed.scheme not in {"http", "https"}
        or (
            parsed.scheme == "http"
            and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        )
    ):
        raise ValueError("Supabase base URL must be HTTPS or loopback HTTP")
    return value.rstrip("/")


def _request_json(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    payload: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, method=method)
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with build_opener(_NoRedirect()).open(request, timeout=5.0) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError("Supabase identity operation failed") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("Supabase identity response exceeded the size limit")
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Supabase identity response was invalid") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("Supabase identity response was invalid")
    return decoded


def _read_existing(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def provision(
    *,
    base_url: str,
    compose_path: Path,
    output_path: Path,
    email: str,
    organization_id: str,
) -> dict[str, object]:
    base_url = _safe_base_url(base_url)
    compose = compose_path.resolve(strict=True).read_text(encoding="utf-8")
    service_key = extract_compose_value(compose, "SUPABASE_SERVICE_KEY")
    anon_key = extract_compose_value(compose, "ANON_KEY")
    existing = _read_existing(output_path)
    password = existing.get("CAPTAIN_PORTAL_DEMO_PASSWORD") or secrets.token_urlsafe(24)
    admin_headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
    }
    users = _request_json(
        "GET",
        f"{base_url}/auth/v1/admin/users?{urlencode({'page': 1, 'per_page': 1000})}",
        headers=admin_headers,
    ).get("users")
    if not isinstance(users, list):
        raise RuntimeError("Supabase user listing was invalid")
    matching = [user for user in users if isinstance(user, dict) and user.get("email") == email]
    payload = {
        "email": email,
        "password": password,
        "email_confirm": True,
        "app_metadata": {"organization_id": organization_id},
    }
    if len(matching) > 1:
        raise RuntimeError("Supabase portal identity is ambiguous")
    if matching:
        subject_id = matching[0].get("id")
        if not isinstance(subject_id, str):
            raise RuntimeError("Supabase portal identity was invalid")
        user = _request_json(
            "PUT",
            f"{base_url}/auth/v1/admin/users/{subject_id}",
            headers=admin_headers,
            payload=payload,
        )
        action = "updated"
    else:
        user = _request_json(
            "POST",
            f"{base_url}/auth/v1/admin/users",
            headers=admin_headers,
            payload=payload,
        )
        action = "created"
    subject_id = user.get("id")
    if not isinstance(subject_id, str) or not subject_id:
        raise RuntimeError("Supabase portal identity was invalid")
    session = _request_json(
        "POST",
        f"{base_url}/auth/v1/token?grant_type=password",
        headers={"apikey": anon_key},
        payload={"email": email, "password": password},
    )
    access_token = session.get("access_token")
    if not isinstance(access_token, str):
        raise RuntimeError("Supabase login did not return a session")
    summary = validate_session(access_token, organization_id=organization_id)
    write_identity_environment(
        output_path,
        email=email,
        password=password,
        organization_id=organization_id,
        subject_id=subject_id,
        anon_key=anon_key,
    )
    return {
        "status": "ready",
        "action": action,
        "algorithm": summary["algorithm"],
        "organization_bound": True,
        "secrets_emitted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--email", default="captain.portal.demo@example.invalid")
    parser.add_argument("--organization", default="captain-demo")
    args = parser.parse_args()
    result = provision(
        base_url=args.base_url,
        compose_path=args.compose,
        output_path=args.output,
        email=args.email,
        organization_id=args.organization,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
