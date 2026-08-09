from __future__ import annotations

import json
import subprocess
import asyncio
import multiprocessing
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
    HermesFactoryAssignmentScope,
    HermesFactoryRetryAuthorization,
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


def _run_retry_worker_process(
    assignment_json: str,
    build_json: str,
    checkpoint_root: str,
    cas_root: str,
    barrier,
    codex_calls,
    outcomes,
) -> None:
    assignment = FactoryBuildAssignmentV2.model_validate_json(assignment_json)
    build = HermesFactoryCodexBuild.model_validate_json(build_json)

    class ProcessCodex:
        async def build(self, _assignment):
            with codex_calls.get_lock():
                codex_calls.value += 1
            await asyncio.sleep(0.2)
            return build

    class RetryAuthority:
        def retry_authorization(self, assigned, checkpoint):
            return HermesFactoryRetryAuthorization(
                assignment_id=assigned.assignment_id,
                attempt=assigned.attempt,
                expected_revision=checkpoint.revision,
                authorization_sha256="4" * 64,
            )

    worker = HermesFactoryWorker(
        minibook=_Minibook(assignment),
        codex=ProcessCodex(),
        skill_verifier=_SkillVerifier(),
        worker_id="hermes-factory-v3",
        released_skill_name="captain-agent-factory-loop",
        released_skill_sha256="a" * 64,
        checkpoint_store=FilesystemHermesFactoryWorkerCheckpointStore(
            Path(checkpoint_root)
        ),
        codex_receipts=CaptainCodexBuildReceiptIssuer(
            CodexBuildArtifactCas(Path(cas_root))
        ),
        retry_authority=RetryAuthority(),
    )
    barrier.wait()
    try:
        asyncio.run(worker.run_once())
    except ValueError:
        outcomes.put("fenced")
    else:
        outcomes.put("effect")


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
async def test_concurrent_workers_start_exactly_one_codex_session(tmp_path: Path) -> None:
    assignment = _assignment()
    build, issuer = _build(assignment, tmp_path)
    calls = 0

    class SlowCodex:
        async def build(self, _assignment):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return build

    workers = tuple(
        HermesFactoryWorker(
            minibook=_Minibook(assignment),
            codex=SlowCodex(),
            skill_verifier=_SkillVerifier(),
            worker_id="hermes-factory-v3",
            released_skill_name="captain-agent-factory-loop",
            released_skill_sha256="a" * 64,
            checkpoint_store=FilesystemHermesFactoryWorkerCheckpointStore(
                tmp_path / "worker"
            ),
            codex_receipts=issuer,
        )
        for _ in range(2)
    )

    results = await asyncio.gather(
        *(worker.run_once() for worker in workers),
        return_exceptions=True,
    )

    assert calls == 1
    assert sum(isinstance(item, FactoryBuildEffectV2) for item in results) == 1
    assert sum(isinstance(item, ValueError) for item in results) == 1


@pytest.mark.asyncio
async def test_concurrent_authorized_retries_start_one_codex_session(
    tmp_path: Path,
) -> None:
    assignment = _assignment()
    build, issuer = _build(assignment, tmp_path)
    root = tmp_path / "worker"
    store = FilesystemHermesFactoryWorkerCheckpointStore(root)
    failing = HermesFactoryWorker(
        minibook=_Minibook(assignment),
        codex=_Codex(None, error=TimeoutError()),
        skill_verifier=_SkillVerifier(),
        worker_id="hermes-factory-v3",
        released_skill_name="captain-agent-factory-loop",
        released_skill_sha256="a" * 64,
        checkpoint_store=store,
        codex_receipts=issuer,
    )
    with pytest.raises(TimeoutError):
        await failing.run_once()
    calls = 0

    class RecoveringCodex:
        async def build(self, _assignment):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return build

    class RetryAuthority:
        def retry_authorization(self, assigned, checkpoint):
            return HermesFactoryRetryAuthorization(
                assignment_id=assigned.assignment_id,
                attempt=assigned.attempt,
                expected_revision=checkpoint.revision,
                authorization_sha256="6" * 64,
            )

    workers = tuple(
        HermesFactoryWorker(
            minibook=_Minibook(assignment),
            codex=RecoveringCodex(),
            skill_verifier=_SkillVerifier(),
            worker_id="hermes-factory-v3",
            released_skill_name="captain-agent-factory-loop",
            released_skill_sha256="a" * 64,
            checkpoint_store=FilesystemHermesFactoryWorkerCheckpointStore(root),
            codex_receipts=issuer,
            retry_authority=RetryAuthority(),
        )
        for _ in range(2)
    )

    results = await asyncio.gather(
        *(worker.run_once() for worker in workers),
        return_exceptions=True,
    )

    assert calls == 1
    assert sum(isinstance(item, FactoryBuildEffectV2) for item in results) == 1
    assert sum(isinstance(item, ValueError) for item in results) == 1


def test_two_processes_share_one_revision_bound_retry_execution_lease(
    tmp_path: Path,
) -> None:
    assignment = _assignment()
    build, _issuer = _build(assignment, tmp_path)
    checkpoint_root = tmp_path / "worker"
    store = FilesystemHermesFactoryWorkerCheckpointStore(checkpoint_root)
    started = store.start(
        assignment,
        worker_id="hermes-factory-v3",
        retry_authorization=None,
    )
    store.failed(
        assignment,
        started,
        failure_kind="timeout",
    )
    context = multiprocessing.get_context("spawn")
    barrier = context.Event()
    codex_calls = context.Value("i", 0)
    outcomes = context.Queue()
    arguments = (
        assignment.model_dump_json(by_alias=True),
        build.model_dump_json(by_alias=True),
        str(checkpoint_root),
        str(tmp_path / "cas"),
        barrier,
        codex_calls,
        outcomes,
    )
    processes = tuple(
        context.Process(target=_run_retry_worker_process, args=arguments)
        for _ in range(2)
    )
    for process in processes:
        process.start()
    barrier.set()
    for process in processes:
        process.join(timeout=10)

    assert all(not process.is_alive() for process in processes)
    assert all(process.exitcode == 0 for process in processes)
    assert codex_calls.value == 1
    assert sorted(outcomes.get(timeout=1) for _ in processes) == [
        "effect",
        "fenced",
    ]


@pytest.mark.asyncio
async def test_crashed_started_checkpoint_requires_revision_bound_retry(
    tmp_path: Path,
) -> None:
    assignment = _assignment()
    build, issuer = _build(assignment, tmp_path)
    root = tmp_path / "worker"
    store = FilesystemHermesFactoryWorkerCheckpointStore(root)
    started = store.start(
        assignment,
        worker_id="hermes-factory-v3",
        retry_authorization=None,
    )
    assert started.state == "started"

    without_authority = HermesFactoryWorker(
        minibook=_Minibook(assignment),
        codex=_Codex(build),
        skill_verifier=_SkillVerifier(),
        worker_id="hermes-factory-v3",
        released_skill_name="captain-agent-factory-loop",
        released_skill_sha256="a" * 64,
        checkpoint_store=store,
        codex_receipts=issuer,
    )
    with pytest.raises(ValueError, match="retry authority"):
        await without_authority.run_once()

    class RetryAuthority:
        def retry_authorization(self, assigned, checkpoint):
            assert checkpoint.revision == started.revision
            return HermesFactoryRetryAuthorization(
                assignment_id=assigned.assignment_id,
                attempt=assigned.attempt,
                expected_revision=checkpoint.revision,
                authorization_sha256="5" * 64,
            )

    codex = _Codex(build)
    effect = await HermesFactoryWorker(
        minibook=_Minibook(assignment),
        codex=codex,
        skill_verifier=_SkillVerifier(),
        worker_id="hermes-factory-v3",
        released_skill_name="captain-agent-factory-loop",
        released_skill_sha256="a" * 64,
        checkpoint_store=store,
        codex_receipts=issuer,
        retry_authority=RetryAuthority(),
    ).run_once()

    assert effect is not None
    assert codex.calls == 1
    final = store.load(assignment)
    assert final is not None
    assert final.consumed_retry_authorizations == ("5" * 64,)


@pytest.mark.asyncio
async def test_consumed_retry_authorization_cannot_be_reused(
    tmp_path: Path,
) -> None:
    assignment = _assignment()
    _build_result, issuer = _build(assignment, tmp_path)
    root = tmp_path / "worker"
    store = FilesystemHermesFactoryWorkerCheckpointStore(root)
    first = store.start(
        assignment,
        worker_id="hermes-factory-v3",
        retry_authorization=None,
    )
    failed = store.failed(assignment, first, failure_kind="timeout")
    authorization = HermesFactoryRetryAuthorization(
        assignment_id=assignment.assignment_id,
        attempt=assignment.attempt,
        expected_revision=failed.revision,
        authorization_sha256="3" * 64,
    )
    retried = store.start(
        assignment,
        worker_id="hermes-factory-v3",
        retry_authorization=authorization,
    )
    failed_again = store.failed(assignment, retried, failure_kind="timeout")
    reused = authorization.model_copy(
        update={"expected_revision": failed_again.revision}
    )

    with pytest.raises(ValueError, match="binding changed"):
        store.start(
            assignment,
            worker_id="hermes-factory-v3",
            retry_authorization=reused,
        )

    assert store.load(assignment) == failed_again


@pytest.mark.asyncio
async def test_stale_checkpoint_temp_does_not_block_restart(tmp_path: Path) -> None:
    assignment = _assignment()
    build, issuer = _build(assignment, tmp_path)
    root = tmp_path / "worker"
    root.mkdir()
    (root / f"{assignment.assignment_id.hex}.json.crashed.tmp").write_text(
        "partial secret payload"
    )
    codex = _Codex(build)

    effect = await HermesFactoryWorker(
        minibook=_Minibook(assignment),
        codex=codex,
        skill_verifier=_SkillVerifier(),
        worker_id="hermes-factory-v3",
        released_skill_name="captain-agent-factory-loop",
        released_skill_sha256="a" * 64,
        checkpoint_store=FilesystemHermesFactoryWorkerCheckpointStore(root),
        codex_receipts=issuer,
    ).run_once()

    assert effect is not None
    assert codex.calls == 1


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
    checkpoint = json.loads(next((tmp_path / "worker").glob("*.json")).read_text())
    assert checkpoint["worker_id"] == "hermes-factory-v3"
    assert checkpoint["failure_kind"] == "null_or_untyped"
    assert checkpoint["failure_exit_code"] is None


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
    ("error", "failure_kind", "exit_code"),
    (
        (TimeoutError("secret C:\\private\\prompt.txt"), "timeout", None),
        (subprocess.CalledProcessError(23, ["secret-command"]), "process_exit", 23),
        (RuntimeError("provider response with credential"), "runtime_error", None),
    ),
)
async def test_worker_durable_failure_never_restarts_codex_without_retry_authority(
    tmp_path: Path,
    error: Exception,
    failure_kind: str,
    exit_code: int | None,
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
    content = next((tmp_path / "worker").glob("*.json")).read_text()
    checkpoint = json.loads(content)
    assert checkpoint["worker_id"] == "hermes-factory-v3"
    assert checkpoint["failure_kind"] == failure_kind
    assert checkpoint["failure_exit_code"] == exit_code
    assert "secret" not in content
    assert "private" not in content
    assert "credential" not in content
    assert "provider response" not in content


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
        def retry_authorization(self, assigned, checkpoint):
            assert assigned == assignment
            if checkpoint.status != "codex_failed":
                return None
            return HermesFactoryRetryAuthorization(
                assignment_id=assigned.assignment_id,
                attempt=assigned.attempt,
                expected_revision=checkpoint.revision,
                authorization_sha256="7" * 64,
            )

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


@pytest.mark.asyncio
async def test_scoped_worker_ignores_unrelated_assignment_without_claim(
    tmp_path: Path,
) -> None:
    assignment = _assignment()
    unrelated_scope = HermesFactoryAssignmentScope(
        factory_job_id=uuid4(),
        correlation_id=uuid4(),
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=assignment.model_dump(mode="json", by_alias=True),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://minibook.invalid",
    ) as session:
        minibook = AuthenticatedMinibookV3WorkerClient(
            client=session,
            worker_token="worker-secret",
            worker_id="hermes-factory-v3",
            checkpoint_store=FilesystemHermesFactoryWorkerCheckpointStore(
                tmp_path / "worker"
            ),
        )
        worker = HermesFactoryWorker(
            minibook=minibook,
            codex=_Codex(None),
            skill_verifier=_SkillVerifier(),
            worker_id="hermes-factory-v3",
            released_skill_name="captain-agent-factory-loop",
            released_skill_sha256="a" * 64,
            checkpoint_store=FilesystemHermesFactoryWorkerCheckpointStore(
                tmp_path / "worker"
            ),
            codex_receipts=CaptainCodexBuildReceiptIssuer(
                CodexBuildArtifactCas(tmp_path / "cas")
            ),
        )

        assert await worker.run_once(unrelated_scope) is None

    assert len(requests) == 1
    assert requests[0].url.params["factory_job_id"] == str(
        unrelated_scope.factory_job_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("response_mode", ("empty", "malformed", "conflicting"))
async def test_typed_effect_response_failure_leaves_checkpoint_pending(
    tmp_path: Path,
    response_mode: str,
) -> None:
    assignment = _assignment()
    build, issuer = _build(assignment, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/next") or request.url.path.endswith("/claim"):
            return httpx.Response(
                200,
                json=assignment.model_dump(mode="json", by_alias=True),
            )
        if response_mode == "empty":
            return httpx.Response(202, content=b"")
        if response_mode == "malformed":
            return httpx.Response(202, content=b"not-json")
        payload = json.loads(request.content)
        payload["hermes_usage_receipt_ref"] = _ref("conflict").model_dump(
            mode="json"
        )
        return httpx.Response(200, json=payload)

    store = FilesystemHermesFactoryWorkerCheckpointStore(tmp_path / "worker")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://minibook.invalid",
    ) as session:
        minibook = AuthenticatedMinibookV3WorkerClient(
            client=session,
            worker_token="worker-secret",
            worker_id="hermes-factory-v3",
            checkpoint_store=store,
        )
        worker = HermesFactoryWorker(
            minibook=minibook,
            codex=_Codex(build),
            skill_verifier=_SkillVerifier(),
            worker_id="hermes-factory-v3",
            released_skill_name="captain-agent-factory-loop",
            released_skill_sha256="a" * 64,
            checkpoint_store=store,
            codex_receipts=issuer,
        )

        with pytest.raises(ValueError, match="effect response|different build effect"):
            await worker.run_once()

    checkpoint = store.load(assignment)
    assert checkpoint is not None
    assert checkpoint.state == "pending_effect"
