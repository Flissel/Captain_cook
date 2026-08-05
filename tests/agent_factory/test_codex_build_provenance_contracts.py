from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from agenten.agent_factory.forge_contracts import (
    CodexBuildReceiptV1,
    CreationJobV1,
    CreationJobV2,
    codex_build_receipt_sha256,
)
from agenten.agent_factory.skill_workflow_contracts import (
    CodexBuildEvidenceV1,
    FactorySkillInvocationV1,
    FactorySkillStep,
)
from tests.agent_factory.test_skill_workflow_contracts import (
    CORRELATION_ID,
    JOB_ID,
    NOW,
    artifact,
    lease_payload,
    released_skill_payload,
)


CREATION_JOB_ID = "00000000-0000-0000-0000-000000000305"
ASSIGNMENT_ID = "00000000-0000-0000-0000-000000000304"
RECEIPT_ID = "00000000-0000-0000-0000-000000000308"
INVOCATION_ID = "00000000-0000-0000-0000-000000000309"


def ref(name: str, digest: str, media_type: str = "application/json") -> dict[str, str]:
    value = artifact(name, digest)
    value["media_type"] = media_type
    return value


def receipt_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "captain.codex-build-receipt.v1",
        "receipt_id": RECEIPT_ID,
        "producer": "captain",
        "outcome": "sealed",
        "factory_job_id": JOB_ID,
        "creation_job_id": CREATION_JOB_ID,
        "correlation_id": CORRELATION_ID,
        "subject_version": 1,
        "attempt": 1,
        "assignment_id": ASSIGNMENT_ID,
        "idempotency_key": "b" * 64,
        "seal_idempotency_key": "7" * 64,
        "build_brief_ref": ref("brief_codex-artifact", "b" * 64),
        "workspace_ref": "workspace://factory/workflow",
        "codex_session_ref": ref("codex-session", "c" * 64),
        "workspace_snapshot_ref": ref(
            "workspace-snapshot", "d" * 64, "application/zip"
        ),
        "candidate_manifest_ref": ref("candidate-manifest", "e" * 64),
        "source_archive_ref": ref("source-archive", "f" * 64, "application/zip"),
        "test_evidence_refs": [ref("test-evidence", "1" * 64)],
        "acceptance_assertion_ids": ["schema_valid", "real_case_green"],
        "completed_at": NOW + timedelta(minutes=2),
    }
    payload.update(overrides)
    return payload


def receipt_ref(receipt: dict[str, object]) -> dict[str, str]:
    digest = codex_build_receipt_sha256(CodexBuildReceiptV1.model_validate(receipt))
    return ref(f"captain-build-receipt/{digest}", digest)


def seal_invocation_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "captain.factory-skill-invocation.v1",
        "invocation_id": INVOCATION_ID,
        "job_id": JOB_ID,
        "correlation_id": CORRELATION_ID,
        "subject_version": 1,
        "attempt": 1,
        "step": "seal_codex_build",
        "released_skill": released_skill_payload("captain-factory-seal-codex-build"),
        "input_ref": ref("brief_codex-artifact", "b" * 64),
        "input_sha256": "b" * 64,
        "lease": lease_payload("tool_integrator", "factory-tool-integrator"),
        "idempotency_key": "7" * 64,
        "acceptance_assertion_ids": ["schema_valid", "real_case_green"],
    }
    payload.update(overrides)
    return payload


def evidence_payload(**overrides: object) -> dict[str, object]:
    receipt = receipt_payload()
    sealed_receipt_ref = receipt_ref(receipt)
    payload: dict[str, object] = {
        "schema": "hermes.factory-codex-build-evidence.v1",
        "invocation": seal_invocation_payload(),
        "invocation_id": INVOCATION_ID,
        "job_id": JOB_ID,
        "correlation_id": CORRELATION_ID,
        "subject_version": 1,
        "attempt": 1,
        "occurred_at": NOW + timedelta(minutes=3),
        "producer": "hermes",
        "artifact_ref": ref("codex-build-evidence", "2" * 64),
        "evidence_refs": [sealed_receipt_ref],
        "acceptance_assertion_ids": ["schema_valid", "real_case_green"],
        "build_receipt_ref": sealed_receipt_ref,
        "build_receipt": receipt,
        "status": "sealed",
    }
    payload.update(overrides)
    return payload


def test_captain_codex_build_receipt_is_strict_frozen_and_digest_bound() -> None:
    receipt = CodexBuildReceiptV1.model_validate(receipt_payload())

    assert receipt.producer == "captain"
    assert receipt.outcome == "sealed"
    assert receipt.candidate_manifest_ref.media_type == "application/json"
    assert receipt.source_archive_ref.media_type == "application/zip"
    assert CodexBuildReceiptV1.model_validate(
        receipt.model_dump(mode="json", by_alias=True)
    ) == receipt
    with pytest.raises(ValidationError):
        CodexBuildReceiptV1.model_validate(receipt_payload(unknown=True))
    with pytest.raises(ValidationError, match="frozen"):
        receipt.outcome = "failed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("completed_at", NOW.replace(tzinfo=None), "UTC"),
        ("candidate_manifest_ref", ref("candidate", "e" * 64, "text/plain"), "manifest"),
        ("source_archive_ref", ref("source", "f" * 64), "source archive"),
        ("workspace_snapshot_ref", ref("snapshot", "d" * 64), "workspace snapshot"),
        (
            "test_evidence_refs",
            [ref("same-test", "1" * 64), ref("same-test", "1" * 64)],
            "test evidence",
        ),
        (
            "acceptance_assertion_ids",
            ["schema_valid", "schema_valid"],
            "assertion",
        ),
    ],
)
def test_captain_codex_build_receipt_rejects_unsealed_or_ambiguous_evidence(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        CodexBuildReceiptV1.model_validate(receipt_payload(**{field: value}))


def test_codex_build_evidence_binds_receipt_brief_and_captain_invocation() -> None:
    evidence = CodexBuildEvidenceV1.model_validate(evidence_payload())

    assert evidence.invocation.step is FactorySkillStep.SEAL_CODEX_BUILD
    assert evidence.build_receipt.factory_job_id == evidence.job_id
    assert evidence.build_receipt.build_brief_ref.sha256 == evidence.invocation.input_ref.sha256
    assert evidence.build_receipt.candidate_manifest_ref.sha256 == "e" * 64


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"factory_job_id": "00000000-0000-0000-0000-000000000399"}, "job"),
        ({"correlation_id": "00000000-0000-0000-0000-000000000399"}, "correlation"),
        ({"subject_version": 2}, "version"),
        ({"attempt": 2}, "attempt"),
        ({"seal_idempotency_key": "9" * 64}, "idempotency"),
        ({"build_brief_ref": ref("different-brief", "9" * 64)}, "brief"),
        ({"workspace_ref": "workspace://factory/different"}, "workspace"),
        ({"acceptance_assertion_ids": ["schema_valid"]}, "assertion"),
    ],
)
def test_codex_build_evidence_rejects_receipt_binding_mismatch(
    mutation: dict[str, object],
    message: str,
) -> None:
    receipt = receipt_payload(**mutation)
    with pytest.raises(ValidationError, match=message):
        CodexBuildEvidenceV1.model_validate(evidence_payload(build_receipt=receipt))


def test_codex_build_evidence_may_reference_only_the_captain_receipt() -> None:
    payload = evidence_payload()
    payload["evidence_refs"] = [
        payload["build_receipt_ref"],
        ref("source-archive", "f" * 64, "application/zip"),
    ]

    with pytest.raises(ValidationError, match="only.*Captain receipt"):
        CodexBuildEvidenceV1.model_validate(payload)


def test_codex_build_evidence_rejects_receipt_content_under_a_foreign_digest() -> None:
    payload = evidence_payload()
    receipt = dict(payload["build_receipt"])  # type: ignore[arg-type]
    receipt["candidate_manifest_ref"] = ref("substituted-candidate", "9" * 64)
    payload["build_receipt"] = receipt

    with pytest.raises(ValidationError, match="receipt digest"):
        CodexBuildEvidenceV1.model_validate(payload)


def test_codex_build_evidence_requires_json_receipt_artifact() -> None:
    payload = evidence_payload()
    receipt_artifact = dict(payload["build_receipt_ref"])  # type: ignore[arg-type]
    receipt_artifact["media_type"] = "text/plain"
    payload["build_receipt_ref"] = receipt_artifact
    payload["evidence_refs"] = [receipt_artifact]

    with pytest.raises(ValidationError, match="receipt.*application/json"):
        CodexBuildEvidenceV1.model_validate(payload)


def test_creation_job_v2_binds_exact_captain_receipt_and_source() -> None:
    legacy = CreationJobV1.model_validate_json(
        Path("tests/fixtures/contracts/minibook_creation_job.v1.json").read_text(
            encoding="utf-8"
        )
    )
    receipt_data = receipt_payload(
        factory_job_id=str(legacy.factory_job_id),
        creation_job_id=str(legacy.creation_job_id),
        correlation_id=str(legacy.correlation_id),
        subject_version=legacy.subject_version,
        attempt=legacy.attempt,
        idempotency_key=legacy.idempotency_key,
        acceptance_assertion_ids=list(legacy.public_assertion_ids),
    )
    receipt = CodexBuildReceiptV1.model_validate(receipt_data)
    sealed_ref = receipt_ref(receipt_data)
    payload = legacy.model_dump(mode="json", by_alias=True) | {
        "schema": "minibook.creation-job.v2",
        "source_archive_ref": receipt.source_archive_ref.model_dump(mode="json"),
        "codex_build_receipt_ref": sealed_ref,
        "codex_build_receipt": receipt.model_dump(mode="json", by_alias=True),
    }

    parsed = CreationJobV2.model_validate(payload)

    assert parsed.codex_build_receipt == receipt
    assert parsed.codex_build_receipt_ref.sha256 == codex_build_receipt_sha256(receipt)
    changed = dict(payload)
    changed["source_archive_ref"] = ref(
        "different-source", "9" * 64, "application/zip"
    )
    with pytest.raises(ValidationError, match="receipt.*creation job"):
        CreationJobV2.model_validate(changed)
