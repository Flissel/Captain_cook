from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import httpx

from agenten.agent_factory.forge_contracts import (
    ArtifactRef,
    CodexBuildReceiptV1,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
)
from agenten.agent_factory.hermes_factory_worker import (
    FilesystemHermesFactoryWorkerCheckpointStore,
    AuthenticatedMinibookV3WorkerClient,
    HermesFactoryCodexBuild,
    HermesFactoryWorker,
)
from agenten.agent_factory.codex_build_provenance import (
    CaptainCodexBuildReceiptIssuer,
    CodexBuildArtifactCas,
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


def _build(
    assignment: FactoryBuildAssignmentV2,
    tmp_path: Path,
) -> tuple[HermesFactoryCodexBuild, CaptainCodexBuildReceiptIssuer]:
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
    issuer = CaptainCodexBuildReceiptIssuer(CodexBuildArtifactCas(tmp_path / "cas"))
    receipt_ref = issuer.persist_receipt(receipt)
    return HermesFactoryCodexBuild(
        hermes_usage_receipt_ref=_ref("hermes-usage"),
        codex_build_receipt_ref=ArtifactRef.model_validate(
            receipt_ref.model_dump(mode="json")
        ),
        codex_build_receipt=receipt,
        build_evidence_refs=(_ref("build-evidence"),),
    ), issuer


class _Minibook:
    def __init__(
        self,
        assignment: FactoryBuildAssignmentV2,
        *,
        fail_submissions: int = 0,
    ) -> None:
        self.assignment = assignment
        self.claims = 0
        self.submissions = 0
        self.effect: FactoryBuildEffectV2 | None = None
        self.fail_submissions = fail_submissions

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
        if self.submissions <= self.fail_submissions:
            raise RuntimeError("effect transport failed")
        self.effect = effect
        return effect


class _Codex:
    def __init__(self, result: HermesFactoryCodexBuild | None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def build(self, assignment: FactoryBuildAssignmentV2) -> HermesFactoryCodexBuild:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("second Codex session started")
        if self.error is not None:
            raise self.error
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
async def test_worker_replay_returns_effect_without_second_codex_session(tmp_path: Path) -> None:
    assignment = _assignment()
    minibook = _Minibook(assignment)
    build, issuer = _build(assignment, tmp_path)
    codex = _Codex(build)
    verifier = _SkillVerifier()
    worker = HermesFactoryWorker(
        minibook=minibook,
        codex=codex,
        skill_verifier=verifier,
        worker_id="hermes-factory-v3",
        released_skill_name="captain-agent-factory-loop",
        released_skill_sha256="a" * 64,
        checkpoint_store=FilesystemHermesFactoryWorkerCheckpointStore(tmp_path / "worker"),
        codex_receipts=issuer,
    )

    first = await worker.run_once()
    replay = await worker.run_once()

    assert first == replay
    assert codex.calls == 1
    assert minibook.claims == 2
    assert minibook.submissions == 1
    assert verifier.calls == [("captain-agent-factory-loop", "a" * 64)] * 2


@pytest.mark.asyncio
async def test_pending_effect_replay_resubmits_without_second_codex(
    tmp_path: Path,
) -> None:
    assignment = _assignment()
    minibook = _Minibook(assignment, fail_submissions=1)
    build, issuer = _build(assignment, tmp_path)
    codex = _Codex(build)
    worker = HermesFactoryWorker(
        minibook=minibook,
        codex=codex,
        skill_verifier=_SkillVerifier(),
        worker_id="hermes-factory-v3",
        released_skill_name="captain-agent-factory-loop",
        released_skill_sha256="f" * 64,
        checkpoint_store=FilesystemHermesFactoryWorkerCheckpointStore(
            tmp_path / "worker"
        ),
        codex_receipts=issuer,
    )

    with pytest.raises(RuntimeError, match="effect transport"):
        await worker.run_once()
    accepted = await worker.run_once()

    assert accepted == minibook.effect
    assert codex.calls == 1
    assert minibook.submissions == 2


@pytest.mark.asyncio
async def test_worker_rejects_null_codex_result_without_retry_or_effect(tmp_path: Path) -> None:
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
        checkpoint_store=FilesystemHermesFactoryWorkerCheckpointStore(tmp_path / "worker"),
        codex_receipts=CaptainCodexBuildReceiptIssuer(CodexBuildArtifactCas(tmp_path / "cas")),
    )

    with pytest.raises(ValueError, match="typed Codex build"):
        await worker.run_once()
    with pytest.raises(ValueError, match="retry authority"):
        await worker.run_once()

    assert codex.calls == 1
    assert minibook.submissions == 0


@pytest.mark.asyncio
async def test_worker_rejects_expired_assignment_before_codex(tmp_path: Path) -> None:
    assignment = _assignment()
    minibook = _Minibook(assignment)
    codex = _Codex(None)
    worker = HermesFactoryWorker(
        minibook=minibook,
        codex=codex,
        skill_verifier=_SkillVerifier(),
        worker_id="hermes-factory-v3",
        released_skill_name="captain-agent-factory-loop",
        released_skill_sha256="8" * 64,
        checkpoint_store=FilesystemHermesFactoryWorkerCheckpointStore(
            tmp_path / "worker"
        ),
        codex_receipts=CaptainCodexBuildReceiptIssuer(
            CodexBuildArtifactCas(tmp_path / "cas")
        ),
        clock=lambda: datetime(2026, 8, 9, 12, 0, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="deadline expired"):
        await worker.run_once()

    assert codex.calls == 0
    assert minibook.submissions == 0


@pytest.mark.asyncio
async def test_worker_rejects_receipt_identity_drift(tmp_path: Path) -> None:
    assignment = _assignment()
    build, issuer = _build(assignment, tmp_path)
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
        checkpoint_store=FilesystemHermesFactoryWorkerCheckpointStore(tmp_path / "worker"),
        codex_receipts=issuer,
    )

    with pytest.raises(ValueError, match="receipt.*assignment"):
        await worker.run_once()

    assert codex.calls == 1
    assert minibook.submissions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (TimeoutError(), RuntimeError("process exited without a sealed receipt")),
)
async def test_worker_durable_failure_never_restarts_codex_without_retry_authority(
    tmp_path: Path,
    error: Exception,
) -> None:
    assignment = _assignment()
    minibook = _Minibook(assignment)
    codex = _Codex(None, error=error)
    worker = HermesFactoryWorker(
        minibook=minibook,
        codex=codex,
        skill_verifier=_SkillVerifier(),
        worker_id="hermes-factory-v3",
        released_skill_name="captain-agent-factory-loop",
        released_skill_sha256="d" * 64,
        checkpoint_store=FilesystemHermesFactoryWorkerCheckpointStore(tmp_path / "worker"),
        codex_receipts=CaptainCodexBuildReceiptIssuer(CodexBuildArtifactCas(tmp_path / "cas")),
    )

    with pytest.raises(type(error)):
        await worker.run_once()
    with pytest.raises(ValueError, match="retry authority"):
        await worker.run_once()

    assert codex.calls == 1
    assert minibook.submissions == 0


@pytest.mark.asyncio
async def test_worker_retries_once_only_with_explicit_authority(tmp_path: Path) -> None:
    assignment = _assignment()
    build, issuer = _build(assignment, tmp_path)

    class RecoveringCodex:
        def __init__(self) -> None:
            self.calls = 0

        async def build(self, _assignment):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError()
            return build

    class RetryAuthority:
        def allows_retry(self, assigned, checkpoint) -> bool:
            assert assigned == assignment
            return checkpoint.status == "codex_failed"

    codex = RecoveringCodex()
    minibook = _Minibook(assignment)
    worker = HermesFactoryWorker(
        minibook=minibook,
        codex=codex,
        skill_verifier=_SkillVerifier(),
        worker_id="hermes-factory-v3",
        released_skill_name="captain-agent-factory-loop",
        released_skill_sha256="9" * 64,
        checkpoint_store=FilesystemHermesFactoryWorkerCheckpointStore(
            tmp_path / "worker"
        ),
        codex_receipts=issuer,
        retry_authority=RetryAuthority(),
    )

    with pytest.raises(TimeoutError):
        await worker.run_once()
    effect = await worker.run_once()

    assert effect is not None
    assert codex.calls == 2
    assert minibook.submissions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_state", ("missing", "tampered"))
async def test_worker_missing_or_tampered_receipt_never_submits_effect(
    tmp_path: Path,
    receipt_state: str,
) -> None:
    assignment = _assignment()
    build, issuer = _build(assignment, tmp_path)
    receipt_path = issuer._cas.local_path(build.codex_build_receipt_ref)
    if receipt_state == "missing":
        receipt_path.unlink()
    else:
        receipt_path.write_bytes(b'{"tampered":true}')
    minibook = _Minibook(assignment)
    codex = _Codex(build)
    worker = HermesFactoryWorker(
        minibook=minibook,
        codex=codex,
        skill_verifier=_SkillVerifier(),
        worker_id="hermes-factory-v3",
        released_skill_name="captain-agent-factory-loop",
        released_skill_sha256="e" * 64,
        checkpoint_store=FilesystemHermesFactoryWorkerCheckpointStore(tmp_path / "worker"),
        codex_receipts=issuer,
    )

    with pytest.raises(ValueError, match="receipt|CAS"):
        await worker.run_once()

    assert minibook.submissions == 0


@pytest.mark.asyncio
async def test_authenticated_minibook_client_uses_typed_v3_worker_routes(
    tmp_path: Path,
) -> None:
    assignment = _assignment()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer worker-secret"
        if request.url.path.endswith("/next"):
            return httpx.Response(
                200,
                json=assignment.model_dump(mode="json", by_alias=True),
            )
        if request.url.path.endswith("/claim"):
            assert json.loads(request.content) == {"worker_id": "hermes-factory-v3"}
            return httpx.Response(
                200,
                json=assignment.model_dump(mode="json", by_alias=True),
            )
        return httpx.Response(202, json={"status": "validating"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://minibook.invalid",
    ) as session:
        client = AuthenticatedMinibookV3WorkerClient(
            client=session,
            worker_token="worker-secret",
            worker_id="hermes-factory-v3",
            checkpoint_store=FilesystemHermesFactoryWorkerCheckpointStore(
                tmp_path / "worker"
            ),
        )
        assert await client.next_assignment() == assignment
        assert await client.claim(
            assignment.assignment_id,
            "hermes-factory-v3",
        ) == assignment

    assert [request.url.path for request in requests] == [
        "/v3/creation/assignments/next",
        f"/v3/creation/assignments/{assignment.assignment_id}/claim",
    ]
