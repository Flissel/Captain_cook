from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agenten.agent_factory.forge_contracts import (
    ArtifactRef,
    CodexBuildReceiptV1,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
    codex_build_receipt_sha256,
)
from agenten.agent_factory.hermes_factory_worker import (
    HermesFactoryCodexBuild,
    HermesFactoryWorker,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "contracts"


def _assignment() -> FactoryBuildAssignmentV2:
    return FactoryBuildAssignmentV2.model_validate(
        json.loads((FIXTURES / "hermes_factory_assignment.v2.json").read_text())
    )


def _ref(namespace: str, *, media_type: str = "application/json") -> ArtifactRef:
    digest = (namespace.encode().hex() + "0" * 64)[:64]
    return ArtifactRef(
        uri=f"artifact://test/{namespace}/{digest}",
        sha256=digest,
        media_type=media_type,
    )


def _build(assignment: FactoryBuildAssignmentV2) -> HermesFactoryCodexBuild:
    receipt = CodexBuildReceiptV1(
        receipt_id=uuid4(),
        producer="captain",
        outcome="sealed",
        factory_job_id=assignment.factory_job_id,
        creation_job_id=assignment.creation_job_id,
        correlation_id=assignment.correlation_id,
        subject_version=assignment.subject_version,
        attempt=assignment.attempt,
        assignment_id=assignment.assignment_id,
        idempotency_key=assignment.effect_key,
        seal_idempotency_key=assignment.effect_key,
        build_brief_ref=_ref("brief"),
        workspace_ref=assignment.workspace_ref,
        codex_session_ref=_ref("session"),
        workspace_snapshot_ref=_ref("snapshot", media_type="application/zip"),
        candidate_manifest_ref=_ref("manifest"),
        source_archive_ref=_ref("source", media_type="application/zip"),
        test_evidence_refs=(_ref("test"),),
        acceptance_assertion_ids=("team-topology",),
        completed_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )
    receipt_ref = ArtifactRef(
        uri=f"artifact://captain-codex-build/build-receipt/{codex_build_receipt_sha256(receipt)}",
        sha256=codex_build_receipt_sha256(receipt),
        media_type="application/json",
    )
    return HermesFactoryCodexBuild(
        hermes_usage_receipt_ref=_ref("hermes-usage"),
        codex_build_receipt_ref=receipt_ref,
        codex_build_receipt=receipt,
        build_evidence_refs=(_ref("build-evidence"),),
    )


class _Minibook:
    def __init__(self, assignment: FactoryBuildAssignmentV2) -> None:
        self.assignment = assignment
        self.claims = 0
        self.submissions = 0
        self.effect: FactoryBuildEffectV2 | None = None

    async def next_assignment(self) -> FactoryBuildAssignmentV2 | None:
        return self.assignment

    async def claim(self, assignment_id: UUID, worker_id: str) -> FactoryBuildAssignmentV2:
        assert assignment_id == self.assignment.assignment_id
        assert worker_id == "hermes-factory-v3"
        self.claims += 1
        return self.assignment

    async def accepted_effect(self, assignment_id: UUID) -> FactoryBuildEffectV2 | None:
        assert assignment_id == self.assignment.assignment_id
        return self.effect

    async def submit_effect(self, effect: FactoryBuildEffectV2) -> FactoryBuildEffectV2:
        self.submissions += 1
        self.effect = effect
        return effect


class _Codex:
    def __init__(self, result: HermesFactoryCodexBuild | None) -> None:
        self.result = result
        self.calls = 0

    async def build(self, assignment: FactoryBuildAssignmentV2) -> HermesFactoryCodexBuild:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("second Codex session started")
        assert assignment.allowed_output_roots == ("output",)
        if self.result is None:
            return None  # type: ignore[return-value]
        return self.result


class _SkillVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def validate_runtime_skill(self, *, skill_name: str, released_skill_sha256: str) -> None:
        self.calls.append((skill_name, released_skill_sha256))


@pytest.mark.asyncio
async def test_worker_replay_returns_effect_without_second_codex_session() -> None:
    assignment = _assignment()
    minibook = _Minibook(assignment)
    codex = _Codex(_build(assignment))
    verifier = _SkillVerifier()
    worker = HermesFactoryWorker(
        minibook=minibook,
        codex=codex,
        skill_verifier=verifier,
        worker_id="hermes-factory-v3",
        released_skill_name="captain-agent-factory-loop",
        released_skill_sha256="a" * 64,
    )

    first = await worker.run_once()
    replay = await worker.run_once()

    assert first == replay
    assert codex.calls == 1
    assert minibook.claims == 2
    assert minibook.submissions == 1
    assert verifier.calls == [("captain-agent-factory-loop", "a" * 64)] * 2


@pytest.mark.asyncio
async def test_worker_rejects_null_codex_result_without_retry_or_effect() -> None:
    assignment = _assignment()
    minibook = _Minibook(assignment)
    codex = _Codex(None)
    worker = HermesFactoryWorker(
        minibook=minibook,
        codex=codex,
        skill_verifier=_SkillVerifier(),
        worker_id="hermes-factory-v3",
        released_skill_name="captain-agent-factory-loop",
        released_skill_sha256="b" * 64,
    )

    with pytest.raises(ValueError, match="typed Codex build"):
        await worker.run_once()

    assert codex.calls == 1
    assert minibook.submissions == 0


@pytest.mark.asyncio
async def test_worker_rejects_receipt_identity_drift() -> None:
    assignment = _assignment()
    build = _build(assignment)
    drifted = build.model_copy(
        update={
            "codex_build_receipt": build.codex_build_receipt.model_copy(
                update={"assignment_id": uuid4()}
            )
        }
    )
    minibook = _Minibook(assignment)
    codex = _Codex(drifted)
    worker = HermesFactoryWorker(
        minibook=minibook,
        codex=codex,
        skill_verifier=_SkillVerifier(),
        worker_id="hermes-factory-v3",
        released_skill_name="captain-agent-factory-loop",
        released_skill_sha256="c" * 64,
    )

    with pytest.raises(ValueError, match="receipt.*assignment"):
        await worker.run_once()

    assert codex.calls == 1
    assert minibook.submissions == 0
