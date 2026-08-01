from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkContentAddressedArtifactStore,
)
from agenten.agent_factory.codex_build_provenance import CodexBuildArtifactCas
from agenten.agent_factory.contracts import (
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.minibook_forge import CaptainCreationJobMapper
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_factory.skill_workflow_contracts import CandidateRevisionV1
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.agent_factory_live_operator import (
    FactoryLiveOperatorSettings,
    _FactoryInputMaterializer,
    _LazyProductionBenchmarkInputs,
    validate_factory_total_cost_envelope,
    run_business_demo_factory_jobs,
)
from gateway.factory_forge_evidence import CaptainForgeEvidenceBridge
from tests.agent_factory.test_minibook_forge import _workflow_evidence
from tests.agent_factory.test_skill_workflow_contracts import revision_payload


LOCAL_DSN = "mariadb://captain_test:redacted@127.0.0.1:3306/captain_test"
JOB_IDS = (
    UUID("71000000-0000-0000-0000-000000000001"),
    UUID("71000000-0000-0000-0000-000000000002"),
)


def test_factory_input_materializer_routes_and_verifies_each_owned_cas(
    tmp_path: Path,
) -> None:
    benchmark = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "benchmark"
    )
    codex = CodexBuildArtifactCas(tmp_path / ".captain-cook" / "codex")
    benchmark_ref = benchmark.put(
        b"benchmark-input",
        "application/json",
        namespace="job-input",
    )
    codex_ref = codex.put_bytes(
        b"sealed-source",
        media_type="application/zip",
        namespace="source-archive",
    )
    materializer = _FactoryInputMaterializer(
        benchmark_artifacts=benchmark,
        codex_build_artifacts=codex,
    )

    assert materializer.materialize(benchmark_ref).read_bytes() == b"benchmark-input"
    assert materializer.materialize(codex_ref).read_bytes() == b"sealed-source"

    codex.local_path(codex_ref).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="digest"):
        materializer.materialize(codex_ref)

    unknown_ref = ArtifactRef(
        uri=f"artifact://foreign/source/{'f' * 64}",
        sha256="f" * 64,
        media_type="application/zip",
    )
    with pytest.raises(FactoryDispatchError, match="owner"):
        materializer.materialize(unknown_ref)


class _ForgeRepository:
    def __init__(self, blocks, released_skill) -> None:
        self._blocks = tuple(blocks)
        self._released_skill = released_skill

    def blocks(self, _job_id: UUID):
        return self._blocks

    def workflow_artifacts(self, _job_id: UUID):
        return ()

    def released_for(self, _job, _step):
        return self._released_skill


def _forge_block(job, phase: FactoryPhase, refs: tuple[ArtifactRef, ...]):
    role = (
        FactoryRole.AGENT_ARCHITECT
        if phase is FactoryPhase.BLUEPRINT_CREATED
        else FactoryRole.TOOL_INTEGRATOR
    )
    return FactoryEvidenceBlock(
        schema="captain.agent-factory-block.v1",
        event_id=uuid5(NAMESPACE_URL, f"forge-bridge:{job.job_id}:{phase.value}"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        causation_id=job.event_id,
        occurred_at=job.occurred_at,
        producer="hermes",
        subject_version=job.subject_version,
        attempt=1,
        phase=phase,
        role=role,
        status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=refs[:1],
        evidence_refs=refs,
        assertion_ids=job.acceptance_assertion_ids,
        lease_id=f"lease-{phase.value}",
    )


async def _persist_forge_evidence(root: Path):
    job, inventory, brief, build = _workflow_evidence()
    store = FilesystemFactoryEvidenceStore(root)
    refs = tuple(
        [
            await store.persist(
                job,
                artifact.model_dump_json(by_alias=True).encode("utf-8"),
            )
            for artifact in (inventory, brief, build)
        ]
    )
    blocks = (
        _forge_block(job, FactoryPhase.BLUEPRINT_CREATED, (refs[0], refs[0])),
        _forge_block(job, FactoryPhase.TOOL_CANDIDATE_TESTED, refs[1:]),
    )
    repository = _ForgeRepository(blocks, brief.invocation.released_skill)
    return job, (inventory, brief, build), refs, repository


def _forge_request(job) -> FactoryDispatch:
    return FactoryDispatch(
        job=job,
        action=FactoryAction(kind=FactoryActionKind.SUBMIT_FORGE_JOB, attempt=1),
        role=None,
        lease=None,
    )


def _write_hermes_runtime(root: Path, *, include_logging: bool = True) -> None:
    module_root = root / "hermes-agent"
    package = module_root / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    if include_logging:
        (module_root / "concurrent_log_handler.py").write_text("", encoding="utf-8")


def test_operator_settings_enforce_isolated_database_two_jobs_and_cost_allocation(
    tmp_path: Path,
) -> None:
    _write_hermes_runtime(tmp_path)
    hermes_python = Path(sys.executable)
    settings = FactoryLiveOperatorSettings(
        workspace_root=tmp_path,
        python_executable=Path(sys.executable),
        hermes_python_executable=hermes_python,
        test_mariadb_dsn=LOCAL_DSN,
        job_ids=JOB_IDS,
        hermes_provider="openai-api",
        hermes_model="gpt-4.1-mini",
        hermes_maximum_total_cost_usd=Decimal("0.25"),
        stop_before_quality_warden=True,
    )

    assert settings.job_ids == JOB_IDS
    assert settings.hermes_python_executable == hermes_python
    assert settings.stop_before_quality_warden is True
    with pytest.raises(ValueError, match="cost allocation"):
        FactoryLiveOperatorSettings(
            workspace_root=tmp_path,
            python_executable=Path(sys.executable),
            hermes_python_executable=hermes_python,
            test_mariadb_dsn=LOCAL_DSN,
            job_ids=JOB_IDS,
            hermes_provider="openai-api",
            hermes_model="gpt-4.1-mini",
            hermes_maximum_total_cost_usd=Decimal("0.26"),
        )
    with pytest.raises(ValueError, match="distinct jobs"):
        FactoryLiveOperatorSettings(
            workspace_root=tmp_path,
            python_executable=Path(sys.executable),
            hermes_python_executable=hermes_python,
            test_mariadb_dsn=LOCAL_DSN,
            job_ids=(JOB_IDS[0], JOB_IDS[0]),
            hermes_provider="openai-api",
            hermes_model="gpt-4.1-mini",
            hermes_maximum_total_cost_usd=Decimal("0.10"),
        )


def test_total_cost_envelope_requires_subscription_codex_and_keeps_each_team_below_cap() -> None:
    environment = {
        "CAPTAIN_CODEX_AUTH_MODE": "chatgpt_subscription",
        "CAPTAIN_FACTORY_USER_MAX_EUR_PER_TEAM": "1.00",
        "CAPTAIN_FACTORY_BUDGET_EUR_PER_USD": "1.25",
        "CAPTAIN_FACTORY_MAX_TOTAL_COST_USD_PER_TEAM": "0.80",
        "CAPTAIN_FACTORY_CODEX_METERED_USD_PER_TEAM": "0",
        "CAPTAIN_FACTORY_HERMES_MAX_TOTAL_USD": "0.20",
        "CAPTAIN_FACTORY_PRIOR_ATTEMPT_RESERVE_USD_PER_TEAM": "0.20",
    }

    validate_factory_total_cost_envelope(
        environment=environment,
        benchmark_maximum_usd_per_team=(Decimal("0.40"), Decimal("0.40")),
    )

    with pytest.raises(ValueError, match="ChatGPT subscription"):
        validate_factory_total_cost_envelope(
            environment={**environment, "CAPTAIN_CODEX_AUTH_MODE": "api_key"},
            benchmark_maximum_usd_per_team=(Decimal("0.40"), Decimal("0.40")),
        )
    with pytest.raises(ValueError, match="total cost envelope"):
        validate_factory_total_cost_envelope(
            environment={
                **environment,
                "CAPTAIN_FACTORY_MAX_TOTAL_COST_USD_PER_TEAM": "0.74",
            },
            benchmark_maximum_usd_per_team=(Decimal("0.40"), Decimal("0.40")),
        )
    with pytest.raises(ValueError, match="total cost envelope"):
        validate_factory_total_cost_envelope(
            environment={
                **environment,
                "CAPTAIN_FACTORY_BUDGET_EUR_PER_USD": "1.34",
            },
            benchmark_maximum_usd_per_team=(Decimal("0.40"), Decimal("0.40")),
        )


@pytest.mark.asyncio
async def test_operator_threads_quality_warden_stop_to_both_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[UUID, int, FactoryActionKind | None]] = []

    class FakeAsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Composition:
        async def run(
            self,
            job_id: UUID,
            *,
            maximum_dispatches: int,
            stop_before_action: FactoryActionKind | None,
        ) -> object:
            calls.append((job_id, maximum_dispatches, stop_before_action))
            return SimpleNamespace(job_id=job_id)

    monkeypatch.setattr(
        "gateway.agent_factory_live_operator.httpx.AsyncClient",
        FakeAsyncClient,
    )
    monkeypatch.setattr(
        "gateway.agent_factory_live_operator.compose_business_demo_factory_operator",
        lambda *_args, **_kwargs: Composition(),
    )
    settings = SimpleNamespace(
        job_ids=JOB_IDS,
        maximum_dispatches=9,
        stop_before_quality_warden=True,
    )

    results = await run_business_demo_factory_jobs(
        settings,  # type: ignore[arg-type]
        environment={},
    )

    assert tuple(result.job_id for result in results) == JOB_IDS
    assert calls == [
        (JOB_IDS[0], 9, FactoryActionKind.DISPATCH_QUALITY_WARDEN),
        (JOB_IDS[1], 9, FactoryActionKind.DISPATCH_QUALITY_WARDEN),
    ]


def test_operator_settings_reject_incomplete_hermes_runtime(tmp_path: Path) -> None:
    _write_hermes_runtime(tmp_path, include_logging=False)

    with pytest.raises(ValueError, match="Hermes Python runtime is incomplete"):
        FactoryLiveOperatorSettings(
            workspace_root=tmp_path,
            python_executable=Path(sys.executable),
            hermes_python_executable=Path(sys.executable),
            test_mariadb_dsn=LOCAL_DSN,
            job_ids=JOB_IDS,
            hermes_provider="openai-api",
            hermes_model="gpt-4.1-mini",
            hermes_maximum_total_cost_usd=Decimal("0.10"),
        )


def test_lazy_benchmark_inputs_build_one_composition_per_job() -> None:
    calls: list[object] = []
    expected = object()

    class Composition:
        def dispatch_inputs(self, settings: object, request: object) -> object:
            calls.append((settings, request))
            return expected

    class Loader:
        def __call__(self, settings: object) -> Composition:
            calls.append(settings)
            return Composition()

    selected = SimpleNamespace(profile="claims")
    inputs = _LazyProductionBenchmarkInputs(
        settings={JOB_IDS[0]: selected},  # type: ignore[arg-type]
        loader=Loader(),  # type: ignore[arg-type]
    )
    request = SimpleNamespace(job=SimpleNamespace(job_id=JOB_IDS[0]))

    assert inputs.resolve(request) is expected  # type: ignore[arg-type]
    assert inputs.resolve(request) is expected  # type: ignore[arg-type]
    assert calls.count(selected) == 1
    assert inputs.resolve(
        SimpleNamespace(job=SimpleNamespace(job_id=JOB_IDS[1]))  # type: ignore[arg-type]
    ) is None


@pytest.mark.asyncio
async def test_forge_evidence_bridge_maps_block_referenced_captain_artifacts_when_gateway_artifacts_are_empty(
    tmp_path: Path,
) -> None:
    job, artifacts, _, repository = await _persist_forge_evidence(tmp_path / "evidence")
    assert repository.workflow_artifacts(job.job_id) == ()
    bridge = CaptainForgeEvidenceBridge(
        repository=repository,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
    )

    assert bridge.workflow_artifacts(job.job_id) == artifacts
    creation_job = CaptainCreationJobMapper(evidence=bridge).map(_forge_request(job))

    assert creation_job.factory_job_id == job.job_id
    assert creation_job.codex_build_receipt == artifacts[2].build_receipt
    assert bridge.released_for(job, FactorySkillStep.BRIEF_CODEX) == (
        artifacts[1].invocation.released_skill
    )


@pytest.mark.asyncio
async def test_forge_evidence_bridge_accepts_revision_evidence_on_retry(
    tmp_path: Path,
) -> None:
    revision = CandidateRevisionV1.model_validate(revision_payload())
    job = SimpleNamespace(
        job_id=revision.job_id,
        correlation_id=revision.correlation_id,
        event_id=revision.invocation_id,
        occurred_at=revision.occurred_at,
        subject_version=revision.subject_version,
        acceptance_assertion_ids=revision.acceptance_assertion_ids,
    )
    store = FilesystemFactoryEvidenceStore(tmp_path / "evidence")
    reference = await store.persist(
        job,  # type: ignore[arg-type]
        revision.model_dump_json(by_alias=True).encode("utf-8"),
    )
    repository = _ForgeRepository(
        (_forge_block(job, FactoryPhase.TOOL_CANDIDATE_TESTED, (reference,)),),
        revision.invocation.released_skill,
    )
    bridge = CaptainForgeEvidenceBridge(
        repository=repository,
        evidence_store=store,
    )

    assert bridge.workflow_artifacts(revision.job_id) == (revision,)


@pytest.mark.asyncio
async def test_forge_evidence_bridge_rejects_digest_mismatched_referenced_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    job, _, refs, repository = await _persist_forge_evidence(root)
    referenced_path = root / str(job.job_id) / f"{refs[0].sha256}.json"
    referenced_path.write_bytes(b'{"schema":"tampered"}')
    bridge = CaptainForgeEvidenceBridge(
        repository=repository,
        evidence_store=FilesystemFactoryEvidenceStore(root),
    )

    with pytest.raises(FactoryDispatchError, match="digest"):
        bridge.workflow_artifacts(job.job_id)


@pytest.mark.asyncio
async def test_forge_evidence_bridge_does_not_discover_unreferenced_store_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    job, artifacts, refs, repository = await _persist_forge_evidence(root)
    repository._blocks = (
        _forge_block(job, FactoryPhase.BLUEPRINT_CREATED, (refs[0],)),
        _forge_block(job, FactoryPhase.TOOL_CANDIDATE_TESTED, (refs[1],)),
    )
    bridge = CaptainForgeEvidenceBridge(
        repository=repository,
        evidence_store=FilesystemFactoryEvidenceStore(root),
    )

    assert bridge.workflow_artifacts(job.job_id) == artifacts[:2]
    with pytest.raises(FactoryDispatchError, match="exactly one"):
        CaptainCreationJobMapper(evidence=bridge).map(_forge_request(job))


@pytest.mark.asyncio
async def test_forge_evidence_bridge_rejects_malformed_or_wrong_job_references(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    job, artifacts, refs, repository = await _persist_forge_evidence(root)
    malformed = artifacts[0].model_dump(mode="json", by_alias=True)
    malformed["unexpected"] = True
    content = json.dumps(malformed, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    path = root / str(job.job_id) / f"{digest}.json"
    path.write_bytes(content)
    malformed_ref = ArtifactRef(
        uri=f"artifact://factory-evidence/{job.job_id}/{digest}",
        sha256=digest,
        media_type="application/json",
    )
    repository._blocks = (
        _forge_block(job, FactoryPhase.BLUEPRINT_CREATED, (malformed_ref,)),
    )
    bridge = CaptainForgeEvidenceBridge(
        repository=repository,
        evidence_store=FilesystemFactoryEvidenceStore(root),
    )

    with pytest.raises(FactoryDispatchError, match="schema"):
        bridge.workflow_artifacts(job.job_id)

    wrong_job_ref = refs[0].model_copy(
        update={
            "uri": (
                "artifact://factory-evidence/"
                f"{JOB_IDS[1]}/{refs[0].sha256}"
            )
        }
    )
    repository._blocks = (
        _forge_block(job, FactoryPhase.BLUEPRINT_CREATED, (wrong_job_ref,)),
    )
    with pytest.raises(FactoryDispatchError, match="job"):
        bridge.workflow_artifacts(job.job_id)


@pytest.mark.asyncio
async def test_forge_evidence_bridge_rejects_scalar_coercion_in_typed_json(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    job, artifacts, _, repository = await _persist_forge_evidence(root)
    scalar_brief = artifacts[1].model_dump(mode="json", by_alias=True)
    assignment = scalar_brief["build_assignment"]
    assert isinstance(assignment, dict)
    documentation_queries = assignment["documentation_queries"]
    assert isinstance(documentation_queries, list)
    query = documentation_queries[0]
    assert isinstance(query, dict)
    query["required"] = 1
    content = json.dumps(scalar_brief, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    path = root / str(job.job_id) / f"{digest}.json"
    path.write_bytes(content)
    scalar_ref = ArtifactRef(
        uri=f"artifact://factory-evidence/{job.job_id}/{digest}",
        sha256=digest,
        media_type="application/json",
    )
    repository._blocks = (
        _forge_block(job, FactoryPhase.TOOL_CANDIDATE_TESTED, (scalar_ref,)),
    )
    bridge = CaptainForgeEvidenceBridge(
        repository=repository,
        evidence_store=FilesystemFactoryEvidenceStore(root),
    )

    with pytest.raises(FactoryDispatchError, match="schema"):
        bridge.workflow_artifacts(job.job_id)


@pytest.mark.asyncio
async def test_forge_evidence_bridge_accepts_strict_json_timestamp_normalization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    job, artifacts, _, repository = await _persist_forge_evidence(root)
    inventory = artifacts[0].model_dump(mode="json", by_alias=True)
    inventory["occurred_at"] = "2026-07-21T10:01:00+00:00"
    content = json.dumps(inventory, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    path = root / str(job.job_id) / f"{digest}.json"
    path.write_bytes(content)
    normalized_ref = ArtifactRef(
        uri=f"artifact://factory-evidence/{job.job_id}/{digest}",
        sha256=digest,
        media_type="application/json",
    )
    repository._blocks = (
        _forge_block(job, FactoryPhase.BLUEPRINT_CREATED, (normalized_ref,)),
    )
    bridge = CaptainForgeEvidenceBridge(
        repository=repository,
        evidence_store=FilesystemFactoryEvidenceStore(root),
    )

    recovered = bridge.workflow_artifacts(job.job_id)

    assert len(recovered) == 1
    assert recovered[0].occurred_at == artifacts[0].occurred_at


def test_operator_cli_is_importable_from_scripts_directory() -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "run-agent-factory-business-demo.py"
    )
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=script.parent,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--hermes-max-usd" in completed.stdout
    assert "--hermes-python-executable" in completed.stdout
