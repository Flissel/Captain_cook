"""Independent, fail-closed verification for Factory V3 live evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
from math import log2
import re
from typing import Callable, Protocol
from uuid import UUID
from urllib.parse import urlsplit
import zipfile

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agenten.agent_factory.forge_contracts import (
    CodexBuildReceiptV1,
    CreationResultV2,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
    ForgeBuildSkillUsageReceiptV1,
    PackageBuildEvidenceV1,
    PackageRealCaseEvidenceV1,
)
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.factory_v3_live_authority import FactoryV3LiveAuthorityBinding


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

    def live_attestations(
        self, correlation_id: UUID
    ) -> tuple[FactoryV3AuthorityRecord, ...]: ...

    def minibook_cas(self, reference: ArtifactRef) -> bytes: ...

    def captain_cas(self, reference: ArtifactRef) -> bytes: ...

    def public_cas(self, reference: ArtifactRef) -> bytes: ...


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
        authority_binding: FactoryV3LiveAuthorityBinding,
        captain_token: str,
        worker_token: str,
    ) -> None:
        self._authority_binding_sha256 = authority_binding.sha256
        self._gateway_origin = _canonical_origin(authority_binding.gateway.origin)
        self._minibook_origin = _canonical_origin(authority_binding.minibook.origin)
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
        token: str | None,
        reference: ArtifactRef,
    ) -> bytes:
        try:
            headers = {} if token is None else {"Authorization": f"Bearer {token}"}
            with httpx.Client(follow_redirects=False, timeout=10.0) as client:
                response = client.get(
                    f"{origin}{path}",
                    headers=headers,
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

    def live_attestations(
        self, correlation_id: UUID
    ) -> tuple[FactoryV3AuthorityRecord, ...]:
        return self._record_set(
            origin=self._gateway_origin,
            path=f"/v1/factory/live-evidence/attestations/{correlation_id}",
            token=self._captain_token,
            authority="captain-live-runner",
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

    def public_cas(self, reference: ArtifactRef) -> bytes:
        return self._cas_get(
            origin=self._gateway_origin,
            path=f"/v1/factory/live-evidence/public-cas/{reference.sha256}",
            token=None,
            reference=reference,
        )


@dataclass(frozen=True)
class FactoryV3LiveEvidenceIndex:
    preflight_run_id: UUID
    correlation_id: UUID
    factory_job_id: UUID
    creation_job_id: UUID
    preflight_report_sha256: str
    authority_binding_sha256: str
    active_released_skill_sha256: str

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
            "authority_binding_sha256",
            "active_released_skill_sha256",
        } or payload.get("schema") != "captain.factory-v3-live-evidence-index.v1":
            raise FactoryV3LiveEvidenceError("live evidence index is invalid")
        try:
            digest = str(payload["preflight_report_sha256"])
            authority_digest = str(payload["authority_binding_sha256"])
            skill_digest = str(payload["active_released_skill_sha256"])
            if any(
                _SHA256.fullmatch(value) is None
                for value in (digest, authority_digest, skill_digest)
            ):
                raise ValueError
            return cls(
                preflight_run_id=UUID(str(payload["preflight_run_id"])),
                correlation_id=UUID(str(payload["correlation_id"])),
                factory_job_id=UUID(str(payload["factory_job_id"])),
                creation_job_id=UUID(str(payload["creation_job_id"])),
                preflight_report_sha256=digest,
                authority_binding_sha256=authority_digest,
                active_released_skill_sha256=skill_digest,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FactoryV3LiveEvidenceError(
                "live evidence index identity is invalid"
            ) from exc


@dataclass(frozen=True)
class FactoryV3CaptainAcceptance:
    preflight_run_id: UUID
    preflight_report_sha256: str
    authority_binding_sha256: str
    active_released_skill_sha256: str
    correlation_id: UUID
    factory_job_id: UUID
    creation_job_id: UUID
    subject_version: int
    minibook_result_ref: ArtifactRef
    team_manifest_ref: ArtifactRef
    source_archive_ref: ArtifactRef
    cas_readback_receipt_ref: ArtifactRef
    live_attestation_ref: ArtifactRef
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
            "authority_binding_sha256",
            "active_released_skill_sha256",
            "correlation_id",
            "factory_job_id",
            "creation_job_id",
            "subject_version",
            "minibook_result_ref",
            "team_manifest_ref",
            "source_archive_ref",
            "cas_readback_receipt_ref",
            "live_attestation_ref",
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
            authority_digest = str(payload["authority_binding_sha256"])
            skill_digest = str(payload["active_released_skill_sha256"])
            if any(
                _SHA256.fullmatch(value) is None
                for value in (digest, authority_digest, skill_digest)
            ):
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
                authority_binding_sha256=authority_digest,
                active_released_skill_sha256=skill_digest,
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
                live_attestation_ref=ArtifactRef.model_validate(
                    payload["live_attestation_ref"]
                ),
                accepted_at=accepted_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FactoryV3LiveEvidenceError(
                "Captain live acceptance identity is invalid"
            ) from exc


_LIVE_ATTESTATION_UNSIGNED_FIELDS = {
    "schema",
    "runner_id",
    "runner_key_id",
    "mode",
    "preflight_run_id",
    "preflight_report_sha256",
    "authority_binding_sha256",
    "active_released_skill_name",
    "active_released_skill_sha256",
    "correlation_id",
    "factory_job_id",
    "creation_job_id",
    "codex_binary_sha256",
    "pwsh_binary_sha256",
    "hermes_python_sha256",
    "codex_session_script_sha256",
    "hermes_runtime_sha256",
    "workspace_policy_ref",
    "assignment_ref",
    "effect_ref",
    "codex_session_ref",
    "workspace_snapshot_ref",
    "issued_at",
}


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class FactoryV3LiveRuntimeAttestation:
    runner_id: str
    preflight_run_id: UUID
    preflight_report_sha256: str
    authority_binding_sha256: str
    active_released_skill_sha256: str
    correlation_id: UUID
    factory_job_id: UUID
    creation_job_id: UUID
    workspace_policy_ref: ArtifactRef
    assignment_ref: ArtifactRef
    effect_ref: ArtifactRef
    codex_session_ref: ArtifactRef
    workspace_snapshot_ref: ArtifactRef
    issued_at: datetime

    @classmethod
    def parse(
        cls,
        payload: Mapping[str, object],
        *,
        binding: FactoryV3LiveAuthorityBinding,
        index: FactoryV3LiveEvidenceIndex | None,
    ) -> "FactoryV3LiveRuntimeAttestation":
        assert_factory_v3_live_secret_free(payload)
        if set(payload) != {*_LIVE_ATTESTATION_UNSIGNED_FIELDS, "signature_ed25519"}:
            raise FactoryV3LiveEvidenceError("live attestation is invalid")
        unsigned = {
            key: value for key, value in payload.items() if key != "signature_ed25519"
        }
        try:
            encoded_signature = payload["signature_ed25519"]
            if not isinstance(encoded_signature, str):
                raise ValueError
            signature = base64.b64decode(encoded_signature, validate=True)
            if len(signature) != 64:
                raise ValueError
            Ed25519PublicKey.from_public_bytes(
                binding.runner_attestation.public_key
            ).verify(signature, _canonical_bytes(unsigned))
            digests = tuple(
                str(payload[field])
                for field in (
                    "preflight_report_sha256",
                    "authority_binding_sha256",
                    "active_released_skill_sha256",
                    "codex_binary_sha256",
                    "pwsh_binary_sha256",
                    "hermes_python_sha256",
                    "codex_session_script_sha256",
                    "hermes_runtime_sha256",
                )
            )
            if any(_SHA256.fullmatch(value) is None for value in digests):
                raise ValueError
            issued_text = str(payload["issued_at"])
            if not issued_text.endswith("Z"):
                raise ValueError
            issued_at = datetime.fromisoformat(issued_text.replace("Z", "+00:00"))
            attestation = cls(
                runner_id=str(payload["runner_id"]),
                preflight_run_id=UUID(str(payload["preflight_run_id"])),
                preflight_report_sha256=digests[0],
                authority_binding_sha256=digests[1],
                active_released_skill_sha256=digests[2],
                correlation_id=UUID(str(payload["correlation_id"])),
                factory_job_id=UUID(str(payload["factory_job_id"])),
                creation_job_id=UUID(str(payload["creation_job_id"])),
                workspace_policy_ref=ArtifactRef.model_validate(
                    payload["workspace_policy_ref"]
                ),
                assignment_ref=ArtifactRef.model_validate(payload["assignment_ref"]),
                effect_ref=ArtifactRef.model_validate(payload["effect_ref"]),
                codex_session_ref=ArtifactRef.model_validate(
                    payload["codex_session_ref"]
                ),
                workspace_snapshot_ref=ArtifactRef.model_validate(
                    payload["workspace_snapshot_ref"]
                ),
                issued_at=issued_at,
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            InvalidSignature,
        ) as exc:
            raise FactoryV3LiveEvidenceError("live attestation is invalid") from exc
        expected_binding = (
            payload.get("schema")
            == "captain.factory-v3-live-runtime-attestation.v1"
            and payload.get("mode") == "live"
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", attestation.runner_id)
            is not None
            and payload.get("runner_key_id") == binding.runner_attestation.key_id
            and payload.get("authority_binding_sha256") == binding.sha256
            and payload.get("active_released_skill_name")
            == binding.released_skill.name
            and payload.get("active_released_skill_sha256")
            == binding.released_skill.sha256
            and payload.get("codex_binary_sha256")
            == binding.executables.codex.sha256
            and payload.get("pwsh_binary_sha256") == binding.executables.pwsh.sha256
            and payload.get("hermes_python_sha256")
            == binding.executables.hermes_python.sha256
            and payload.get("codex_session_script_sha256")
            == binding.codex_session.sha256
            and payload.get("hermes_runtime_sha256")
            == binding.hermes_runtime.runtime_sha256
            and attestation.issued_at.tzinfo is not None
            and attestation.issued_at.utcoffset()
            == timezone.utc.utcoffset(attestation.issued_at)
        )
        expected_index = bool(
            index is None
            or (
                attestation.preflight_run_id == index.preflight_run_id
                and attestation.preflight_report_sha256
                == index.preflight_report_sha256
                and attestation.authority_binding_sha256
                == index.authority_binding_sha256
                and attestation.active_released_skill_sha256
                == index.active_released_skill_sha256
                and attestation.correlation_id == index.correlation_id
                and attestation.factory_job_id == index.factory_job_id
                and attestation.creation_job_id == index.creation_job_id
            )
        )
        if not expected_binding or not expected_index:
            raise FactoryV3LiveEvidenceError("live attestation authority differs")
        return attestation


def issue_factory_v3_live_runtime_attestation(
    payload: Mapping[str, object],
    *,
    binding: FactoryV3LiveAuthorityBinding,
    signer: Callable[[bytes], bytes],
) -> dict[str, object]:
    """Production issuer seam for an authorized live runner signing one session."""

    if set(payload) != _LIVE_ATTESTATION_UNSIGNED_FIELDS:
        raise FactoryV3LiveEvidenceError("live attestation is invalid")
    unsigned = dict(payload)
    try:
        signature = signer(_canonical_bytes(unsigned))
    except Exception as exc:
        raise FactoryV3LiveEvidenceError("live attestation signing failed") from exc
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise FactoryV3LiveEvidenceError("live attestation signing failed")
    signed = unsigned | {
        "signature_ed25519": base64.b64encode(signature).decode("ascii")
    }
    FactoryV3LiveRuntimeAttestation.parse(
        signed,
        binding=binding,
        index=None,
    )
    return signed


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
            key.casefold()
            in {
                "idempotency_key",
                "seal_idempotency_key",
                "effect_key",
                "signature_ed25519",
            }
            or re.fullmatch(
                r"(?:[a-z0-9]+_)*(?:sha256|digest)(?:_[a-z0-9]+)*",
                key.casefold(),
            )
        )
    )


def _opaque_high_entropy(value: str, key: str | None) -> bool:
    if _UUID.fullmatch(value):
        return False
    if (
        key is not None
        and (key.casefold() == "id" or key.casefold().endswith("_id"))
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value)
    ):
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
        public = reader.public_cas(reference)
    except Exception as exc:
        raise FactoryV3LiveEvidenceError("independent public CAS read failed") from exc
    if (
        minibook != captain
        or minibook != public
        or hashlib.sha256(minibook).hexdigest() != reference.sha256
    ):
        raise FactoryV3LiveEvidenceError("independent public CAS readback differs")
    return minibook


def _public_cas_read(
    reader: FactoryV3LiveEvidenceAuthority,
    reference: ArtifactRef,
) -> bytes:
    try:
        content = reader.public_cas(reference)
    except Exception as exc:
        raise FactoryV3LiveEvidenceError("independent public CAS read failed") from exc
    if hashlib.sha256(content).hexdigest() != reference.sha256:
        raise FactoryV3LiveEvidenceError("independent public CAS readback differs")
    return content


def _public_json(
    reader: FactoryV3LiveEvidenceAuthority,
    reference: ArtifactRef,
) -> Mapping[str, object]:
    if reference.media_type != "application/json":
        raise FactoryV3LiveEvidenceError("public CAS JSON reference is invalid")
    content = _public_cas_read(reader, reference)
    if len(content) > 2 * 1024 * 1024:
        raise FactoryV3LiveEvidenceError("public CAS JSON is too large")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactoryV3LiveEvidenceError("public CAS JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise FactoryV3LiveEvidenceError("public CAS JSON is invalid")
    assert_factory_v3_live_secret_free(payload)
    return payload


def _validate_live_zip(content: bytes, *, workspace: bool) -> Mapping[str, object] | None:
    if len(content) > 32 * 1024 * 1024:
        raise FactoryV3LiveEvidenceError("live ZIP evidence is invalid")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > 4096:
                raise ValueError
            names: set[str] = set()
            total = 0
            for info in infos:
                name = info.filename
                parts = name.replace("\\", "/").split("/")
                if (
                    name in names
                    or name.startswith(("/", "\\"))
                    or "\\" in name
                    or any(part in {"", ".", ".."} for part in parts)
                    or any(
                        fragment in name.casefold()
                        for fragment in (".env", "secret", "credential", "token", "transcript")
                    )
                ):
                    raise ValueError
                names.add(name)
                total += info.file_size
                if total > 16 * 1024 * 1024:
                    raise ValueError
            if not workspace:
                return None
            manifest_name = "factory-v3-workspace-manifest.json"
            if manifest_name not in names:
                raise ValueError
            payload = json.loads(archive.read(manifest_name))
            if not isinstance(payload, dict):
                raise ValueError
            assert_factory_v3_live_secret_free(payload)
            return payload
    except (OSError, ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise FactoryV3LiveEvidenceError("live ZIP evidence is invalid") from exc


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


def _ref_equal(left: _ArtifactReferenceLike, right: _ArtifactReferenceLike) -> bool:
    return _ref_identity(left) == _ref_identity(right)


def _validate_supporting_json(payload: Mapping[str, object]) -> None:
    schema = payload.get("schema")
    if (
        not isinstance(schema, str)
        or re.fullmatch(
            r"(?:captain|minibook|hermes)\.[A-Za-z0-9][A-Za-z0-9._-]{2,127}",
            schema,
        )
        is None
    ):
        raise FactoryV3LiveEvidenceError("supporting live evidence is untyped")


def _validate_codex_session(
    payload: Mapping[str, object],
    *,
    index: FactoryV3LiveEvidenceIndex,
    binding: FactoryV3LiveAuthorityBinding,
    attestation: FactoryV3LiveRuntimeAttestation,
) -> None:
    fields = {
        "schema",
        "mode",
        "provider",
        "outcome",
        "session_id",
        "preflight_run_id",
        "correlation_id",
        "factory_job_id",
        "creation_job_id",
        "assignment_ref",
        "workspace_policy_ref",
        "workspace_snapshot_ref",
        "codex_binary_sha256",
        "codex_script_sha256",
        "completed_at",
    }
    if (
        set(payload) != fields
        or payload.get("schema") != "captain.codex-live-session-evidence.v1"
        or payload.get("mode") != "live"
        or payload.get("provider") != "codex"
        or payload.get("outcome") != "completed"
        or payload.get("codex_binary_sha256") != binding.executables.codex.sha256
        or payload.get("codex_script_sha256") != binding.codex_session.sha256
    ):
        raise FactoryV3LiveEvidenceError("Codex live session evidence is invalid")
    try:
        session_id = UUID(str(payload["session_id"]))
        completed_text = str(payload["completed_at"])
        if not completed_text.endswith("Z"):
            raise ValueError
        completed_at = datetime.fromisoformat(completed_text.replace("Z", "+00:00"))
        preflight_run_id = UUID(str(payload["preflight_run_id"]))
        correlation_id = UUID(str(payload["correlation_id"]))
        factory_job_id = UUID(str(payload["factory_job_id"]))
        creation_job_id = UUID(str(payload["creation_job_id"]))
        assignment_ref = ArtifactRef.model_validate(payload["assignment_ref"])
        policy_ref = ArtifactRef.model_validate(payload["workspace_policy_ref"])
        snapshot_ref = ArtifactRef.model_validate(payload["workspace_snapshot_ref"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FactoryV3LiveEvidenceError(
            "Codex live session evidence is invalid"
        ) from exc
    del session_id
    if (
        preflight_run_id != index.preflight_run_id
        or correlation_id != index.correlation_id
        or factory_job_id != index.factory_job_id
        or creation_job_id != index.creation_job_id
        or not _ref_equal(assignment_ref, attestation.assignment_ref)
        or not _ref_equal(policy_ref, attestation.workspace_policy_ref)
        or not _ref_equal(snapshot_ref, attestation.workspace_snapshot_ref)
        or completed_at.tzinfo is None
        or completed_at.utcoffset() != timezone.utc.utcoffset(completed_at)
    ):
        raise FactoryV3LiveEvidenceError("Codex live session identity differs")


def _validate_package_children(
    authority: FactoryV3LiveEvidenceAuthority,
    result: CreationResultV2,
) -> tuple[ArtifactRef, ...]:
    package_ref = result.package_manifest_ref
    if package_ref is None:
        raise FactoryV3LiveEvidenceError("Minibook package manifest is missing")
    payload = _public_json(authority, package_ref)
    if set(payload) != {"schema", "files"} or payload.get("schema") != (
        "minibook.team-package-manifest.v1"
    ):
        raise FactoryV3LiveEvidenceError("Minibook package manifest is invalid")
    entries = payload.get("files")
    if not isinstance(entries, list) or not entries or len(entries) > 4096:
        raise FactoryV3LiveEvidenceError("Minibook package children are invalid")
    children: list[ArtifactRef] = []
    paths: set[str] = set()
    for item in entries:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "uri",
            "sha256",
            "media_type",
        }:
            raise FactoryV3LiveEvidenceError("Minibook package child is invalid")
        path = item["path"]
        if (
            not isinstance(path, str)
            or path in paths
            or path.startswith(("/", "\\", "."))
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(
                fragment in path.casefold()
                for fragment in (".env", "secret", "credential", "token", "transcript")
            )
        ):
            raise FactoryV3LiveEvidenceError("Minibook package child path is invalid")
        paths.add(path)
        try:
            reference = ArtifactRef.model_validate(
                {key: item[key] for key in ("uri", "sha256", "media_type")}
            )
        except Exception as exc:
            raise FactoryV3LiveEvidenceError(
                "Minibook package child reference is invalid"
            ) from exc
        _public_cas_read(authority, reference)
        children.append(reference)
    expected = tuple(
        reference
        for reference in (
            result.team_manifest_ref,
            result.candidate_manifest_ref,
            result.hermes_usage_receipt_ref,
            result.codex_build_receipt_ref,
            *result.build_evidence_refs,
            *result.real_case_evidence_refs,
        )
        if reference is not None
    )
    identities = {_ref_identity(reference) for reference in children}
    if any(_ref_identity(reference) not in identities for reference in expected):
        raise FactoryV3LiveEvidenceError("Minibook package children are incomplete")
    return tuple(children)


def verify_factory_v3_live_evidence(
    index_payload: Mapping[str, object],
    *,
    authority: FactoryV3LiveEvidenceAuthority,
    authority_binding: FactoryV3LiveAuthorityBinding,
) -> FactoryV3LiveVerifiedEvidence:
    """Verify signed live execution and independently read every evidence edge."""

    index = FactoryV3LiveEvidenceIndex.parse(index_payload)
    if (
        index.authority_binding_sha256 != authority_binding.sha256
        or index.active_released_skill_sha256
        != authority_binding.released_skill.sha256
    ):
        raise FactoryV3LiveEvidenceError("live authority binding differs")
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
    attestation_record = _exactly_one(
        authority.live_attestations(index.correlation_id),
        authority="live attestation",
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
        attestation = FactoryV3LiveRuntimeAttestation.parse(
            _record_json(attestation_record),
            binding=authority_binding,
            index=index,
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
        or acceptance.authority_binding_sha256 != index.authority_binding_sha256
        or acceptance.active_released_skill_sha256
        != index.active_released_skill_sha256
        or attestation.issued_at > acceptance.accepted_at
        or hermes.released_skill.skill_id != authority_binding.released_skill.name
        or hermes.released_skill.content_sha256
        != authority_binding.released_skill.sha256
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
        or _ref_identity(acceptance.live_attestation_ref)
        != _ref_identity(attestation_record.reference)
    ):
        raise FactoryV3LiveEvidenceError("live authority identities differ")

    team_manifest_ref = result.team_manifest_ref
    source_archive_ref = result.source_archive_ref
    readback_receipt_ref = result.cas_readback_receipt_ref
    if (
        team_manifest_ref is None
        or source_archive_ref is None
        or readback_receipt_ref is None
        or result.package_manifest_ref is None
        or result.candidate_manifest_ref is None
    ):
        raise FactoryV3LiveEvidenceError("successful Minibook result is incomplete")
    if (
        _bound_cas_read(authority, hermes_record.reference)
        != hermes_record.content
        or _bound_cas_read(authority, codex_record.reference)
        != codex_record.content
        or _bound_cas_read(authority, result_record.reference)
        != result_record.content
        or _bound_cas_read(authority, acceptance_record.reference)
        != acceptance_record.content
        or _bound_cas_read(authority, attestation_record.reference)
        != attestation_record.content
    ):
        raise FactoryV3LiveEvidenceError("authority receipt CAS readback differs")

    try:
        assignment = FactoryBuildAssignmentV2.model_validate(
            _public_json(authority, attestation.assignment_ref)
        )
        effect = FactoryBuildEffectV2.model_validate(
            _public_json(authority, attestation.effect_ref)
        )
    except FactoryV3LiveEvidenceError:
        raise
    except Exception as exc:
        raise FactoryV3LiveEvidenceError(
            "live assignment or effect is invalid"
        ) from exc
    assignment_identity = (
        assignment.creation_job_id,
        assignment.factory_job_id,
        assignment.correlation_id,
    )
    effect_identity = (
        effect.creation_job_id,
        effect.factory_job_id,
        effect.correlation_id,
    )
    if (
        assignment_identity != identity
        or effect_identity != identity
        or assignment.assignment_id != effect.assignment_id
        or assignment.assignment_id != codex.assignment_id
        or assignment.effect_key != effect.effect_key
        or assignment.effect_key != codex.idempotency_key
        or assignment.workspace_policy_sha256
        != attestation.workspace_policy_ref.sha256
        or not _ref_equal(effect.hermes_usage_receipt_ref, hermes_record.reference)
        or not _ref_equal(effect.codex_build_receipt_ref, codex_record.reference)
        or not _ref_equal(effect.codex_session_ref, attestation.codex_session_ref)
        or not _ref_equal(
            effect.workspace_snapshot_ref,
            attestation.workspace_snapshot_ref,
        )
        or not _ref_equal(effect.source_archive_ref, source_archive_ref)
        or tuple(_ref_identity(item) for item in effect.build_evidence_refs)
        != tuple(_ref_identity(item) for item in result.build_evidence_refs)
        or tuple(_ref_identity(item) for item in effect.test_evidence_refs)
        != tuple(_ref_identity(item) for item in result.real_case_evidence_refs)
    ):
        raise FactoryV3LiveEvidenceError("live assignment or effect identity differs")

    workspace_policy = _public_json(authority, attestation.workspace_policy_ref)
    if (
        workspace_policy.get("schema")
        != "captain.factory-v3-workspace-policy.v1"
        or workspace_policy.get("network_allowed") is not False
        or workspace_policy.get("allowed_output_roots")
        != ["src", "tests", "evidence"]
    ):
        raise FactoryV3LiveEvidenceError("live workspace policy is invalid")

    for reference in hermes.evidence_refs:
        _validate_supporting_json(_public_json(authority, reference))
    _public_cas_read(authority, hermes.released_skill.content_ref)

    session_payload = _public_json(authority, codex.codex_session_ref)
    if not _ref_equal(codex.codex_session_ref, attestation.codex_session_ref):
        raise FactoryV3LiveEvidenceError("Codex live session reference differs")
    _validate_codex_session(
        session_payload,
        index=index,
        binding=authority_binding,
        attestation=attestation,
    )
    snapshot_bytes = _public_cas_read(authority, codex.workspace_snapshot_ref)
    if not _ref_equal(
        codex.workspace_snapshot_ref,
        attestation.workspace_snapshot_ref,
    ):
        raise FactoryV3LiveEvidenceError("Codex workspace snapshot reference differs")
    snapshot_manifest = _validate_live_zip(snapshot_bytes, workspace=True)
    if (
        snapshot_manifest is None
        or set(snapshot_manifest)
        != {"schema", "mode", "correlation_id", "assignment_sha256"}
        or snapshot_manifest.get("schema")
        != "captain.factory-v3-workspace-snapshot.v1"
        or snapshot_manifest.get("mode") != "live"
        or snapshot_manifest.get("correlation_id") != str(index.correlation_id)
        or snapshot_manifest.get("assignment_sha256")
        != attestation.assignment_ref.sha256
    ):
        raise FactoryV3LiveEvidenceError("Codex workspace snapshot is invalid")

    build_evidence: list[PackageBuildEvidenceV1] = []
    for reference in result.build_evidence_refs:
        try:
            item = PackageBuildEvidenceV1.model_validate(
                _public_json(authority, reference)
            )
        except FactoryV3LiveEvidenceError:
            raise
        except Exception as exc:
            raise FactoryV3LiveEvidenceError(
                "Minibook build evidence is invalid"
            ) from exc
        build_evidence.append(item)
    real_evidence: list[PackageRealCaseEvidenceV1] = []
    for reference in result.real_case_evidence_refs:
        try:
            item = PackageRealCaseEvidenceV1.model_validate(
                _public_json(authority, reference)
            )
        except FactoryV3LiveEvidenceError:
            raise
        except Exception as exc:
            raise FactoryV3LiveEvidenceError(
                "Minibook real-case evidence is invalid"
            ) from exc
        for evidence_reference in item.evidence_refs:
            _public_cas_read(authority, evidence_reference)
        real_evidence.append(item)
    if (
        not build_evidence
        or not real_evidence
        or tuple(_ref_identity(item) for item in codex.test_evidence_refs)
        != tuple(_ref_identity(item) for item in result.real_case_evidence_refs)
        or any(
            (
                item.assignment_id != assignment.assignment_id
                or (item.creation_job_id, item.factory_job_id, item.correlation_id)
                != identity
                or item.effect_key != assignment.effect_key
                or not _ref_equal(item.source_archive_ref, source_archive_ref)
                or not _ref_equal(item.codex_session_ref, codex.codex_session_ref)
                or not _ref_equal(
                    item.workspace_snapshot_ref,
                    codex.workspace_snapshot_ref,
                )
            )
            for item in build_evidence
        )
        or any(
            (
                item.assignment_id != assignment.assignment_id
                or (item.creation_job_id, item.factory_job_id, item.correlation_id)
                != identity
                or item.effect_key != assignment.effect_key
                or not _ref_equal(item.source_archive_ref, source_archive_ref)
                or not _ref_equal(item.codex_session_ref, codex.codex_session_ref)
                or tuple(item.assertion_ids) != tuple(codex.acceptance_assertion_ids)
            )
            for item in real_evidence
        )
    ):
        raise FactoryV3LiveEvidenceError("Minibook build evidence identity differs")

    package_children = _validate_package_children(authority, result)
    team_bytes = _bound_cas_read(authority, team_manifest_ref)
    candidate_bytes = _bound_cas_read(authority, result.candidate_manifest_ref)
    source_bytes = _bound_cas_read(authority, source_archive_ref)
    _bound_cas_read(authority, result.package_manifest_ref)
    readback_bytes = _bound_cas_read(authority, readback_receipt_ref)
    _validate_team_manifest(team_bytes)
    _validate_live_zip(source_bytes, workspace=False)
    try:
        candidate = json.loads(candidate_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactoryV3LiveEvidenceError("candidate manifest is invalid") from exc
    if not isinstance(candidate, dict):
        raise FactoryV3LiveEvidenceError("candidate manifest is invalid")
    assert_factory_v3_live_secret_free(candidate)
    if candidate.get("schema") != "captain.factory-candidate.v1":
        raise FactoryV3LiveEvidenceError("candidate manifest is invalid")
    try:
        readback = json.loads(readback_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactoryV3LiveEvidenceError("Minibook readback receipt is invalid") from exc
    assert_factory_v3_live_secret_free(readback)
    expected = {
        (reference.uri, reference.sha256)
        for reference in (
            *package_children,
            result.package_manifest_ref,
            source_archive_ref,
            codex.workspace_snapshot_ref,
            attestation_record.reference,
        )
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
    "FactoryV3LiveRuntimeAttestation",
    "FactoryV3LiveVerifiedEvidence",
    "FactoryV3HttpEvidenceAuthority",
    "assert_factory_v3_live_secret_free",
    "issue_factory_v3_live_runtime_attestation",
    "verify_factory_v3_live_evidence",
]
