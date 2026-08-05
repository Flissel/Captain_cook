"""Independent Bearer and OAuth2 provider with durable secret-free receipts."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import jwt
from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator


Clock = Callable[[], datetime]

_ENVIRONMENT_NAMES = {
    "CAPTAIN_PROVIDER_DATABASE_PATH": "database_path",
    "CAPTAIN_PROVIDER_ISSUER": "issuer",
    "CAPTAIN_PROVIDER_AUDIENCE": "audience",
    "CAPTAIN_PROVIDER_BEARER_TOKEN": "bearer_token",
    "CAPTAIN_PROVIDER_OAUTH_CLIENT_ID": "oauth_client_id",
    "CAPTAIN_PROVIDER_OAUTH_CLIENT_SECRET": "oauth_client_secret",
    "CAPTAIN_PROVIDER_OAUTH_SIGNING_SECRET": "oauth_signing_secret",
    "CAPTAIN_PROVIDER_AUDIT_TOKEN": "audit_token",
}


@dataclass(frozen=True, repr=False)
class ProviderSettings:
    database_path: Path
    issuer: str
    audience: str
    bearer_token: str = field(repr=False)
    oauth_client_id: str
    oauth_client_secret: str = field(repr=False)
    oauth_signing_secret: str = field(repr=False)
    audit_token: str = field(repr=False)

    def __post_init__(self) -> None:
        issuer = urlsplit(self.issuer)
        if (
            issuer.scheme != "https"
            or not issuer.hostname
            or issuer.username is not None
            or issuer.password is not None
            or issuer.path not in {"", "/"}
            or issuer.query
            or issuer.fragment
        ):
            raise ValueError("provider issuer must be a safe HTTPS origin")
        if not self.audience or any(character.isspace() for character in self.audience):
            raise ValueError("provider audience must be a non-empty identifier")
        protected = (
            self.bearer_token,
            self.oauth_client_secret,
            self.oauth_signing_secret,
            self.audit_token,
        )
        if any(len(value) < 24 for value in protected) or len(set(protected)) != len(
            protected
        ):
            raise ValueError("provider secrets must be long and distinct")
        if not self.oauth_client_id or any(
            character.isspace() for character in self.oauth_client_id
        ):
            raise ValueError("OAuth client ID must be a non-empty identifier")


def load_settings(environ: Mapping[str, str]) -> ProviderSettings:
    missing = tuple(
        name for name in _ENVIRONMENT_NAMES if not environ.get(name, "").strip()
    )
    if missing:
        raise ValueError("controlled provider configuration is incomplete")
    values = {
        field_name: environ[environment_name]
        for environment_name, field_name in _ENVIRONMENT_NAMES.items()
    }
    values["database_path"] = Path(values["database_path"])
    return ProviderSettings(**values)


class ProbeRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    probe_id: UUID
    correlation_id: UUID
    setup_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProbeReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["captain.controlled-provider-receipt.v1"] = Field(
        alias="schema"
    )
    trace_id: UUID
    kind: Literal["bearer", "oauth2"]
    probe_id: UUID
    correlation_id: UUID
    setup_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: datetime
    proof_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("occurred_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("provider receipt time must be UTC")
        return value.astimezone(timezone.utc)


class ReceiptStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS provider_receipts (
                       trace_id TEXT PRIMARY KEY,
                       effect_key TEXT NOT NULL UNIQUE,
                       request_sha256 TEXT NOT NULL,
                       payload TEXT NOT NULL
                   )"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def record(
        self,
        *,
        kind: Literal["bearer", "oauth2"],
        request: ProbeRequestV1,
        occurred_at: datetime,
    ) -> ProbeReceiptV1:
        public_request = request.model_dump(mode="json")
        request_sha256 = _sha256(public_request)
        effect_key = f"{kind}:{request.probe_id}"
        trace_id = uuid5(NAMESPACE_URL, f"captain-provider:{effect_key}:{request_sha256}")
        unsigned = {
            "schema": "captain.controlled-provider-receipt.v1",
            "trace_id": str(trace_id),
            "kind": kind,
            **public_request,
            "occurred_at": _require_utc(occurred_at).isoformat(),
        }
        receipt = ProbeReceiptV1.model_validate(
            {**unsigned, "proof_sha256": _sha256(unsigned)}
        )
        payload = receipt.model_dump_json(by_alias=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_sha256, payload FROM provider_receipts WHERE effect_key = ?",
                (effect_key,),
            ).fetchone()
            if row is not None:
                if row["request_sha256"] != request_sha256:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="provider probe already exists with different content",
                    )
                return ProbeReceiptV1.model_validate_json(row["payload"])
            connection.execute(
                "INSERT INTO provider_receipts "
                "(trace_id, effect_key, request_sha256, payload) VALUES (?, ?, ?, ?)",
                (str(trace_id), effect_key, request_sha256, payload),
            )
        return receipt

    def get(self, trace_id: UUID) -> ProbeReceiptV1:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM provider_receipts WHERE trace_id = ?",
                (str(trace_id),),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trace not found")
        return ProbeReceiptV1.model_validate_json(row["payload"])


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("provider clock must return UTC")
    return value.astimezone(timezone.utc)


def _bearer(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, separator, value = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer identity",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return value


def _require_exact_bearer(request: Request, expected: str) -> None:
    if not secrets.compare_digest(_bearer(request), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer identity",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _basic_credentials(request: Request) -> tuple[str, str]:
    authorization = request.headers.get("authorization", "")
    scheme, separator, value = authorization.partition(" ")
    if separator != " " or scheme.lower() != "basic" or not value:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid client")
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
        client_id, client_secret = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid client") from None
    return client_id, client_secret


def create_app(*, settings: ProviderSettings, clock: Clock | None = None) -> FastAPI:
    now = clock or (lambda: datetime.now(timezone.utc))
    store = ReceiptStore(settings.database_path)
    app = FastAPI(title="Captain Controlled Integration Provider")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "captain-controlled-provider"}

    @app.post("/v1/bearer/probes", response_model=ProbeReceiptV1)
    async def bearer_probe(
        probe: ProbeRequestV1,
        request: Request,
    ) -> ProbeReceiptV1:
        _require_exact_bearer(request, settings.bearer_token)
        return store.record(kind="bearer", request=probe, occurred_at=now())

    @app.post("/oauth/token")
    async def oauth_token(request: Request) -> dict[str, object]:
        client_id, client_secret = _basic_credentials(request)
        if not (
            secrets.compare_digest(client_id, settings.oauth_client_id)
            and secrets.compare_digest(client_secret, settings.oauth_client_secret)
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid client")
        form = parse_qs((await request.body()).decode("utf-8"), strict_parsing=True)
        if form.get("grant_type") != ["client_credentials"] or form.get("scope") != [
            "probe:read"
        ]:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid grant")
        issued_at = _require_utc(now())
        claims = {
            "iss": settings.issuer,
            "aud": settings.audience,
            "sub": settings.oauth_client_id,
            "scope": "probe:read",
            "iat": int(issued_at.timestamp()),
            "exp": int((issued_at + timedelta(minutes=5)).timestamp()),
            "jti": str(uuid4()),
        }
        token = jwt.encode(claims, settings.oauth_signing_secret, algorithm="HS256")
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": "probe:read",
        }

    @app.post("/v1/oauth2/probes", response_model=ProbeReceiptV1)
    async def oauth_probe(
        probe: ProbeRequestV1,
        request: Request,
    ) -> ProbeReceiptV1:
        token = _bearer(request)
        try:
            claims = jwt.decode(
                token,
                settings.oauth_signing_secret,
                algorithms=["HS256"],
                audience=settings.audience,
                issuer=settings.issuer,
                options={"verify_exp": False, "verify_iat": False},
            )
            current = int(_require_utc(now()).timestamp())
            if (
                claims.get("sub") != settings.oauth_client_id
                or claims.get("scope") != "probe:read"
                or not isinstance(claims.get("exp"), int)
                or claims["exp"] <= current
            ):
                raise jwt.InvalidTokenError
        except jwt.PyJWTError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid OAuth access token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from None
        return store.record(kind="oauth2", request=probe, occurred_at=now())

    @app.get("/v1/audit/traces/{trace_id}", response_model=ProbeReceiptV1)
    async def audit_trace(trace_id: UUID, request: Request) -> ProbeReceiptV1:
        _require_exact_bearer(request, settings.audit_token)
        return store.get(trace_id)

    return app
