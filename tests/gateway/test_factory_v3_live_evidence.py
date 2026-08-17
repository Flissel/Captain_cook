from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from agenten.agent_factory.forge_contracts import CreationResultV2
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.factory_v3_live_evidence import (
    FactoryV3AuthorityRecord,
    FactoryV3LiveEvidenceError,
    assert_factory_v3_live_secret_free,
    verify_factory_v3_live_evidence,
)
from minibook.swarm.team_package import seal_team_package, validate_team_package
from minibook.swarm.contracts import ArtifactRef as MinibookArtifactRef
from minibook.tests.test_team_package import _fixture


RUN_ID = UUID("a1000000-0000-0000-0000-000000000001")
PREFLIGHT_SHA256 = "9" * 64


def _json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8") + b"\n"


@dataclass
class _Authority:
    store: object
    hermes: tuple[FactoryV3AuthorityRecord, ...]
    codex: tuple[FactoryV3AuthorityRecord, ...]
    result: tuple[FactoryV3AuthorityRecord, ...]
    acceptance: tuple[FactoryV3AuthorityRecord, ...]
    tamper_captain_sha256: str | None = None

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def hermes_receipts(self, correlation_id: UUID) -> tuple[FactoryV3AuthorityRecord, ...]:
        self.calls.append(("hermes", str(correlation_id)))
        return self.hermes

    def codex_receipts(self, correlation_id: UUID) -> tuple[FactoryV3AuthorityRecord, ...]:
        self.calls.append(("codex", str(correlation_id)))
        return self.codex

    def minibook_results(self, creation_job_id: UUID) -> tuple[FactoryV3AuthorityRecord, ...]:
        self.calls.append(("minibook-result", str(creation_job_id)))
        return self.result

    def captain_acceptances(self, factory_job_id: UUID) -> tuple[FactoryV3AuthorityRecord, ...]:
        self.calls.append(("captain-acceptance", str(factory_job_id)))
        return self.acceptance

    def prohibited_lifecycle_events(self, correlation_id: UUID) -> tuple[FactoryV3AuthorityRecord, ...]:
        self.calls.append(("prohibited-events", str(correlation_id)))
        return ()

    def minibook_cas(self, reference: ArtifactRef) -> bytes:
        self.calls.append(("minibook-cas", reference.sha256))
        converted = MinibookArtifactRef.model_validate(reference.model_dump(mode="json"))
        return self.store.read_bytes(converted)  # type: ignore[attr-defined]

    def captain_cas(self, reference: ArtifactRef) -> bytes:
        self.calls.append(("captain-cas", reference.sha256))
        converted = MinibookArtifactRef.model_validate(reference.model_dump(mode="json"))
        content = self.store.read_bytes(converted)  # type: ignore[attr-defined]
        if self.tamper_captain_sha256 == reference.sha256:
            return content + b"tampered"
        return content


def _record(store: object, ref: ArtifactRef) -> FactoryV3AuthorityRecord:
    return FactoryV3AuthorityRecord(
        reference=ref,
        content=store.read_bytes(ref),  # type: ignore[attr-defined]
    )


def _live_fixture(tmp_path: Path) -> tuple[dict[str, object], _Authority, CreationResultV2]:
    store, _, _, refs, gaps = _fixture(tmp_path)
    validated = validate_team_package(artifact_store=store, tool_gaps=gaps, **refs)
    sealed = seal_team_package(validated, artifact_store=store)
    job = refs["authority_job"]
    result = CreationResultV2.model_validate(
        {
            "creation_job_id": str(job.creation_job_id),
            "factory_job_id": str(job.factory_job_id),
            "correlation_id": str(job.correlation_id),
            "subject_version": job.subject_version,
            "attempt": job.attempt,
            "status": "succeeded",
            "team_manifest_ref": sealed.team_manifest_ref.model_dump(mode="json"),
            "candidate_manifest_ref": sealed.candidate_manifest_ref.model_dump(mode="json"),
            "source_archive_ref": sealed.source_archive_ref.model_dump(mode="json"),
            "package_manifest_ref": sealed.package_manifest_ref.model_dump(mode="json"),
            "hermes_usage_receipt_ref": sealed.hermes_usage_receipt_ref.model_dump(mode="json"),
            "codex_build_receipt_ref": sealed.codex_build_receipt_ref.model_dump(mode="json"),
            "build_evidence_refs": [ref.model_dump(mode="json") for ref in sealed.build_evidence_refs],
            "real_case_evidence_refs": [ref.model_dump(mode="json") for ref in sealed.real_case_evidence_refs],
            "cas_readback_receipt_ref": sealed.readback_receipt_ref.model_dump(mode="json"),
            "tool_gaps": [gap.model_dump(mode="json", by_alias=True) for gap in gaps],
        }
    )
    result_ref = store.put(
        _json(result.model_dump(mode="json", by_alias=True)),
        "application/json",
        namespace="live-result",
    )
    acceptance_payload = {
        "schema": "captain.factory-v3-live-acceptance.v1",
        "producer": "captain",
        "outcome": "accepted",
        "preflight_run_id": str(RUN_ID),
        "preflight_report_sha256": PREFLIGHT_SHA256,
        "correlation_id": str(job.correlation_id),
        "factory_job_id": str(job.factory_job_id),
        "creation_job_id": str(job.creation_job_id),
        "subject_version": job.subject_version,
        "minibook_result_ref": result_ref.model_dump(mode="json"),
        "team_manifest_ref": sealed.team_manifest_ref.model_dump(mode="json"),
        "source_archive_ref": sealed.source_archive_ref.model_dump(mode="json"),
        "cas_readback_receipt_ref": sealed.readback_receipt_ref.model_dump(mode="json"),
        "accepted_at": "2026-08-17T12:00:00Z",
    }
    acceptance_ref = store.put(
        _json(acceptance_payload),
        "application/json",
        namespace="live-acceptance",
    )
    authority = _Authority(
        store=store,
        hermes=(_record(store, sealed.hermes_usage_receipt_ref),),
        codex=(_record(store, sealed.codex_build_receipt_ref),),
        result=(_record(store, result_ref),),
        acceptance=(_record(store, acceptance_ref),),
    )
    index = {
        "schema": "captain.factory-v3-live-evidence-index.v1",
        "preflight_run_id": str(RUN_ID),
        "correlation_id": str(job.correlation_id),
        "factory_job_id": str(job.factory_job_id),
        "creation_job_id": str(job.creation_job_id),
        "preflight_report_sha256": PREFLIGHT_SHA256,
    }
    return index, authority, result


def test_live_evidence_reads_each_typed_authority_and_both_cas_owners(tmp_path: Path) -> None:
    index, authority, result = _live_fixture(tmp_path)

    verified = verify_factory_v3_live_evidence(index, authority=authority)

    assert verified.preflight_run_id == RUN_ID
    assert verified.correlation_id == UUID(str(index["correlation_id"]))
    assert verified.source_archive_sha256 == result.source_archive_ref.sha256  # type: ignore[union-attr]
    assert [name for name, _ in authority.calls[:4]] == [
        "hermes",
        "codex",
        "minibook-result",
        "captain-acceptance",
    ]
    assert sum(name == "minibook-cas" for name, _ in authority.calls) == 6
    assert sum(name == "captain-cas" for name, _ in authority.calls) == 6
    assert verified.as_dict()["hermes_invocation_count"] == 1
    assert verified.as_dict()["codex_session_count"] == 1


def test_receipt_only_or_self_authored_scalar_evidence_fails_closed(tmp_path: Path) -> None:
    index, authority, _ = _live_fixture(tmp_path)
    self_authored = dict(index)
    self_authored["captain_acceptance_status"] = "accepted"
    with pytest.raises(FactoryV3LiveEvidenceError, match="index"):
        verify_factory_v3_live_evidence(self_authored, authority=authority)

    authority.acceptance = ()
    with pytest.raises(FactoryV3LiveEvidenceError, match="Captain.*exactly one"):
        verify_factory_v3_live_evidence(index, authority=authority)


def test_duplicate_hermes_receipt_and_tampered_captain_cas_fail_closed(tmp_path: Path) -> None:
    index, authority, result = _live_fixture(tmp_path)
    authority.hermes = authority.hermes * 2
    with pytest.raises(FactoryV3LiveEvidenceError, match="Hermes.*exactly one"):
        verify_factory_v3_live_evidence(index, authority=authority)

    authority.hermes = authority.hermes[:1]
    assert result.team_manifest_ref is not None
    authority.tamper_captain_sha256 = result.team_manifest_ref.sha256
    with pytest.raises(FactoryV3LiveEvidenceError, match="CAS readback differs"):
        verify_factory_v3_live_evidence(index, authority=authority)


@pytest.mark.parametrize(
    "payload",
    [
        {"outer": [{"token": "redacted"}]},
        {"outer": [{"accessToken": "redacted"}]},
        {"outer": [{"secretValue": "redacted"}]},
        {"outer": {"api_key": "redacted"}},
        {"outer": {"credential": "credential://tenant/value"}},
        {"outer": {"password": "redacted"}},
        {"outer": {"safe": "Bearer abcdef"}},
        {"outer": {"safe": "Y7pL2vQ9xN4mR8sT6uW3aB5cD1eF0gHk"}},
        {"outer": {"safe": "secret-canary-value"}},
        {"outer": {"safe": "Y7pL2vQ9xN4mR8sT6uW3aB5cD1eF0gHk" * 5}},
    ],
)
def test_recursive_secret_key_and_value_scanner_fails_closed(payload: object) -> None:
    with pytest.raises(FactoryV3LiveEvidenceError, match="secret"):
        assert_factory_v3_live_secret_free(payload)


def test_secret_scanner_allows_ids_digests_and_display_safe_status() -> None:
    assert_factory_v3_live_secret_free(
        {
            "run_id": str(RUN_ID),
            "artifact_sha256": hashlib.sha256(b"artifact").hexdigest(),
            "status": "verified",
            "nested": [{"agent_id": "account_researcher"}],
        }
    )
