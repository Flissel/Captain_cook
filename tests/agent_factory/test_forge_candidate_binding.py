from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path
from pathlib import PurePosixPath
from uuid import uuid4
import zipfile

import pytest

from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkContentAddressedArtifactStore,
)
from agenten.agent_factory.candidate_evaluation import (
    CandidateEvaluationFactory,
    FactoryCandidateManifest,
    GatewayForgeCandidateProvider,
)
from agenten.agent_factory.contracts import FactoryPhase, FactoryRole
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.forge_contracts import (
    CodexBuildReceiptV1,
    CreationPackageManifestV1,
    CreationPackageManifestV2,
    CreationResultV1,
    CreationResultV2,
    CreationJobV3,
)
from agenten.agent_factory.minibook_forge import FilesystemCaptainCreationJobV3Store
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from minibook.swarm.contracts import (
    CreationJobV1 as MinibookCreationJobV1,
    ForgeBuildSkillUsageReceiptV1 as MinibookForgeBuildSkillUsageReceiptV1,
)
from minibook.swarm.pipeline_adapter import (
    ContentAddressedCreationArtifactPublisher,
    CreationExportBundle,
)
from tests.agent_factory.test_candidate_evaluation import _write_candidate_archive
from tests.agent_factory.test_state_machine import job_v3
from minibook.tests.test_team_package import _fixture as v3_package_fixture
from minibook.swarm.team_package import seal_team_package, validate_team_package


class Blocks:
    def __init__(self, released_skill: ReleasedHermesSkill) -> None:
        self.items = ()
        self.released_skill = released_skill

    def blocks(self, job_id):
        assert not self.items or self.items[0].job_id == job_id
        return self.items

    def released_for(self, job, step):
        assert step is FactorySkillStep.BRIEF_CODEX
        return self.released_skill


def _durable_v3_result(
    tmp_path: Path,
    *,
    candidate_mutation: str | None = None,
    source_content_mutation: bool = False,
    stale_declared_test_paths: bool = False,
):
    minibook_store, _, _, refs, gaps = v3_package_fixture(tmp_path)
    build_bytes = minibook_store.read_bytes(refs["build_evidence_refs"][0])
    build_ref = minibook_store.put(
        build_bytes,
        "application/json",
        namespace="team-file",
    )
    real_payload = json.loads(
        minibook_store.read_bytes(refs["test_evidence_refs"][0])
    )
    real_payload["evidence_refs"] = [build_ref.model_dump(mode="json")]
    real_bytes = json.dumps(real_payload, sort_keys=True).encode("utf-8")
    real_ref = minibook_store.put(
        real_bytes,
        "application/json",
        namespace="team-file",
    )
    codex_payload = json.loads(
        minibook_store.read_bytes(refs["codex_build_receipt_ref"])
    )
    codex_payload["test_evidence_refs"] = [real_ref.model_dump(mode="json")]
    codex_bytes = json.dumps(codex_payload, sort_keys=True).encode("utf-8")
    codex_ref = minibook_store.put(
        codex_bytes,
        "application/json",
        namespace="team-file",
    )
    hermes_ref = minibook_store.put(
        minibook_store.read_bytes(refs["hermes_usage_receipt_ref"]),
        "application/json",
        namespace="team-file",
    )
    refs.update(
        {
            "hermes_usage_receipt_ref": hermes_ref,
            "codex_build_receipt_ref": codex_ref,
            "build_evidence_refs": (build_ref,),
            "test_evidence_refs": (real_ref,),
            "authority_effect": refs["authority_effect"].model_copy(
                update={
                    "hermes_usage_receipt_ref": hermes_ref,
                    "codex_build_receipt_ref": codex_ref,
                    "build_evidence_refs": (build_ref,),
                    "test_evidence_refs": (real_ref,),
                }
            ),
        }
    )
    validated = validate_team_package(
        artifact_store=minibook_store,
        tool_gaps=gaps,
        **refs,
    )
    authority_job = CreationJobV3.model_validate(
        refs["authority_job"].model_dump(mode="json", by_alias=True)
    )
    captain_job = job_v3(mode="demo").model_copy(
        update={
            "compiled_spec_ref": type(job_v3(mode="demo").compiled_spec_ref).model_validate(
                authority_job.compiled_spec_ref.model_dump(mode="json")
            ),
            "acceptance_assertion_ids": authority_job.public_assertion_ids,
        }
    )
    team_bytes = json.dumps(
        validated.team_manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    team_ref = minibook_store.put(team_bytes, "application/json", namespace="team-file")
    candidate_team_ref = team_ref
    if candidate_mutation == "team_ref":
        candidate_team_ref = refs["team_manifest_ref"]
    candidate_template = FactoryCandidateManifest(
        candidate_id="sales_pipeline_brief_v1",
        source_archive_ref=refs["source_archive_ref"].model_dump(mode="json"),
        team_manifest={
            "reference": candidate_team_ref.model_dump(mode="json"),
            "relative_path": "team_manifest.json",
        },
        build_command=("python", "-m", "compileall", "-q", "."),
        real_case_command=("python", "-m", "pytest"),
        timeout_seconds=60,
    )
    preseal_candidate = candidate_template.model_dump(mode="json", by_alias=True)
    preseal_candidate.pop("source_archive_ref")
    source_members = {
        path: validated.files[PurePosixPath(path)]
        for path in (
            *validated.team_manifest["source_files"],
            *validated.team_manifest["test_files"],
        )
    }
    if source_content_mutation:
        source_members[validated.team_manifest["source_files"][0]] = b"TEAM = 'substituted'\n"
    source_members["factory-candidate.json"] = json.dumps(
        preseal_candidate,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    source_stream = io.BytesIO()
    with zipfile.ZipFile(source_stream, "w") as archive:
        for path, content in sorted(source_members.items()):
            archive.writestr(path, content)
    authority_source_ref = minibook_store.put(
        source_stream.getvalue(),
        "application/zip",
        namespace="candidate-source",
    )
    candidate_source_ref = authority_source_ref
    if candidate_mutation == "source_ref":
        candidate_source_ref = minibook_store.put(
            source_stream.getvalue(),
            "application/zip",
            namespace="substituted-source",
        )
    candidate = FactoryCandidateManifest.model_validate(
        {
            **candidate_template.model_dump(mode="json", by_alias=True),
            "source_archive_ref": candidate_source_ref.model_dump(mode="json"),
        }
    )
    changed_files = dict(validated.files)
    build_path = PurePosixPath("evidence/build.json")
    build_payload = json.loads(changed_files[build_path])
    build_payload["source_archive_ref"] = authority_source_ref.model_dump(mode="json")
    changed_files[build_path] = json.dumps(build_payload, sort_keys=True).encode("utf-8")
    build_ref = minibook_store.put(
        changed_files[build_path], "application/json", namespace="team-file"
    )
    real_path = PurePosixPath("evidence/real-case.json")
    real_payload = json.loads(changed_files[real_path])
    real_payload["source_archive_ref"] = authority_source_ref.model_dump(mode="json")
    real_payload["evidence_refs"] = [build_ref.model_dump(mode="json")]
    if stale_declared_test_paths:
        real_payload["declared_test_paths"] = ["tests/substituted_test.py"]
    changed_files[real_path] = json.dumps(real_payload, sort_keys=True).encode("utf-8")
    real_ref = minibook_store.put(
        changed_files[real_path], "application/json", namespace="team-file"
    )
    codex_path = PurePosixPath("evidence/codex-build-receipt.json")
    codex_payload = json.loads(changed_files[codex_path])
    codex_payload["source_archive_ref"] = authority_source_ref.model_dump(mode="json")
    codex_payload["test_evidence_refs"] = [real_ref.model_dump(mode="json")]
    changed_files[codex_path] = json.dumps(codex_payload, sort_keys=True).encode("utf-8")
    codex_ref = minibook_store.put(
        changed_files[codex_path], "application/json", namespace="team-file"
    )
    refs["authority_effect"] = refs["authority_effect"].model_copy(
        update={
            "codex_build_receipt_ref": codex_ref,
            "source_archive_ref": authority_source_ref,
            "build_evidence_refs": (build_ref,),
            "test_evidence_refs": (real_ref,),
        }
    )
    candidate_bytes = json.dumps(
        preseal_candidate,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    validated = replace(
        validated,
        source_archive_ref=candidate_source_ref,
        files={
            **changed_files,
            PurePosixPath("factory-candidate.json"): candidate_bytes,
        },
    )
    sealed = seal_team_package(validated, artifact_store=minibook_store)
    result = CreationResultV2(
        creation_job_id=authority_job.creation_job_id,
        factory_job_id=authority_job.factory_job_id,
        correlation_id=authority_job.correlation_id,
        subject_version=authority_job.subject_version,
        attempt=authority_job.attempt,
        status="succeeded",
        team_manifest_ref=sealed.team_manifest_ref.model_dump(mode="json"),
        candidate_manifest_ref=sealed.candidate_manifest_ref.model_dump(mode="json"),
        source_archive_ref=sealed.source_archive_ref.model_dump(mode="json"),
        package_manifest_ref=sealed.package_manifest_ref.model_dump(mode="json"),
        hermes_usage_receipt_ref=sealed.hermes_usage_receipt_ref.model_dump(mode="json"),
        codex_build_receipt_ref=sealed.codex_build_receipt_ref.model_dump(mode="json"),
        build_evidence_refs=[item.model_dump(mode="json") for item in sealed.build_evidence_refs],
        real_case_evidence_refs=[item.model_dump(mode="json") for item in sealed.real_case_evidence_refs],
        cas_readback_receipt_ref=sealed.readback_receipt_ref.model_dump(mode="json"),
        tool_gaps=[gap.model_dump(mode="json", by_alias=True) for gap in gaps],
    )
    return (
        minibook_store,
        authority_job,
        refs["authority_assignment"],
        refs["authority_effect"],
        captain_job,
        candidate,
        result,
    )


class _GatewayCas:
    def __init__(self, minibook_store) -> None:
        self._store = minibook_store

    @staticmethod
    def _ref(ref):
        return __import__(
            "minibook.swarm.contracts", fromlist=["ArtifactRef"]
        ).ArtifactRef.model_validate(ref.model_dump(mode="json"))

    def read_bytes(self, ref):
        return self._store.read_bytes(self._ref(ref))

    def read_bytes_bounded(self, ref, *, max_bytes: int):
        content = self._store.read_bytes(self._ref(ref))
        if len(content) > max_bytes:
            raise ValueError("artifact exceeds bound")
        return content

    def local_path(self, ref):
        return self._store.local_path(self._ref(ref))


@pytest.mark.asyncio
async def test_v3_candidate_acceptance_survives_fresh_provider_and_store_instances(
    tmp_path: Path,
) -> None:
    (
        minibook_store,
        authority_job,
        authority_assignment,
        authority_effect,
        captain_job,
        candidate,
        result,
    ) = _durable_v3_result(tmp_path)
    durable_root = tmp_path / ".captain-cook" / "v3-jobs"
    first_store = FilesystemCaptainCreationJobV3Store(durable_root)
    first_store.record_creation_job(authority_job)
    first_store.record_assignment(authority_assignment)
    first_store.record_effect(authority_effect)
    blocks = Blocks(_released_skill(captain_job))

    first_provider = GatewayForgeCandidateProvider(
        repository=blocks,
        artifacts=_GatewayCas(minibook_store),
        v3_jobs=first_store,
    )
    validator = CandidateEvaluationFactory(
        provider=first_provider,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "captain-evidence"),
    )
    request = FactoryDispatch(
        job=captain_job,
        action=FactoryAction(
            kind=FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE,
            attempt=1,
        ),
        role=FactoryRole.TOOL_INTEGRATOR,
        lease=issue_factory_lease(
            job=captain_job,
            role=FactoryRole.TOOL_INTEGRATOR,
            attempt=1,
            workspace_ref="workspace://factory/v3-restart",
            now=captain_job.occurred_at,
        ),
    )
    block = await validator.record_creation_result(request, result)
    blocks.items = (block,)

    restarted = GatewayForgeCandidateProvider(
        repository=blocks,
        artifacts=_GatewayCas(minibook_store),
        v3_jobs=FilesystemCaptainCreationJobV3Store(durable_root),
    )
    resolved = restarted.candidate_for(captain_job)

    assert resolved.candidate == candidate
    assert resolved.source_archive == minibook_store.local_path(
        _GatewayCas._ref(candidate.source_archive_ref)
    )
    assert restarted.current_candidate_ref(captain_job, 1) == candidate.source_archive_ref


def test_v3_candidate_rejects_substituted_local_source_after_valid_cas_read(
    tmp_path: Path,
) -> None:
    (
        minibook_store,
        authority_job,
        authority_assignment,
        authority_effect,
        captain_job,
        _,
        result,
    ) = _durable_v3_result(tmp_path)
    substituted = tmp_path / "substituted.zip"
    with zipfile.ZipFile(substituted, "w") as archive:
        archive.writestr("unrelated.py", b"UNRELATED = True\n")
    codex_receipt = json.loads(
        minibook_store.read_bytes(_GatewayCas._ref(result.codex_build_receipt_ref))
    )
    candidate_source_sha256 = codex_receipt["source_archive_ref"]["sha256"]

    class SubstitutingGatewayCas(_GatewayCas):
        def local_path(self, ref):
            if ref.sha256 == candidate_source_sha256:
                return substituted
            return super().local_path(ref)

    durable = FilesystemCaptainCreationJobV3Store(
        tmp_path / ".captain-cook" / "v3-local-path-substitution"
    )
    durable.record_creation_job(authority_job)
    durable.record_assignment(authority_assignment)
    durable.record_effect(authority_effect)
    provider = GatewayForgeCandidateProvider(
        repository=Blocks(_released_skill(captain_job)),
        artifacts=SubstitutingGatewayCas(minibook_store),
        v3_jobs=durable,
    )

    with pytest.raises(
        FactoryDispatchError,
        match="candidate source archive binding is invalid",
    ):
        provider.accept_creation_result(captain_job, result)

    with pytest.raises(FactoryDispatchError, match="durable authority is unavailable"):
        durable.accepted_result_for(captain_job, 1)


def test_v3_package_validation_never_uses_local_source_path(tmp_path: Path) -> None:
    (
        minibook_store,
        authority_job,
        authority_assignment,
        authority_effect,
        captain_job,
        candidate,
        result,
    ) = _durable_v3_result(tmp_path)
    package_source_sha256 = result.source_archive_ref.sha256

    class PublicReadOnlyPackageCas(_GatewayCas):
        def read_bytes(self, _ref):
            raise AssertionError("V3 readback used unbounded read_bytes")

        def read_bytes_bounded(self, ref, *, max_bytes: int):
            content = self._store.read_bytes(self._ref(ref))
            if len(content) > max_bytes:
                raise ValueError("artifact exceeds bound")
            return content

        def local_path(self, ref):
            if ref.sha256 == package_source_sha256:
                raise AssertionError("package validation reopened the local path")
            return super().local_path(ref)

    durable = FilesystemCaptainCreationJobV3Store(
        tmp_path / ".captain-cook" / "v3-public-read-package"
    )
    durable.record_creation_job(authority_job)
    durable.record_assignment(authority_assignment)
    durable.record_effect(authority_effect)
    provider = GatewayForgeCandidateProvider(
        repository=Blocks(_released_skill(captain_job)),
        artifacts=PublicReadOnlyPackageCas(minibook_store),
        v3_jobs=durable,
    )

    resolved = provider.accept_creation_result(captain_job, result)

    assert resolved.candidate == candidate


def test_v3_package_zip_bomb_is_rejected_in_memory() -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(
        stream,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("bomb.bin", b"0" * (2 * 1024 * 1024))

    with pytest.raises(ValueError, match="member is unsafe"):
        GatewayForgeCandidateProvider._safe_v3_package_zip_files(
            stream.getvalue(),
            maximum_files=10,
        )


@pytest.mark.parametrize(
    "names",
    (
        ("../escape.py",),
        ("CON.txt",),
        ("Agent.py", "agent.py"),
        ("café.py", "cafe\u0301.py"),
    ),
)
def test_v3_package_rejects_ambiguous_or_unsafe_member_names(
    names: tuple[str, ...],
) -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name in names:
            archive.writestr(name, b"safe")

    with pytest.raises(ValueError, match="member is unsafe"):
        GatewayForgeCandidateProvider._safe_v3_package_zip_files(
            stream.getvalue(),
            maximum_files=10,
        )


def test_v3_package_rejects_raw_backslash_member_name() -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("absolute/path.py", b"safe")
    content = stream.getvalue().replace(b"absolute/path.py", b"absolute\\path.py")

    with pytest.raises(ValueError, match="member is unsafe"):
        GatewayForgeCandidateProvider._safe_v3_package_zip_files(
            content,
            maximum_files=10,
        )


@pytest.mark.parametrize("mutation", ("source_ref", "team_ref"))
def test_v3_candidate_manifest_never_normalizes_substituted_references(
    tmp_path: Path,
    mutation: str,
) -> None:
    (
        minibook_store,
        authority_job,
        authority_assignment,
        authority_effect,
        captain_job,
        _,
        result,
    ) = _durable_v3_result(tmp_path, candidate_mutation=mutation)
    durable = FilesystemCaptainCreationJobV3Store(
        tmp_path / ".captain-cook" / "v3-substitution"
    )
    durable.record_creation_job(authority_job)
    durable.record_assignment(authority_assignment)
    durable.record_effect(authority_effect)

    provider = GatewayForgeCandidateProvider(
        repository=Blocks(_released_skill(captain_job)),
        artifacts=_GatewayCas(minibook_store),
        v3_jobs=durable,
    )

    with pytest.raises(FactoryDispatchError, match="candidate binding is invalid"):
        provider.accept_creation_result(captain_job, result)


@pytest.mark.parametrize("mutation", ("duplicate", "unverified", "reordered"))
def test_v3_readback_receipt_requires_exact_verified_canonical_sequence(
    tmp_path: Path,
    mutation: str,
) -> None:
    (
        minibook_store,
        authority_job,
        authority_assignment,
        authority_effect,
        captain_job,
        _,
        result,
    ) = _durable_v3_result(tmp_path)
    receipt = json.loads(
        minibook_store.read_bytes(_GatewayCas._ref(result.cas_readback_receipt_ref))
    )
    if mutation == "duplicate":
        receipt["artifacts"].append(dict(receipt["artifacts"][0]))
    elif mutation == "unverified":
        receipt["artifacts"].append(
            {**receipt["artifacts"][0], "verified": False}
        )
    else:
        receipt["artifacts"] = list(reversed(receipt["artifacts"]))
    receipt_ref = minibook_store.put(
        json.dumps(
            receipt,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n",
        "application/json",
        namespace="mutated-readback",
    )
    result = result.model_copy(update={"cas_readback_receipt_ref": receipt_ref})
    durable = FilesystemCaptainCreationJobV3Store(
        tmp_path / ".captain-cook" / "v3-readback-sequence"
    )
    durable.record_creation_job(authority_job)
    durable.record_assignment(authority_assignment)
    durable.record_effect(authority_effect)

    provider = GatewayForgeCandidateProvider(
        repository=Blocks(_released_skill(captain_job)),
        artifacts=_GatewayCas(minibook_store),
        v3_jobs=durable,
    )

    with pytest.raises(FactoryDispatchError, match="readback receipt is invalid"):
        provider.accept_creation_result(captain_job, result)


def test_v3_receipts_reject_stale_coherent_assignment_and_effect_identity(
    tmp_path: Path,
) -> None:
    (
        minibook_store,
        authority_job,
        authority_assignment,
        authority_effect,
        captain_job,
        _,
        result,
    ) = _durable_v3_result(tmp_path)
    stale_assignment = authority_assignment.model_copy(
        update={"checkpoint_version": authority_assignment.checkpoint_version + 1}
    )
    stale_effect = authority_effect.model_copy(
        update={"checkpoint_version": stale_assignment.checkpoint_version}
    )
    durable = FilesystemCaptainCreationJobV3Store(
        tmp_path / ".captain-cook" / "v3-stale-assignment"
    )
    durable.record_creation_job(authority_job)
    durable.record_assignment(stale_assignment)
    durable.record_effect(stale_effect)
    provider = GatewayForgeCandidateProvider(
        repository=Blocks(_released_skill(captain_job)),
        artifacts=_GatewayCas(minibook_store),
        v3_jobs=durable,
    )

    with pytest.raises(FactoryDispatchError, match="stale or failed"):
        provider.accept_creation_result(captain_job, result)


def test_v3_result_fails_closed_without_durable_assignment_and_effect(
    tmp_path: Path,
) -> None:
    minibook_store, authority_job, _, _, captain_job, _, result = _durable_v3_result(
        tmp_path
    )

    class CreationOnlyAuthority:
        def creation_job_for(self, _job, _attempt):
            return authority_job

    provider = GatewayForgeCandidateProvider(
        repository=Blocks(_released_skill(captain_job)),
        artifacts=_GatewayCas(minibook_store),
        v3_jobs=CreationOnlyAuthority(),
    )

    with pytest.raises(FactoryDispatchError, match="assignment or effect authority"):
        provider.accept_creation_result(captain_job, result)


def test_v3_rejects_coherently_substituted_candidate_source_code(
    tmp_path: Path,
) -> None:
    (
        minibook_store,
        authority_job,
        authority_assignment,
        authority_effect,
        captain_job,
        _,
        result,
    ) = _durable_v3_result(tmp_path, source_content_mutation=True)
    durable = FilesystemCaptainCreationJobV3Store(
        tmp_path / ".captain-cook" / "v3-source-substitution"
    )
    durable.record_creation_job(authority_job)
    durable.record_assignment(authority_assignment)
    durable.record_effect(authority_effect)

    class ReadOnlyGatewayCas(_GatewayCas):
        def local_path(self, ref):
            if ref.sha256 == authority_effect.source_archive_ref.sha256:
                raise AssertionError("candidate source validation must not request a path")
            return super().local_path(ref)

    provider = GatewayForgeCandidateProvider(
        repository=Blocks(_released_skill(captain_job)),
        artifacts=ReadOnlyGatewayCas(minibook_store),
        v3_jobs=durable,
    )

    with pytest.raises(FactoryDispatchError, match="candidate binding is invalid"):
        provider.accept_creation_result(captain_job, result)


def test_v3_real_case_paths_must_equal_final_team_manifest_order(
    tmp_path: Path,
) -> None:
    (
        minibook_store,
        authority_job,
        authority_assignment,
        authority_effect,
        captain_job,
        _,
        result,
    ) = _durable_v3_result(tmp_path, stale_declared_test_paths=True)
    durable = FilesystemCaptainCreationJobV3Store(
        tmp_path / ".captain-cook" / "v3-test-paths"
    )
    durable.record_creation_job(authority_job)
    durable.record_assignment(authority_assignment)
    durable.record_effect(authority_effect)
    provider = GatewayForgeCandidateProvider(
        repository=Blocks(_released_skill(captain_job)),
        artifacts=_GatewayCas(minibook_store),
        v3_jobs=durable,
    )

    with pytest.raises(FactoryDispatchError, match="stale or failed"):
        provider.accept_creation_result(captain_job, result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("cas", "Captain CAS readback digest mismatch"),
        ("stale_build", "evidence refs differ from the canonical package"),
        ("candidate_substitution", "result refs differ from sealed package"),
    ),
)
async def test_captain_rejects_minibook_v3_success_when_independent_readback_is_tampered(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    (
        minibook_store,
        authority_job,
        authority_assignment,
        authority_effect,
        captain_job,
        _,
        result,
    ) = _durable_v3_result(tmp_path)
    if mutation == "cas":
        minibook_store.local_path(_GatewayCas._ref(result.source_archive_ref)).write_bytes(
            b"changed-after-validation"
        )
    elif mutation == "stale_build":
        stale = minibook_store.put(
            minibook_store.read_bytes(_GatewayCas._ref(result.build_evidence_refs[0])),
            "application/json",
            namespace="stale-build",
        )
        result = result.model_copy(update={"build_evidence_refs": (stale,)})
    elif mutation == "candidate_substitution":
        substituted = minibook_store.put(
            minibook_store.read_bytes(_GatewayCas._ref(result.candidate_manifest_ref))
            + b" ",
            "application/json",
            namespace="substituted-candidate",
        )
        result = result.model_copy(update={"candidate_manifest_ref": substituted})

    durable = FilesystemCaptainCreationJobV3Store(
        tmp_path / ".captain-cook" / "v3-independent-readback"
    )
    durable.record_creation_job(authority_job)
    durable.record_assignment(authority_assignment)
    durable.record_effect(authority_effect)
    blocks = Blocks(_released_skill(captain_job))
    provider = GatewayForgeCandidateProvider(
        repository=blocks,
        artifacts=_GatewayCas(minibook_store),
        v3_jobs=durable,
    )
    evidence_root = tmp_path / "captain-evidence"
    validator = CandidateEvaluationFactory(
        provider=provider,
        evidence_store=FilesystemFactoryEvidenceStore(evidence_root),
    )
    lease = issue_factory_lease(
        job=captain_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/v3-readback",
        now=captain_job.occurred_at,
    )
    request = FactoryDispatch(
        job=captain_job,
        action=FactoryAction(kind=FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE, attempt=1),
        role=FactoryRole.TOOL_INTEGRATOR,
        lease=lease,
    )

    with pytest.raises(FactoryDispatchError, match=message):
        await validator.record_creation_result(request, result)
    assert blocks.items == ()
    assert not evidence_root.exists()


def _released_skill(factory_job) -> ReleasedHermesSkill:
    return ReleasedHermesSkill(
        schema="captain.released-hermes-skill.v1",
        skill_id="captain-factory-brief-codex",
        version=1,
        capability="factory_codex_build",
        content_ref=factory_job.input_ref,
        content_sha256=factory_job.input_ref.sha256,
        status="released",
        released_at=factory_job.occurred_at,
        producer="captain",
    )


def _sealed_result(tmp_path: Path, factory_job):
    archive_path = tmp_path / "candidate.zip"
    team_ref, workflow_ref, input_ref, output_ref, original_source_ref = _write_candidate_archive(
        archive_path
    )
    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "forge-cas"
    )
    candidate_template = FactoryCandidateManifest(
        candidate_id="support_triage_v1",
        source_archive_ref=original_source_ref,
        team_manifest={"reference": team_ref, "relative_path": "team_manifest.json"},
        workflow_artifacts=(
            {
                "reference": workflow_ref,
                "relative_path": "workflows/support_triage.json",
            },
        ),
        tool_schema_artifacts=(
            {
                "reference": input_ref,
                "relative_path": "schemas/support_triage.input.json",
            },
            {
                "reference": output_ref,
                "relative_path": "schemas/support_triage.output.json",
            },
        ),
        n8n_tools=(
            TypedN8nTool(
                name="support_triage",
                description="Route a support request.",
                input_schema_ref=input_ref.uri,
                output_schema_ref=output_ref.uri,
            ),
        ),
        build_command=("python", "-m", "compileall", "-q", "."),
        real_case_command=("python", "run_case.py"),
        timeout_seconds=10,
    )
    candidate_payload = candidate_template.model_dump(mode="json", by_alias=True)
    candidate_payload.pop("source_archive_ref")
    preseal_candidate_bytes = json.dumps(
        candidate_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with zipfile.ZipFile(archive_path, "a") as archive:
        archive.writestr("factory-candidate.json", preseal_candidate_bytes)
    original_source_ref = type(original_source_ref)(
        uri=original_source_ref.uri,
        sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        media_type=original_source_ref.media_type,
    )
    creation_job_id = uuid4()
    creation_job = MinibookCreationJobV1(
        creation_job_id=creation_job_id,
        factory_job_id=factory_job.job_id,
        correlation_id=factory_job.correlation_id,
        causation_id=factory_job.event_id,
        subject_version=factory_job.subject_version,
        attempt=1,
        idempotency_key="0" * 64,
        input_ref=factory_job.input_ref.model_dump(mode="json"),
        compiled_spec_ref=factory_job.compiled_spec_ref.model_dump(mode="json"),
        dependency_graph_ref=factory_job.dependency_graph_ref.model_dump(mode="json"),
        released_skill={
            "skill_id": "captain-factory-brief-codex",
            "version": 1,
            "content_ref": factory_job.input_ref.model_dump(mode="json"),
            "content_sha256": factory_job.input_ref.sha256,
        },
        public_assertion_ids=factory_job.acceptance_assertion_ids,
        deadline_at=factory_job.deadline_at,
    )
    skill_usage_receipt = MinibookForgeBuildSkillUsageReceiptV1(
        schema="hermes.forge-build-skill-usage-receipt.v1",
        producer="hermes",
        outcome="fulfilled",
        creation_job_id=creation_job.creation_job_id,
        factory_job_id=creation_job.factory_job_id,
        correlation_id=creation_job.correlation_id,
        subject_version=creation_job.subject_version,
        attempt=creation_job.attempt,
        idempotency_key=creation_job.idempotency_key,
        released_skill=creation_job.released_skill,
        public_assertion_ids=creation_job.public_assertion_ids,
        evidence_refs=(
            {
                "uri": "artifact://forge/build-evidence/" + "9" * 64,
                "sha256": "9" * 64,
                "media_type": "application/json",
            },
        ),
    )
    receipt = ContentAddressedCreationArtifactPublisher(store).publish(
        creation_job,
        CreationExportBundle(
            source_archive=archive_path.read_bytes(),
            candidate_manifest=candidate_payload,
            skill_usage_receipt=skill_usage_receipt.model_dump_json(
                by_alias=True
            ).encode("utf-8"),
        ),
    )
    candidate_ref = receipt.candidate_manifest_ref
    source_ref = receipt.source_archive_ref
    candidate = FactoryCandidateManifest.model_validate_json(
        store.read_bytes(
            type(factory_job.input_ref).model_validate(
                candidate_ref.model_dump(mode="json")
            )
        )
    )
    result = CreationResultV1(
        creation_job_id=creation_job_id,
        correlation_id=factory_job.correlation_id,
        subject_version=factory_job.subject_version,
        attempt=1,
        status="succeeded",
        package_manifest_ref=receipt.package_manifest_ref.model_dump(mode="json"),
        artifact_refs=(
            candidate_ref.model_dump(mode="json"),
            source_ref.model_dump(mode="json"),
        ),
        skill_usage_receipt_ref=receipt.skill_usage_receipt_ref.model_dump(mode="json"),
    )
    return store, candidate, result, source_ref


def _upgrade_result_to_codex_package(store, factory_job, result):
    ref_type = type(factory_job.input_ref)
    package_ref = ref_type.model_validate(
        result.package_manifest_ref.model_dump(mode="json")
    )
    package = CreationPackageManifestV1.model_validate_json(
        store.read_bytes(package_ref)
    )
    source_path = store.local_path(
        ref_type.model_validate(package.source_archive_ref.model_dump(mode="json"))
    )
    with zipfile.ZipFile(source_path) as archive:
        preseal_candidate_bytes = archive.read("factory-candidate.json")
    preseal_candidate_sha = hashlib.sha256(preseal_candidate_bytes).hexdigest()
    receipt = CodexBuildReceiptV1(
        receipt_id=uuid4(),
        producer="captain",
        outcome="sealed",
        factory_job_id=factory_job.job_id,
        creation_job_id=package.creation_job_id,
        correlation_id=factory_job.correlation_id,
        subject_version=factory_job.subject_version,
        attempt=1,
        assignment_id=uuid4(),
        idempotency_key="0" * 64,
        seal_idempotency_key="1" * 64,
        build_brief_ref={
            "uri": f"artifact://captain-codex-build/build-brief/{'2' * 64}",
            "sha256": "2" * 64,
            "media_type": "application/json",
        },
        workspace_ref="workspace://factory/support-triage",
        codex_session_ref={
            "uri": f"artifact://captain-codex-build/session/{'3' * 64}",
            "sha256": "3" * 64,
            "media_type": "application/json",
        },
        workspace_snapshot_ref={
            "uri": f"artifact://captain-codex-build/workspace/{'4' * 64}",
            "sha256": "4" * 64,
            "media_type": "application/zip",
        },
        candidate_manifest_ref={
            "uri": f"artifact://captain-codex-build/candidate/{preseal_candidate_sha}",
            "sha256": preseal_candidate_sha,
            "media_type": "application/json",
        },
        source_archive_ref=package.source_archive_ref,
        test_evidence_refs=(
            {
                "uri": f"artifact://captain-codex-build/test/{'5' * 64}",
                "sha256": "5" * 64,
                "media_type": "application/json",
            },
        ),
        acceptance_assertion_ids=factory_job.acceptance_assertion_ids,
        completed_at=factory_job.occurred_at,
    )
    receipt_bytes = json.dumps(
        receipt.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    receipt_ref = store.put(
        receipt_bytes,
        "application/json",
        namespace="captain-codex-build-receipt",
    )
    package_payload = package.model_dump(mode="python", by_alias=False)
    package_payload.pop("schema_name")
    package_v2 = CreationPackageManifestV2(
        **package_payload,
        codex_build_receipt_ref=receipt_ref.model_dump(mode="json"),
    )
    package_ref = store.put(
        package_v2.model_dump_json(by_alias=True).encode("utf-8"),
        "application/json",
        namespace="forge-package-manifest-v2",
    )
    return result.model_copy(
        update={
            "package_manifest_ref": package_ref,
            "artifact_refs": (*result.artifact_refs, receipt_ref),
        }
    )


def test_gateway_accepts_v2_package_only_with_bound_captain_codex_receipt(
    tmp_path: Path,
) -> None:
    factory_job = job_v3(mode="demo")
    store, _, result, _ = _sealed_result(tmp_path, factory_job)
    v2_result = _upgrade_result_to_codex_package(store, factory_job, result)
    provider = GatewayForgeCandidateProvider(
        repository=Blocks(_released_skill(factory_job)),
        artifacts=store,
    )

    assert provider.accept_creation_result(factory_job, v2_result).candidate.candidate_id

    unbound = v2_result.model_copy(
        update={"artifact_refs": v2_result.artifact_refs[:-1]}
    )
    with pytest.raises(FactoryDispatchError, match="Codex build receipt"):
        provider.accept_creation_result(factory_job, unbound)


@pytest.mark.asyncio
async def test_gateway_forge_candidate_is_available_only_after_authoritative_code_block(
    tmp_path: Path,
) -> None:
    factory_job = job_v3(mode="demo")
    store, candidate, result, source_ref = _sealed_result(tmp_path, factory_job)
    blocks = Blocks(_released_skill(factory_job))
    provider = GatewayForgeCandidateProvider(repository=blocks, artifacts=store)
    validator = CandidateEvaluationFactory(
        provider=provider,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
    )
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=factory_job.occurred_at,
    )
    request = FactoryDispatch(
        job=factory_job,
        action=FactoryAction(
            kind=FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE,
            attempt=1,
        ),
        role=FactoryRole.TOOL_INTEGRATOR,
        lease=lease,
    )

    with pytest.raises(FileNotFoundError, match="authoritative"):
        provider.candidate_for(factory_job)

    block = await validator.record_creation_result(request, result)
    assert block.phase is FactoryPhase.AGENT_CODE_CREATED
    assert provider.current_candidate_ref(factory_job, 1) is None

    blocks.items = (block,)
    resolved = provider.candidate_for(factory_job)

    assert resolved.candidate == candidate
    assert resolved.source_archive == store.local_path(
        type(factory_job.input_ref).model_validate(source_ref.model_dump(mode="json"))
    )
    assert provider.current_candidate_ref(factory_job, 1) == type(
        factory_job.input_ref
    ).model_validate(source_ref.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_gateway_rejects_creation_result_when_package_bytes_are_unavailable(
    tmp_path: Path,
) -> None:
    factory_job = job_v3(mode="demo")
    store, _, result, _ = _sealed_result(tmp_path, factory_job)
    unavailable = result.package_manifest_ref.model_copy(
        update={"uri": "artifact://business-benchmark-production/missing/" + "0" * 64}
    )
    forged = result.model_copy(update={"package_manifest_ref": unavailable})
    provider = GatewayForgeCandidateProvider(
        repository=Blocks(_released_skill(factory_job)), artifacts=store
    )
    validator = CandidateEvaluationFactory(
        provider=provider,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
    )
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=factory_job.occurred_at,
    )

    with pytest.raises(FactoryDispatchError, match="package manifest"):
        await validator.record_creation_result(
            FactoryDispatch(
                job=factory_job,
                action=FactoryAction(
                    kind=FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE,
                    attempt=1,
                ),
                role=FactoryRole.TOOL_INTEGRATOR,
                lease=lease,
            ),
            forged,
        )


def test_gateway_rejects_local_source_path_that_differs_from_verified_cas_bytes(
    tmp_path: Path,
) -> None:
    factory_job = job_v3(mode="demo")
    store, _, result, _ = _sealed_result(tmp_path, factory_job)
    divergent_path = tmp_path / "divergent-candidate.zip"
    divergent_path.write_bytes(b"not-the-verified-source-archive")

    class DivergentArtifactStore:
        def read_bytes(self, reference):
            return store.read_bytes(reference)

        def local_path(self, _reference):
            return divergent_path

    provider = GatewayForgeCandidateProvider(
        repository=Blocks(_released_skill(factory_job)),
        artifacts=DivergentArtifactStore(),
    )

    with pytest.raises(FactoryDispatchError, match="local source archive"):
        provider.accept_creation_result(factory_job, result)


def _replace_skill_receipt(
    store: BusinessBenchmarkContentAddressedArtifactStore,
    result: CreationResultV1,
    content: bytes,
) -> CreationResultV1:
    assert result.package_manifest_ref is not None
    skill_ref = store.put(content, "application/json", namespace="invalid-skill-receipt")
    package_ref = type(skill_ref).model_validate(
        result.package_manifest_ref.model_dump(mode="json")
    )
    package = CreationPackageManifestV1.model_validate_json(store.read_bytes(package_ref))
    changed_package = package.model_copy(
        update={
            "skill_usage_receipt_ref": type(package.skill_usage_receipt_ref).model_validate(
                skill_ref.model_dump(mode="json")
            )
        }
    )
    changed_package_ref = store.put(
        changed_package.model_dump_json(by_alias=True).encode("utf-8"),
        "application/json",
        namespace="invalid-package-manifest",
    )
    return result.model_copy(
        update={
            "package_manifest_ref": type(result.package_manifest_ref).model_validate(
                changed_package_ref.model_dump(mode="json")
            ),
            "skill_usage_receipt_ref": type(result.skill_usage_receipt_ref).model_validate(
                skill_ref.model_dump(mode="json")
            ),
        }
    )


def test_gateway_rejects_schema_only_forge_build_skill_receipt(tmp_path: Path) -> None:
    factory_job = job_v3(mode="demo")
    store, _, result, _ = _sealed_result(tmp_path, factory_job)
    forged = _replace_skill_receipt(
        store,
        result,
        b'{"schema":"hermes.forge-build-skill-usage-receipt.v1"}',
    )
    provider = GatewayForgeCandidateProvider(
        repository=Blocks(_released_skill(factory_job)),
        artifacts=store,
    )

    with pytest.raises(FactoryDispatchError, match="skill usage receipt"):
        provider.accept_creation_result(factory_job, forged)


def test_gateway_rejects_receipt_skill_not_released_by_captain(tmp_path: Path) -> None:
    factory_job = job_v3(mode="demo")
    store, _, result, _ = _sealed_result(tmp_path, factory_job)
    wrong = _released_skill(factory_job).model_copy(update={"skill_id": "wrong-skill"})
    provider = GatewayForgeCandidateProvider(repository=Blocks(wrong), artifacts=store)

    with pytest.raises(FactoryDispatchError, match="released skill"):
        provider.accept_creation_result(factory_job, result)
