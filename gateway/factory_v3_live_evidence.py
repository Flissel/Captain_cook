"""Independent, fail-closed verification for Factory V3 live evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from math import log2
import re
from typing import Protocol
from uuid import UUID
from urllib.parse import urlsplit

import httpx

from agenten.agent_factory.forge_contracts import (
    CodexBuildReceiptV1,
    CreationResultV2,
    ForgeBuildSkillUsageReceiptV1,
)
from agenten.agent_runtime.contracts import ArtifactRef


_AGENTS = (
    "account_researcher",
    "opportunity_qualifier",
    "deal_strategist",
    "executive_brief_writer",
)
_HANDOFFS = tuple(zip(_AGENTS, _AGENTS[1:]))
_REQUIRED_TOOLS = frozenset(
    {"account_snapshot_reader", "opportunity_scoring_rules"}
)
_SECRET_KEY_FRAGMENTS = (
    "apikey",
    "authorization",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
    "prompt",
    "holdout",
    "providerresponse",
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+|basic\s+[A-Za-z0-9+/=]+|"
    r"(?:api[_ -]?key|password|secret|token|credential)\s*=|"
    r"private.?key|system.?prompt|provider.?response|holdout|"
    r"\bsk_(?:live|test)_[A-Za-z0-9_-]+|\bsk-proj-[A-Za-z0-9_-]+|"
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bgh[pours]_[A-Za-z0-9_-]+|"
    r"\bxox[a-z]-[A-Za-z0-9-]+|\bAIza[A-Za-z0-9_-]+|"
    r"\b(?:token|secret|password|credential)[_-][A-Za-z0-9_+/-]{6,})"
)
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FactoryV3LiveEvidenceError(ValueError):
    """A sanitized live-evidence failure safe for operator output."""


@dataclass(frozen=True)
class FactoryV3AuthorityRecord:
    reference: ArtifactRef
    content: bytes


class FactoryV3LiveEvidenceAuthority(Protocol):
    """Read each producer independently instead of trusting a local summary."""

    def hermes_receipts(
        self, correlation_id: UUID
    ) -> tuple[FactoryV3AuthorityRecord, ...]: ...

    def codex_receipts(
        self, correlation_id: UUID
    ) -> tuple[FactoryV3AuthorityRecord, ...]: ...

    def minibook_results(
        self, creation_job_id: UUID
    ) -> tuple[FactoryV3AuthorityRecord, ...]: ...

    def captain_acceptances(
        self, factory_job_id: UUID
    ) -> tuple[FactoryV3AuthorityRecord, ...]: ...

    def prohibited_lifecycle_events(
        self, correlation_id: UUID
    ) -> tuple[FactoryV3AuthorityRecord, ...]: ...

    def minibook_cas(self, reference: ArtifactRef) -> bytes: ...

    def captain_cas(self, reference: ArtifactRef) -> bytes: ...


class _ArtifactReferenceLike(Protocol):
    uri: str
    sha256: str
    media_type: str


def _canonical_origin(value: str) -> str | None:
    if not value or value != value.strip() or "\\" in value or "%" in value:
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
    ):
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname.casefold()
    origin = f"{parsed.scheme}://{host}"
    if port is not None:
        origin = f"{origin}:{port}"
    return origin if value.rstrip("/").casefold() == origin.casefold() else None


class FactoryV3HttpEvidenceAuthority:
    """Bounded read-only client for the Gateway and Minibook authority views."""

    def __init__(
        self,
        *,
        gateway_origin: str,
        minibook_origin: str,
        captain_token: str,
        worker_token: str,
    ) -> None:
        self._gateway_origin = _canonical_origin(gateway_origin)
        self._minibook_origin = _canonical_origin(minibook_origin)
        if (
            self._gateway_origin is None
            or self._minibook_origin is None
            or self._gateway_origin == self._minibook_origin
            or not captain_token
            or not worker_token
        ):
            raise FactoryV3LiveEvidenceError(
                "live evidence authority configuration is invalid"
            )
        self._captain_token = captain_token
        self._worker_token = worker_token

    def _record_set(
        self,
        *,
        origin: str,
        path: str,
        token: str,
        authority: str,
    ) -> tuple[FactoryV3AuthorityRecord, ...]:
        payload = self._json_get(origin=origin, path=path, token=token)
        if (
            set(payload) != {"schema", "authority", "records"}
            or payload.get("schema")
            != "captain.factory-v3-authority-record-set.v1"
            or payload.get("authority") != authority
            or not isinstance(payload.get("records"), list)
            or len(payload["records"]) > 4
        ):
            raise FactoryV3LiveEvidenceError(
                "live evidence authority response is invalid"
            )
        records: list[FactoryV3AuthorityRecord] = []
        try:
            for item in payload["records"]:
                if not isinstance(item, dict) or set(item) != {
                    "reference",
                    "content_base64",
                }:
                    raise ValueError
                reference = ArtifactRef.model_validate(item["reference"])
                encoded = item["content_base64"]
                if not isinstance(encoded, str) or len(encoded) > 3_000_000:
                    raise ValueError
                content = base64.b64decode(encoded, validate=True)
                records.append(
                    FactoryV3AuthorityRecord(reference=reference, content=content)
                )
        except (TypeError, ValueError) as exc:
            raise FactoryV3LiveEvidenceError(
                "live evidence authority record is invalid"
            ) from exc
        return tuple(records)

    def _json_get(self, *, origin: str, path: str, token: str) -> Mapping[str, object]:
        try:
            with httpx.Client(follow_redirects=False, timeout=5.0) as client:
                response = client.get(
                    f"{origin}{path}",
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {token}",
                    },
                )
            if response.status_code != 200 or len(response.content) > 4_000_000:
                raise ValueError
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (httpx.HTTPError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise FactoryV3LiveEvidenceError("live evidence authority read failed") from exc

    def _cas_get(
        self,
        *,
        origin: str,
        path: str,
        token: str,
        reference: ArtifactRef,
    ) -> bytes:
        try:
            with httpx.Client(follow_redirects=False, timeout=10.0) as client:
                response = client.get(
                    f"{origin}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if (
                response.status_code != 200
                or len(response.content) > 32 * 1024 * 1024
                or response.headers.get("X-Artifact-Sha256") != reference.sha256
                or response.headers.get("Content-Type", "").split(";", 1)[0]
                != reference.media_type
                or hashlib.sha256(response.content).hexdigest() != reference.sha256
            ):
                raise ValueError
            return response.content
        except (httpx.HTTPError, ValueError) as exc:
            raise FactoryV3LiveEvidenceError("live CAS authority read failed") from exc

    def hermes_receipts(
        self, correlation_id: UUID
    ) -> tuple[FactoryV3AuthorityRecord, ...]:
        return self._record_set(
            origin=self._gateway_origin,
            path=f"/v1/factory/live-evidence/hermes/{correlation_id}",
            token=self._captain_token,
            authority="hermes",
        )

    def codex_receipts(
        self, correlation_id: UUID
    ) -> tuple[FactoryV3AuthorityRecord, ...]:
        return self._record_set(
            origin=self._gateway_origin,
            path=f"/v1/factory/live-evidence/codex/{correlation_id}",
            token=self._captain_token,
            authority="codex",
        )

    def minibook_results(
        self, creation_job_id: UUID
    ) -> tuple[FactoryV3AuthorityRecord, ...]:
        return self._record_set(
            origin=self._minibook_origin,
            path=f"/v3/creation/jobs/{creation_job_id}/live-result",
            token=self._worker_token,
            authority="minibook",
        )

    def captain_acceptances(
        self, factory_job_id: UUID
    ) -> tuple[FactoryV3AuthorityRecord, ...]:
        return self._record_set(
            origin=self._gateway_origin,
            path=f"/v1/factory/live-evidence/captain/{factory_job_id}",
            token=self._captain_token,
            authority="captain",
        )

    def prohibited_lifecycle_events(
        self, correlation_id: UUID
    ) -> tuple[FactoryV3AuthorityRecord, ...]:
        return self._record_set(
            origin=self._gateway_origin,
            path=f"/v1/factory/live-evidence/prohibited-events/{correlation_id}",
            token=self._captain_token,
            authority="captain-prohibited-lifecycle",
        )

    def minibook_cas(self, reference: ArtifactRef) -> bytes:
        return self._cas_get(
            origin=self._minibook_origin,
            path=f"/v3/creation/artifacts/{reference.sha256}",
            token=self._worker_token,
            reference=reference,
        )

    def captain_cas(self, reference: ArtifactRef) -> bytes:
        return self._cas_get(
            origin=self._gateway_origin,
            path=f"/v1/factory/live-evidence/cas/{reference.sha256}",
            token=self._captain_token,
            reference=reference,
        )


@dataclass(frozen=True)
class FactoryV3LiveEvidenceIndex:
    preflight_run_id: UUID
    correlation_id: UUID
    factory_job_id: UUID
    creation_job_id: UUID
    preflight_report_sha256: str

    @classmethod
    def parse(cls, payload: Mapping[str, object]) -> "FactoryV3LiveEvidenceIndex":
        assert_factory_v3_live_secret_free(payload)
        if set(payload) != {
            "schema",
            "preflight_run_id",
            "correlation_id",
            "factory_job_id",
            "creation_job_id",
            "preflight_report_sha256",
        } or payload.get("schema") != "captain.factory-v3-live-evidence-index.v1":
            raise FactoryV3LiveEvidenceError("live evidence index is invalid")
        try:
            digest = str(payload["preflight_report_sha256"])
            if _SHA256.fullmatch(digest) is None:
                raise ValueError
            return cls(
                preflight_run_id=UUID(str(payload["preflight_run_id"])),
                correlation_id=UUID(str(payload["correlation_id"])),
                factory_job_id=UUID(str(payload["factory_job_id"])),
                creation_job_id=UUID(str(payload["creation_job_id"])),
                preflight_report_sha256=digest,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FactoryV3LiveEvidenceError(
                "live evidence index identity is invalid"
            ) from exc


@dataclass(frozen=True)
class FactoryV3CaptainAcceptance:
    preflight_run_id: UUID
    preflight_report_sha256: str
    correlation_id: UUID
    factory_job_id: UUID
    creation_job_id: UUID
    subject_version: int
    minibook_result_ref: ArtifactRef
    team_manifest_ref: ArtifactRef
    source_archive_ref: ArtifactRef
    cas_readback_receipt_ref: ArtifactRef
    accepted_at: datetime

    @classmethod
    def parse(cls, payload: Mapping[str, object]) -> "FactoryV3CaptainAcceptance":
        assert_factory_v3_live_secret_free(payload)
        fields = {
            "schema",
            "producer",
            "outcome",
            "preflight_run_id",
            "preflight_report_sha256",
            "correlation_id",
            "factory_job_id",
            "creation_job_id",
            "subject_version",
            "minibook_result_ref",
            "team_manifest_ref",
            "source_archive_ref",
            "cas_readback_receipt_ref",
            "accepted_at",
        }
        if (
            set(payload) != fields
            or payload.get("schema") != "captain.factory-v3-live-acceptance.v1"
            or payload.get("producer") != "captain"
            or payload.get("outcome") != "accepted"
        ):
            raise FactoryV3LiveEvidenceError("Captain live acceptance is invalid")
        try:
            subject_version = payload["subject_version"]
            if (
                not isinstance(subject_version, int)
                or isinstance(subject_version, bool)
                or subject_version < 1
            ):
                raise ValueError
            digest = str(payload["preflight_report_sha256"])
            if _SHA256.fullmatch(digest) is None:
                raise ValueError
            accepted_text = str(payload["accepted_at"])
            if not accepted_text.endswith("Z"):
                raise ValueError
            accepted_at = datetime.fromisoformat(
                accepted_text.replace("Z", "+00:00")
            )
            if (
                accepted_at.tzinfo is None
                or accepted_at.utcoffset()
                != timezone.utc.utcoffset(accepted_at)
            ):
                raise ValueError
            return cls(
                preflight_run_id=UUID(str(payload["preflight_run_id"])),
                preflight_report_sha256=digest,
                correlation_id=UUID(str(payload["correlation_id"])),
                factory_job_id=UUID(str(payload["factory_job_id"])),
                creation_job_id=UUID(str(payload["creation_job_id"])),
                subject_version=subject_version,
                minibook_result_ref=ArtifactRef.model_validate(
                    payload["minibook_result_ref"]
                ),
                team_manifest_ref=ArtifactRef.model_validate(
                    payload["team_manifest_ref"]
                ),
                source_archive_ref=ArtifactRef.model_validate(
                    payload["source_archive_ref"]
                ),
                cas_readback_receipt_ref=ArtifactRef.model_validate(
                    payload["cas_readback_receipt_ref"]
                ),
                accepted_at=accepted_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FactoryV3LiveEvidenceError(
                "Captain live acceptance identity is invalid"
            ) from exc


@dataclass(frozen=True)
class FactoryV3LiveVerifiedEvidence:
    preflight_run_id: UUID
    correlation_id: UUID
    factory_job_id: UUID
    creation_job_id: UUID
    hermes_receipt_sha256: str
    codex_receipt_sha256: str
    minibook_result_sha256: str
    captain_acceptance_sha256: str
    source_archive_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "captain.factory-v3-live-verified-evidence.v1",
            "status": "verified",
            "preflight_run_id": str(self.preflight_run_id),
            "correlation_id": str(self.correlation_id),
            "factory_job_id": str(self.factory_job_id),
            "creation_job_id": str(self.creation_job_id),
            "hermes_receipt_sha256": self.hermes_receipt_sha256,
            "codex_receipt_sha256": self.codex_receipt_sha256,
            "minibook_result_sha256": self.minibook_result_sha256,
            "captain_acceptance_sha256": self.captain_acceptance_sha256,
            "source_archive_sha256": self.source_archive_sha256,
            "hermes_invocation_count": 1,
            "codex_session_count": 1,
            "agent_count": 4,
            "required_tool_count": 2,
        }


def _explicit_digest_key(key: str | None) -> bool:
    return bool(
        key
        and (
            key.casefold() in {"idempotency_key", "seal_idempotency_key", "effect_key"}
            or re.fullmatch(
                r"(?:[a-z0-9]+_)*(?:sha256|digest)(?:_[a-z0-9]+)*",
                key.casefold(),
            )
        )
    )


def _opaque_high_entropy(value: str, key: str | None) -> bool:
    if _UUID.fullmatch(value):
        return False
    if _SHA256.fullmatch(value):
        return not _explicit_digest_key(key)
    if not re.fullmatch(r"[A-Za-z0-9_+/-]{32,}={0,2}", value):
        return False
    entropy = -sum(
        (value.count(character) / len(value))
        * log2(value.count(character) / len(value))
        for character in set(value)
    )
    return entropy >= 3.5 and not _explicit_digest_key(key)


def assert_factory_v3_live_secret_free(
    value: object,
    *,
    key: str | None = None,
) -> None:
    """Recursively reject secret-bearing keys and recognizable token values."""

    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            child_key = str(raw_key)
            normalized_key = re.sub(r"[^a-z0-9]", "", child_key.casefold())
            if any(
                fragment in normalized_key for fragment in _SECRET_KEY_FRAGMENTS
            ):
                raise FactoryV3LiveEvidenceError(
                    "live evidence contains forbidden secret metadata"
                )
            assert_factory_v3_live_secret_free(item, key=child_key)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            assert_factory_v3_live_secret_free(item, key=key)
        return
    if isinstance(value, str) and (
        _SECRET_VALUE.search(value) or _opaque_high_entropy(value, key)
    ):
        raise FactoryV3LiveEvidenceError(
            "live evidence contains forbidden secret material"
        )


def _record_json(
    record: FactoryV3AuthorityRecord,
    *,
    maximum_bytes: int = 2 * 1024 * 1024,
) -> Mapping[str, object]:
    if (
        record.reference.media_type != "application/json"
        or len(record.content) > maximum_bytes
        or hashlib.sha256(record.content).hexdigest() != record.reference.sha256
    ):
        raise FactoryV3LiveEvidenceError("authority record binding is invalid")
    try:
        payload = json.loads(record.content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactoryV3LiveEvidenceError("authority record JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise FactoryV3LiveEvidenceError("authority record payload is invalid")
    assert_factory_v3_live_secret_free(payload)
    return payload


def _exactly_one(
    records: tuple[FactoryV3AuthorityRecord, ...],
    *,
    authority: str,
) -> FactoryV3AuthorityRecord:
    if len(records) != 1:
        raise FactoryV3LiveEvidenceError(
            f"{authority} authority did not return exactly one record"
        )
    return records[0]


def _ref_identity(reference: _ArtifactReferenceLike) -> tuple[str, str, str]:
    return (reference.uri, reference.sha256, reference.media_type)


def _bound_cas_read(
    reader: FactoryV3LiveEvidenceAuthority,
    reference: ArtifactRef,
) -> bytes:
    try:
        minibook = reader.minibook_cas(reference)
        captain = reader.captain_cas(reference)
    except Exception as exc:
        raise FactoryV3LiveEvidenceError("independent CAS read failed") from exc
    if (
        minibook != captain
        or hashlib.sha256(minibook).hexdigest() != reference.sha256
    ):
        raise FactoryV3LiveEvidenceError("independent CAS readback differs")
    return minibook


def _validate_team_manifest(content: bytes) -> None:
    try:
        team = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactoryV3LiveEvidenceError("team manifest is invalid") from exc
    assert_factory_v3_live_secret_free(team)
    if not isinstance(team, dict) or set(team) != {
        "schema",
        "agents",
        "handoffs",
        "tools",
        "source_files",
        "test_files",
    } or team.get("schema") != "minibook.team-manifest.v2":
        raise FactoryV3LiveEvidenceError("team manifest is invalid")
    try:
        agents = tuple(item["agent_id"] for item in team["agents"])
        handoffs = tuple(
            (item["from_agent_id"], item["to_agent_id"])
            for item in team["handoffs"]
        )
        tools = {item["tool_id"]: item for item in team["tools"]}
    except (KeyError, TypeError) as exc:
        raise FactoryV3LiveEvidenceError("team manifest topology is invalid") from exc
    if agents != _AGENTS or handoffs != _HANDOFFS or not _REQUIRED_TOOLS.issubset(tools):
        raise FactoryV3LiveEvidenceError("team manifest authority differs")
    for tool_id in _REQUIRED_TOOLS:
        tool = tools[tool_id]
        if (
            not isinstance(tool, dict)
            or set(tool) != {
                "tool_id",
                "input_schema",
                "output_schema",
                "implementation_path",
                "test_path",
            }
            or not isinstance(tool["input_schema"], dict)
            or not isinstance(tool["output_schema"], dict)
            or not str(tool["implementation_path"]).startswith("src/")
            or not str(tool["test_path"]).startswith("tests/")
        ):
            raise FactoryV3LiveEvidenceError("required tool evidence is invalid")


def verify_factory_v3_live_evidence(
    index_payload: Mapping[str, object],
    *,
    authority: FactoryV3LiveEvidenceAuthority,
) -> FactoryV3LiveVerifiedEvidence:
    """Verify four producer records and two-owner CAS reads under one identity."""

    index = FactoryV3LiveEvidenceIndex.parse(index_payload)
    hermes_record = _exactly_one(
        authority.hermes_receipts(index.correlation_id), authority="Hermes"
    )
    codex_record = _exactly_one(
        authority.codex_receipts(index.correlation_id), authority="Codex"
    )
    result_record = _exactly_one(
        authority.minibook_results(index.creation_job_id), authority="Minibook"
    )
    acceptance_record = _exactly_one(
        authority.captain_acceptances(index.factory_job_id), authority="Captain"
    )
    if authority.prohibited_lifecycle_events(index.correlation_id):
        raise FactoryV3LiveEvidenceError(
            "prohibited lifecycle evidence exists for live run"
        )
    try:
        hermes = ForgeBuildSkillUsageReceiptV1.model_validate(
            _record_json(hermes_record)
        )
        codex = CodexBuildReceiptV1.model_validate(_record_json(codex_record))
        result = CreationResultV2.model_validate(_record_json(result_record))
        acceptance = FactoryV3CaptainAcceptance.parse(
            _record_json(acceptance_record)
        )
    except FactoryV3LiveEvidenceError:
        raise
    except Exception as exc:
        raise FactoryV3LiveEvidenceError("typed authority receipt is invalid") from exc

    identity = (
        index.creation_job_id,
        index.factory_job_id,
        index.correlation_id,
    )
    if (
        (hermes.creation_job_id, hermes.factory_job_id, hermes.correlation_id)
        != identity
        or (codex.creation_job_id, codex.factory_job_id, codex.correlation_id)
        != identity
        or (result.creation_job_id, result.factory_job_id, result.correlation_id)
        != identity
        or (
            acceptance.creation_job_id,
            acceptance.factory_job_id,
            acceptance.correlation_id,
        )
        != identity
        or result.status != "succeeded"
        or result.subject_version != hermes.subject_version
        or result.subject_version != codex.subject_version
        or result.subject_version != acceptance.subject_version
        or acceptance.preflight_run_id != index.preflight_run_id
        or acceptance.preflight_report_sha256 != index.preflight_report_sha256
        or _ref_identity(result.hermes_usage_receipt_ref)
        != _ref_identity(hermes_record.reference)
        or _ref_identity(result.codex_build_receipt_ref)
        != _ref_identity(codex_record.reference)
        or _ref_identity(acceptance.minibook_result_ref)
        != _ref_identity(result_record.reference)
        or _ref_identity(acceptance.team_manifest_ref)
        != _ref_identity(result.team_manifest_ref)
        or _ref_identity(acceptance.source_archive_ref)
        != _ref_identity(result.source_archive_ref)
        or _ref_identity(acceptance.cas_readback_receipt_ref)
        != _ref_identity(result.cas_readback_receipt_ref)
    ):
        raise FactoryV3LiveEvidenceError("live authority identities differ")

    team_manifest_ref = result.team_manifest_ref
    source_archive_ref = result.source_archive_ref
    readback_receipt_ref = result.cas_readback_receipt_ref
    if (
        team_manifest_ref is None
        or source_archive_ref is None
        or readback_receipt_ref is None
    ):
        raise FactoryV3LiveEvidenceError("successful Minibook result is incomplete")
    if (
        _bound_cas_read(authority, hermes_record.reference)
        != hermes_record.content
        or _bound_cas_read(authority, codex_record.reference)
        != codex_record.content
        or _bound_cas_read(authority, result_record.reference)
        != result_record.content
    ):
        raise FactoryV3LiveEvidenceError("authority receipt CAS readback differs")
    team_bytes = _bound_cas_read(authority, team_manifest_ref)
    _bound_cas_read(authority, source_archive_ref)
    readback_bytes = _bound_cas_read(authority, readback_receipt_ref)
    _validate_team_manifest(team_bytes)
    try:
        readback = json.loads(readback_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactoryV3LiveEvidenceError("Minibook readback receipt is invalid") from exc
    assert_factory_v3_live_secret_free(readback)
    expected = {
        (team_manifest_ref.uri, team_manifest_ref.sha256),
        (source_archive_ref.uri, source_archive_ref.sha256),
    }
    try:
        actual = {
            (item["uri"], item["sha256"])
            for item in readback["artifacts"]
            if item.get("verified") is True
        }
    except (KeyError, TypeError) as exc:
        raise FactoryV3LiveEvidenceError("Minibook readback receipt is invalid") from exc
    if (
        not isinstance(readback, dict)
        or readback.get("schema") != "minibook.cas-readback-receipt.v1"
        or not expected.issubset(actual)
    ):
        raise FactoryV3LiveEvidenceError("Minibook readback receipt is invalid")
    verified = FactoryV3LiveVerifiedEvidence(
        preflight_run_id=index.preflight_run_id,
        correlation_id=index.correlation_id,
        factory_job_id=index.factory_job_id,
        creation_job_id=index.creation_job_id,
        hermes_receipt_sha256=hermes_record.reference.sha256,
        codex_receipt_sha256=codex_record.reference.sha256,
        minibook_result_sha256=result_record.reference.sha256,
        captain_acceptance_sha256=acceptance_record.reference.sha256,
        source_archive_sha256=source_archive_ref.sha256,
    )
    assert_factory_v3_live_secret_free(verified.as_dict())
    return verified


__all__ = [
    "FactoryV3AuthorityRecord",
    "FactoryV3LiveEvidenceAuthority",
    "FactoryV3LiveEvidenceError",
    "FactoryV3LiveEvidenceIndex",
    "FactoryV3LiveVerifiedEvidence",
    "FactoryV3HttpEvidenceAuthority",
    "assert_factory_v3_live_secret_free",
    "verify_factory_v3_live_evidence",
]
