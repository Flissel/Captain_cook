from __future__ import annotations

import asyncio
import hashlib
import json
import signal
import time
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from agenten.agent_factory.contracts import (
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.candidate_evaluation import (
    FactoryCandidateEvaluationResult,
    FactoryEvaluationCheck,
)
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_factory.hermes_cli import (
    FactorySkillReplayPendingError,
    FactorySkillReplayHermesRetryableFailureError,
    FactorySkillReplayClaim,
    FactorySkillReplayRetryableFailureError,
    FilesystemFactorySkillReplayStore,
    FilesystemReleasedFactorySkillCatalog,
    HermesCliFactory,
    HermesCliSettings,
    _codex_brief_seed_sha256,
    _discovery_seed_sha256,
    _hermes_retry_exceeds_authority,
    _parse_codex_brief_attestation,
    _parse_discovery_attestation,
    _same_or_valid_successor_lease,
    factory_skill_replay_failure_ref,
    _captain_discovery_seed,
    _require_improvement_artifact_binding,
    InMemoryFactorySkillReplayStore,
)
from agenten.agent_factory.codex_build_execution import (
    FactoryCodexBuildFailed,
    FactoryCodexBuildInterrupted,
)
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.skill_sequence import (
    FactoryImprovementAuthorizationV1,
    build_factory_hermes_replay_retry_authorization,
    FactoryRuntimeRetryAuthorizationV1,
)
from agenten.agent_factory.skill_evaluation import (
    HermesSkillEvaluationEvidence,
    HermesSkillEvaluationRequest,
    HermesSkillUsageReceipt,
    ReleasedHermesSkill,
)
from agenten.agent_factory.skill_workflow_contracts import (
    CandidateRevisionV1,
    CodebaseInventoryV1,
    CodexBuildBriefV1,
    CodexBuildEvidenceV1,
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamEvaluationV1,
)
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from tests.agent_factory.test_skill_evaluation_contracts import (
    evidence_payload,
    receipt_payload,
    request_payload,
)
from tests.agent_factory.test_state_machine import block, job, job_v3
from tests.agent_factory.test_skill_workflow_contracts import (
    brief_payload,
    evaluation_payload,
    feedback_payload,
    inventory_payload,
    invocation_payload,
    revision_payload,
)
from tests.agent_factory.test_codex_build_provenance_contracts import (
    evidence_payload as codex_build_evidence_payload,
    receipt_ref as codex_build_receipt_ref,
    seal_invocation_payload,
)


def test_hermes_retry_authority_limits_remaining_not_global_budget() -> None:
    assert not _hermes_retry_exceeds_authority(
        maximum_total_cost_usd=Decimal("0.20"),
        observed_cost_usd=Decimal("0.1904096"),
        maximum_additional_cost_usd=Decimal("0.01"),
    )
    assert _hermes_retry_exceeds_authority(
        maximum_total_cost_usd=Decimal("0.20"),
        observed_cost_usd=Decimal("0.18"),
        maximum_additional_cost_usd=Decimal("0.01"),
    )


_STEP_SKILL_NAMES = {
    FactorySkillStep.DISCOVER: "captain-factory-discover",
    FactorySkillStep.BRIEF_CODEX: "captain-factory-brief-codex",
    FactorySkillStep.SEAL_CODEX_BUILD: "captain-factory-seal-codex-build",
    FactorySkillStep.EXECUTE_TEAM: "captain-factory-execute-team",
    FactorySkillStep.EVALUATE_TEAM: "captain-factory-evaluate-team",
    FactorySkillStep.IMPROVE_TEAM: "captain-factory-improve-team",
    FactorySkillStep.REPORT_CAPTAIN: "captain-factory-report-captain",
}

_CAPTAIN_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


class CaptainBuildSealer:
    def __init__(self) -> None:
        self.calls: list[
            tuple[FactoryDispatch, FactorySkillInvocationV1, CodexBuildBriefV1]
        ] = []

    async def seal(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> CodexBuildEvidenceV1:
        self.calls.append((request, invocation, brief))
        assignment = brief.build_assignment
        payload = codex_build_evidence_payload()
        receipt = payload["build_receipt"]
        assert isinstance(receipt, dict)
        receipt.update(
            {
                "factory_job_id": str(request.job.job_id),
                "creation_job_id": str(assignment.creation_job_id),
                "correlation_id": str(request.job.correlation_id),
                "subject_version": request.job.subject_version,
                "attempt": invocation.attempt,
                "assignment_id": str(assignment.assignment_id),
                "idempotency_key": assignment.idempotency_key,
                "seal_idempotency_key": invocation.idempotency_key,
                "build_brief_ref": brief.artifact_ref.model_dump(mode="json"),
                "workspace_ref": assignment.workspace_ref,
                "acceptance_assertion_ids": list(
                    request.job.acceptance_assertion_ids
                ),
                "completed_at": invocation.lease.issued_at,
            }
        )
        receipt_reference = codex_build_receipt_ref(receipt)
        payload.update(
            {
                "invocation": invocation.model_dump(mode="json", by_alias=True),
                "invocation_id": str(invocation.invocation_id),
                "job_id": str(request.job.job_id),
                "correlation_id": str(request.job.correlation_id),
                "subject_version": request.job.subject_version,
                "attempt": invocation.attempt,
                "occurred_at": invocation.lease.issued_at,
                "evidence_refs": [receipt_reference],
                "acceptance_assertion_ids": list(
                    request.job.acceptance_assertion_ids
                ),
                "build_receipt_ref": receipt_reference,
                "build_receipt": receipt,
            }
        )
        return CodexBuildEvidenceV1.model_validate(payload)


def _directory_digest(path: Path) -> str:
    manifest = [
        {
            "path": item.relative_to(path).as_posix(),
            "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
            "size": item.stat().st_size,
        }
        for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
    ]
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class ReleasedCatalog:
    def __init__(self, releases: dict[FactorySkillStep, ReleasedHermesSkill]) -> None:
        self.releases = releases
        self.calls: list[FactorySkillStep] = []

    def released_for(self, factory_job: object, step: FactorySkillStep) -> ReleasedHermesSkill:
        del factory_job
        self.calls.append(step)
        return self.releases[step]


def _catalog_for(skill_root: Path, *steps: FactorySkillStep) -> ReleasedCatalog:
    releases: dict[FactorySkillStep, ReleasedHermesSkill] = {}
    for step in steps:
        name = _STEP_SKILL_NAMES[step]
        directory = skill_root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        digest = _directory_digest(directory)
        releases[step] = ReleasedHermesSkill.model_validate(
            {
                "schema": "captain.released-hermes-skill.v1",
                "skill_id": name,
                "version": 1,
                "capability": "factory_workflow",
                "content_ref": {
                    "uri": f"artifact://released-skills/{name}/v1",
                    "sha256": digest,
                    "media_type": "application/json",
                },
                "content_sha256": digest,
                "status": "released",
                "released_at": "2026-07-19T09:00:00Z",
                "producer": "captain",
            }
        )
    return ReleasedCatalog(releases)


def test_filesystem_released_catalog_loads_exact_job_step_release(
    tmp_path: Path,
) -> None:
    factory_job = job()
    skill_root = tmp_path / "skills"
    catalog_data = _catalog_for(skill_root, FactorySkillStep.DISCOVER)
    released = catalog_data.releases[FactorySkillStep.DISCOVER]
    release_path = (
        tmp_path
        / "catalog"
        / str(factory_job.job_id)
        / f"{FactorySkillStep.DISCOVER.value}.json"
    )
    release_path.parent.mkdir(parents=True)
    release_path.write_text(
        released.model_dump_json(by_alias=True),
        encoding="utf-8",
    )

    observed = FilesystemReleasedFactorySkillCatalog(
        tmp_path / "catalog"
    ).released_for(factory_job, FactorySkillStep.DISCOVER)

    assert observed == released


def _invocation_from_prompt(prompt: str) -> dict[str, object]:
    prefix = "captain_invocation_json="
    line = next(item for item in prompt.splitlines() if item.startswith(prefix))
    value = json.loads(line.removeprefix(prefix))
    assert isinstance(value, dict)
    return value


def _typed_payload(prompt: str, *, step: FactorySkillStep | None = None) -> dict[str, object]:
    invocation = _invocation_from_prompt(prompt)
    lease = invocation["lease"]
    assert isinstance(lease, dict)
    actual_step = FactorySkillStep(str(invocation["step"]))
    if step is not None:
        actual_step = step
    if actual_step is FactorySkillStep.DISCOVER:
        digest_prefix = "captain_discovery_seed_sha256="
        seed_sha256 = next(
            line.removeprefix(digest_prefix)
            for line in prompt.splitlines()
            if line.startswith(digest_prefix)
        )
        return {
            "schema": "hermes.factory-discovery-attestation.v1",
            "invocation_id": invocation["invocation_id"],
            "seed_sha256": seed_sha256,
            "accepted": True,
        }
    elif actual_step is FactorySkillStep.BRIEF_CODEX:
        payload = brief_payload()
        released = invocation["released_skill"]
        assert isinstance(released, dict)
        current_assignment = payload["build_assignment"]
        assert isinstance(current_assignment, dict)
        deadline_at = current_assignment["deadline_at"]
        assert isinstance(deadline_at, datetime)
        payload["build_assignment"] = {
            **current_assignment,
            "correlation_id": invocation["correlation_id"],
            "subject_version": invocation["subject_version"],
            "attempt": invocation["attempt"],
            "idempotency_key": invocation["idempotency_key"],
            "released_skill": {
                "skill_id": released["skill_id"],
                "version": released["version"],
                "content_ref": released["content_ref"],
                "content_sha256": released["content_sha256"],
            },
            "workspace_ref": lease["workspace_ref"],
            "public_assertion_ids": invocation["acceptance_assertion_ids"],
            "deadline_at": deadline_at.isoformat(),
        }
    elif actual_step is FactorySkillStep.IMPROVE_TEAM:
        payload = revision_payload()
    elif actual_step is FactorySkillStep.EVALUATE_TEAM:
        payload = evaluation_payload()
    elif actual_step is FactorySkillStep.REPORT_CAPTAIN:
        payload = feedback_payload()
    else:
        raise AssertionError(f"test payload is not implemented for {actual_step.value}")
    payload.update(
        {
            "invocation": invocation,
            "invocation_id": invocation["invocation_id"],
            "job_id": invocation["job_id"],
            "correlation_id": invocation["correlation_id"],
            "subject_version": invocation["subject_version"],
            "attempt": invocation["attempt"],
            "occurred_at": lease["issued_at"],
            "acceptance_assertion_ids": invocation["acceptance_assertion_ids"],
        }
    )
    return payload


def test_captain_discovery_seed_is_typed_and_content_addressed() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    invocation = FactorySkillInvocationV1.model_validate(
        invocation_payload("discover")
    )

    seed = _captain_discovery_seed(repository_root, invocation)
    parsed = CodebaseInventoryV1.model_validate(seed)

    assert parsed.invocation == invocation
    assert parsed.inspected_revision
    assert parsed.autogen_version == "0.7.5"
    assert parsed.source_refs
    assert parsed.test_refs
    assert parsed.schema_refs
    assert parsed.artifact_ref.uri.endswith(parsed.artifact_ref.sha256)


def test_discovery_attestation_accepts_one_extra_digest_suffix_character() -> None:
    invocation = FactorySkillInvocationV1.model_validate(invocation_payload("discover"))
    discovery_seed = {"schema": "captain.test-discovery-seed.v1"}
    expected_digest = _discovery_seed_sha256(discovery_seed)
    stdout = json.dumps(
        {
            "schema": "hermes.factory-discovery-attestation.v1",
            "invocation_id": str(invocation.invocation_id),
            "seed_sha256": expected_digest + "d",
            "accepted": True,
        }
    ).encode("utf-8")

    observed = _parse_discovery_attestation(
        stdout,
        invocation=invocation,
        discovery_seed=discovery_seed,
    )

    assert observed.seed_sha256 == expected_digest


def test_discovery_attestation_rejects_wrong_digest_with_extra_suffix() -> None:
    invocation = FactorySkillInvocationV1.model_validate(invocation_payload("discover"))
    discovery_seed = {"schema": "captain.test-discovery-seed.v1"}
    expected_digest = _discovery_seed_sha256(discovery_seed)
    stdout = json.dumps(
        {
            "schema": "hermes.factory-discovery-attestation.v1",
            "invocation_id": str(invocation.invocation_id),
            "seed_sha256": "0" + expected_digest[1:] + "d",
            "accepted": True,
        }
    ).encode("utf-8")

    with pytest.raises(
        FactoryDispatchError,
        match="exactly one typed discovery attestation",
    ):
        _parse_discovery_attestation(
            stdout,
            invocation=invocation,
            discovery_seed=discovery_seed,
        )


def test_demo_bootstrap_lease_accepts_exact_gateway_runtime_successor() -> None:
    payload = invocation_payload("discover")
    lease_payload = payload["lease"]
    assert isinstance(lease_payload, dict)
    lease_payload["workspace_ref"] = (
        "workspace://business-benchmark-demo/claims/epoch-"
        + "a" * 16
    )
    invocation = FactorySkillInvocationV1.model_validate(payload)
    bootstrap = invocation.lease
    successor = bootstrap.model_copy(
        update={
            "lease_id": "factory-runtime-successor",
            "issued_at": bootstrap.expires_at,
            "expires_at": bootstrap.expires_at + timedelta(minutes=15),
            "workspace_ref": (
                "workspace://business-benchmark-factory-v3/"
                f"{bootstrap.job_id}/dispatch_agent_architect/"
                f"{bootstrap.attempt}/"
                + bootstrap.expires_at.strftime("%Y%m%dT%H%M%S%fZ")
            ),
        }
    )

    assert _same_or_valid_successor_lease(bootstrap, successor)
    assert not _same_or_valid_successor_lease(
        bootstrap,
        successor.model_copy(
            update={
                "workspace_ref": successor.workspace_ref.replace(
                    str(bootstrap.job_id),
                    "00000000-0000-0000-0000-000000000000",
                )
            }
        ),
    )


def test_demo_bootstrap_lease_accepts_same_profile_epoch_successor() -> None:
    payload = invocation_payload("discover")
    lease_payload = payload["lease"]
    assert isinstance(lease_payload, dict)
    lease_payload["workspace_ref"] = (
        "workspace://business-benchmark-demo/claims/epoch-"
        + "a" * 16
    )
    invocation = FactorySkillInvocationV1.model_validate(payload)
    bootstrap = invocation.lease
    successor = bootstrap.model_copy(
        update={
            "lease_id": "factory-bootstrap-successor",
            "issued_at": bootstrap.expires_at,
            "expires_at": bootstrap.expires_at + timedelta(minutes=15),
            "workspace_ref": (
                "workspace://business-benchmark-demo/claims/epoch-"
                + "b" * 16
            ),
        }
    )

    assert _same_or_valid_successor_lease(bootstrap, successor)
    assert not _same_or_valid_successor_lease(
        bootstrap,
        successor.model_copy(
            update={
                "workspace_ref": successor.workspace_ref.replace(
                    "/claims/",
                    "/renewal/",
                )
            }
        ),
    )


def test_codex_brief_attestation_accepts_one_redundant_invocation_typo() -> None:
    brief = CodexBuildBriefV1.model_validate(brief_payload())
    invocation = brief.invocation
    expected_invocation_id = str(invocation.invocation_id)
    typo_invocation_id = expected_invocation_id[:-1] + (
        "0" if expected_invocation_id[-1] != "0" else "1"
    )
    stdout = json.dumps(
        {
            "schema": "hermes.factory-codex-brief-attestation.v1",
            "invocation_id": typo_invocation_id,
            "seed_sha256": _codex_brief_seed_sha256(brief),
            "accepted": True,
        }
    ).encode("utf-8")

    observed = _parse_codex_brief_attestation(
        stdout,
        invocation=invocation,
        codex_brief_seed=brief,
    )

    assert observed.invocation_id == invocation.invocation_id


def test_codex_brief_attestation_normalizes_redundant_valid_invocation_id() -> None:
    brief = CodexBuildBriefV1.model_validate(brief_payload())
    invocation = brief.invocation
    expected_invocation_id = str(invocation.invocation_id)
    typo_invocation_id = "11" + expected_invocation_id[2:]
    stdout = json.dumps(
        {
            "schema": "hermes.factory-codex-brief-attestation.v1",
            "invocation_id": typo_invocation_id,
            "seed_sha256": _codex_brief_seed_sha256(brief),
            "accepted": True,
        }
    ).encode("utf-8")

    observed = _parse_codex_brief_attestation(
        stdout,
        invocation=invocation,
        codex_brief_seed=brief,
    )

    assert observed.invocation_id == invocation.invocation_id


@pytest.mark.asyncio
async def test_dispatch_uses_oneshot_mode_for_parseable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_job = job()
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )
    request = FactoryDispatch(
        job=factory_job,
        action=FactoryAction(
            kind=FactoryActionKind.DISPATCH_AGENT_ARCHITECT,
            attempt=1,
            job_id=factory_job.job_id,
        ),
        role=FactoryRole.AGENT_ARCHITECT,
        lease=lease,
    )
    observed: tuple[str, ...] = ()
    recorded_workflow_artifacts: list[object] = []
    catalog = _catalog_for(tmp_path, FactorySkillStep.DISCOVER)

    class EvidenceStore:
        async def persist(self, _, content: bytes) -> ArtifactRef:
            return ArtifactRef(
                uri="artifact://factory-evidence/test/transcript",
                sha256="a" * 64,
                media_type="application/json",
            )

    class WorkflowArtifacts:
        def record_workflow_artifact(self, artifact: object) -> bool:
            recorded_workflow_artifacts.append(artifact)
            return True

    class Process:
        returncode = 0

        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        async def communicate(self) -> tuple[bytes, bytes]:
            return json.dumps(_typed_payload(self.prompt)).encode(), b""

    async def create_process(*command: str, **_: object) -> Process:
        nonlocal observed
        observed = command
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    evidence = await HermesCliFactory(
        settings=HermesCliSettings(
            skill_root=tmp_path,
            working_directory=Path(__file__).resolve().parents[2],
            evidence_root=tmp_path / "evidence",
        ),
        evidence_store=EvidenceStore(),
        workflow_artifacts=WorkflowArtifacts(),  # type: ignore[arg-type]
        released_skill_catalog=catalog,
        clock=lambda: lease.issued_at,
    ).dispatch(request)

    assert observed[0] == "hermes"
    assert observed[1:4] == (
        "--skills",
        "captain-factory-discover",
        "--ignore-rules",
    )
    assert "-z" in observed
    assert "--no-tools" in observed
    assert "chat" not in observed
    assert "/captain-factory-discover" in observed[-1]
    assert "captain_invocation_json=" in observed[-1]
    assert (
        "Return exactly one hermes.factory-discovery-attestation.v1 JSON object"
        in observed[-1]
    )
    assert "PydanticUndefined" not in observed[-1]
    assert "captain_output_json_schema=" in observed[-1]
    assert "captain_discovery_seed=" in observed[-1]
    assert "captain_discovery_seed_sha256=" in observed[-1]
    assert "do not call tools" in observed[-1]
    first_line = observed[-1].splitlines()[0]
    assert first_line.startswith(
        "Return this exact JSON as your first and only response: "
    )
    assert len(recorded_workflow_artifacts) == 1
    assert isinstance(recorded_workflow_artifacts[0], CodebaseInventoryV1)
    binding_prefix = "captain_required_output_bindings="
    binding_line = next(
        line for line in observed[-1].splitlines() if line.startswith(binding_prefix)
    )
    bindings = json.loads(binding_line.removeprefix(binding_prefix))
    assert json.loads(first_line.split(": ", 1)[1]) == bindings
    assert bindings["schema"] == "hermes.factory-discovery-attestation.v1"
    assert bindings["invocation_id"] == _invocation_from_prompt(observed[-1])["invocation_id"]
    assert bindings["seed_sha256"]
    assert bindings["accepted"] is True
    assert f'"lease_id":"{lease.lease_id}"' in observed[-1]
    assert evidence.phase is FactoryPhase.BLUEPRINT_CREATED
    assert any(
        ref.uri == "artifact://factory-evidence/test/transcript"
        for ref in evidence.evidence_refs
    )


@pytest.mark.asyncio
async def test_v3_brief_reuses_discovery_and_accepts_only_digest_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_job = job_v3(mode="demo")
    architect_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/support-triage/discovery",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )
    tool_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage/build",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )
    catalog = _catalog_for(
        tmp_path,
        FactorySkillStep.DISCOVER,
        FactorySkillStep.BRIEF_CODEX,
        FactorySkillStep.SEAL_CODEX_BUILD,
    )
    replay_store = InMemoryFactorySkillReplayStore()
    sealer = CaptainBuildSealer()
    prompts: list[str] = []
    commands: list[tuple[str, ...]] = []

    class PromptStore:
        def __init__(self) -> None:
            self.content: dict[str, bytes] = {}

        def persist(self, job_id: UUID, content: bytes) -> ArtifactRef:
            digest = hashlib.sha256(content).hexdigest()
            reference = ArtifactRef(
                uri=f"artifact://factory-prompts/{job_id}/{digest}",
                sha256=digest,
                media_type="application/json",
            )
            self.content[reference.uri] = content
            return reference

    prompt_store = PromptStore()

    class EvidenceStore:
        async def persist(self, _, content: bytes) -> ArtifactRef:
            digest = hashlib.sha256(content).hexdigest()
            return ArtifactRef(
                uri=f"artifact://factory-evidence/test/{digest}",
                sha256=digest,
                media_type="application/json",
            )

    class Process:
        returncode = 0

        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        async def communicate(self) -> tuple[bytes, bytes]:
            prompts.append(self.prompt)
            if "captain_discovery_seed_sha256=" in self.prompt:
                return json.dumps(_typed_payload(self.prompt)).encode(), b""
            digest_prefix = "captain_codex_brief_seed_sha256="
            digest = next(
                line.removeprefix(digest_prefix)
                for line in self.prompt.splitlines()
                if line.startswith(digest_prefix)
            )
            return json.dumps(
                {
                    "schema": "hermes.factory-codex-brief-attestation.v1",
                    "invocation_id": _invocation_from_prompt(self.prompt)[
                        "invocation_id"
                    ],
                    "seed_sha256": digest,
                    "accepted": True,
                }
            ).encode(), b""

    async def create_process(*command: str, **__: object) -> Process:
        commands.append(command)
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    factory = HermesCliFactory(
        settings=HermesCliSettings(
            skill_root=tmp_path,
            working_directory=_CAPTAIN_WORKSPACE_ROOT,
            evidence_root=tmp_path / "evidence",
        ),
        evidence_store=EvidenceStore(),
        released_skill_catalog=catalog,
        replay_store=replay_store,
        codex_build_sealer=sealer,
        codex_prompt_artifact_store=prompt_store,
        clock=lambda: architect_lease.issued_at,
    )

    await factory.dispatch(
        FactoryDispatch(
            job=factory_job,
            action=FactoryAction(
                kind=FactoryActionKind.DISPATCH_AGENT_ARCHITECT,
                attempt=1,
                job_id=factory_job.job_id,
            ),
            role=FactoryRole.AGENT_ARCHITECT,
            lease=architect_lease,
        )
    )
    evidence = await factory.dispatch(
        FactoryDispatch(
            job=factory_job,
            action=FactoryAction(
                kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
                attempt=1,
                job_id=factory_job.job_id,
            ),
            role=FactoryRole.TOOL_INTEGRATOR,
            lease=tool_lease,
        )
    )

    assert len(prompts) == 2
    assert "captain_codex_brief_seed=" in prompts[1]
    assert "Return exactly one hermes.factory-codex-brief-attestation.v1" in prompts[1]
    assert "--no-tools" in commands[1]
    assert len(prompt_store.content) == 1
    assert len(sealer.calls) == 1
    brief = sealer.calls[0][2]
    discovery = await replay_store.completed(
        factory_job,
        step=FactorySkillStep.DISCOVER,
        attempt=1,
    )
    assert discovery.artifact is not None
    assert brief.invocation.input_ref == discovery.artifact.artifact_ref
    assert brief.prompt_ref.uri in prompt_store.content
    assert brief.build_assignment.compiled_spec_ref.model_dump(mode="json") == (
        factory_job.compiled_spec_ref.model_dump(mode="json")
    )
    assert brief.build_assignment.dependency_graph_ref.model_dump(mode="json") == (
        factory_job.dependency_graph_ref.model_dump(mode="json")
    )
    assert brief.build_assignment.workspace_ref == tool_lease.workspace_ref
    assert evidence.phase is FactoryPhase.TOOL_CANDIDATE_TESTED


@pytest.mark.asyncio
async def test_module_root_runs_only_the_checkout_cli_with_bound_cwd_and_pythonpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    module_root = workspace_root / "hermes-agent"
    entrypoint = module_root / "hermes_cli" / "main.py"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("# checkout entrypoint\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", "global-hermes-location")
    observed_command: tuple[str, ...] = ()
    observed_options: dict[str, object] = {}

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"{}", b""

    async def create_process(*command: str, **options: object) -> Process:
        nonlocal observed_command, observed_options
        observed_command = command
        observed_options = options
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    output = await HermesCliFactory(
        settings=HermesCliSettings(
            executable="python.exe",
            module_root=module_root,
            working_directory=workspace_root,
            evidence_root=tmp_path / "evidence",
            maximum_output_tokens=1536,
        )
    )._run_skill_prompt("sealed prompt", max_seconds=30)

    resolved_root = module_root.resolve()
    environment = observed_options["env"]
    assert isinstance(environment, dict)
    assert output == b"{}"
    assert observed_command == (
        "python.exe",
        "-m",
        "hermes_cli.main",
        "-z",
        "sealed prompt",
    )
    assert Path(observed_options["cwd"]) == workspace_root.resolve()
    assert environment["PYTHONPATH"] == str(resolved_root)
    assert environment["PYTHONPATH"] != "global-hermes-location"
    assert environment["HERMES_MAX_ITERATIONS"] == "16"
    assert environment["HERMES_MAX_TOKENS"] == "1536"


def test_hermes_output_token_bound_is_strict() -> None:
    with pytest.raises(ValueError, match="output tokens"):
        HermesCliSettings(maximum_output_tokens=0)
    with pytest.raises(ValueError, match="output tokens"):
        HermesCliSettings(maximum_output_tokens=8193)


@pytest.mark.asyncio
async def test_pinned_hermes_model_requires_usage_evidence_and_stops_after_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_root = tmp_path / "hermes-agent"
    entrypoint = module_root / "hermes_cli" / "main.py"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("# checkout entrypoint\n", encoding="utf-8")
    observed_commands: list[tuple[str, ...]] = []

    class Process:
        returncode = 0

        def __init__(self, command: tuple[str, ...]) -> None:
            self.command = command

        async def communicate(self) -> tuple[bytes, bytes]:
            usage_path = Path(self.command[self.command.index("--usage-file") + 1])
            usage_path.write_text(
                json.dumps(
                    {
                        "estimated_cost_usd": "0.03",
                        "cost_status": "estimated",
                        "model": "gpt-4.1-mini",
                        "provider": "openai-api",
                        "api_calls": 1,
                        "failed": False,
                    }
                ),
                encoding="utf-8",
            )
            return b"{}", b""

    async def create_process(*command: str, **_: object) -> Process:
        observed_commands.append(command)
        return Process(command)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    factory = HermesCliFactory(
        settings=HermesCliSettings(
            executable="python.exe",
            module_root=module_root,
            evidence_root=tmp_path / "evidence",
            provider="openai-api",
            model="gpt-4.1-mini",
            maximum_total_cost_usd=Decimal("0.05"),
        )
    )

    assert await factory._run_skill_prompt(
        "first",
        max_seconds=30,
        effect_identity="1" * 64,
    ) == b"{}"
    restarted = HermesCliFactory(
        settings=HermesCliSettings(
            executable="python.exe",
            module_root=module_root,
            evidence_root=tmp_path / "evidence",
            provider="openai-api",
            model="gpt-4.1-mini",
            maximum_total_cost_usd=Decimal("0.05"),
        )
    )
    assert restarted.observed_cost_usd == Decimal("0.03")
    with pytest.raises(FactoryDispatchError, match="cost ceiling"):
        await restarted._run_skill_prompt(
            "second",
            max_seconds=30,
            effect_identity="2" * 64,
        )

    assert restarted.observed_cost_usd == Decimal("0.06")
    assert len(observed_commands) == 2
    assert observed_commands[0][3:7] == (
        "--provider",
        "openai-api",
        "-m",
        "gpt-4.1-mini",
    )
    assert "--usage-file" in observed_commands[0]
    effect_root = tmp_path / "evidence" / "provider-effects"
    first_effect = json.loads(
        (effect_root / "effects" / f"{'1' * 64}.json").read_text(
            encoding="utf-8"
        )
    )
    second_effect = json.loads(
        (effect_root / "effects" / f"{'2' * 64}.json").read_text(
            encoding="utf-8"
        )
    )
    assert first_effect["estimated_cost_usd"] == "0.03"
    assert first_effect["usage_ref"]["sha256"] == first_effect["usage_sha256"]
    assert second_effect["estimated_cost_usd"] == "0.03"
    assert first_effect["stdout_ref"]["sha256"] == hashlib.sha256(b"{}").hexdigest()
    assert second_effect["cost_ceiling_exceeded"] is True


@pytest.mark.asyncio
async def test_invalid_usage_is_preserved_before_the_call_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_root = tmp_path / "hermes-agent"
    entrypoint = module_root / "hermes_cli" / "main.py"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("# checkout entrypoint\n", encoding="utf-8")
    invalid_usage = b'{"bad":"usage"}'

    class Process:
        returncode = 0

        def __init__(self, command: tuple[str, ...]) -> None:
            self.command = command

        async def communicate(self) -> tuple[bytes, bytes]:
            usage_path = Path(self.command[self.command.index("--usage-file") + 1])
            usage_path.write_bytes(invalid_usage)
            return b'{"partial":true}', b""

    async def create_process(*command: str, **_: object) -> Process:
        return Process(command)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    effect_identity = "3" * 64
    factory = HermesCliFactory(
        settings=HermesCliSettings(
            executable="python.exe",
            module_root=module_root,
            evidence_root=tmp_path / "evidence",
            provider="openai-api",
            model="gpt-4.1-mini",
            maximum_total_cost_usd=Decimal("0.10"),
        )
    )

    with pytest.raises(FactoryDispatchError, match="usage evidence"):
        await factory._run_skill_prompt(
            "prompt",
            max_seconds=30,
            effect_identity=effect_identity,
        )

    effect = json.loads(
        (
            tmp_path
            / "evidence"
            / "provider-effects"
            / "effects"
            / f"{effect_identity}.json"
        ).read_text(encoding="utf-8")
    )
    assert effect["usage_sha256"] == hashlib.sha256(invalid_usage).hexdigest()
    assert effect["usage_ref"]["sha256"] == effect["usage_sha256"]
    assert effect["estimated_cost_usd"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["missing", "missing_entrypoint", "file"])
async def test_invalid_module_root_fails_closed_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
) -> None:
    module_root = tmp_path / "hermes-agent"
    if layout == "missing_entrypoint":
        module_root.mkdir()
    elif layout == "file":
        module_root.write_text("not a checkout\n", encoding="utf-8")
    spawned = False

    async def create_process(*_: str, **__: object) -> object:
        nonlocal spawned
        spawned = True
        raise AssertionError("invalid Hermes checkout must not be spawned")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="Hermes module root"):
        await HermesCliFactory(
            settings=HermesCliSettings(
                executable="python.exe",
                module_root=module_root,
                evidence_root=tmp_path / "evidence",
            )
        )._run_skill_prompt("sealed prompt", max_seconds=30)

    assert spawned is False


def _architect_dispatch() -> tuple[FactoryDispatch, object]:
    factory_job = job()
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )
    return (
        FactoryDispatch(
            job=factory_job,
            action=FactoryAction(
                kind=FactoryActionKind.DISPATCH_AGENT_ARCHITECT,
                attempt=1,
                job_id=factory_job.job_id,
            ),
            role=FactoryRole.AGENT_ARCHITECT,
            lease=lease,
        ),
        lease,
    )


def _improvement_authorization() -> FactoryImprovementAuthorizationV1:
    evaluation_data = evaluation_payload(
        failure_class="behavioral_failure",
        recommendation="RETRY_BUILD",
        prior_green_regression_ids=["schema_valid"],
    )
    assertion_outcomes = evaluation_data["assertion_outcomes"]
    assert isinstance(assertion_outcomes, list)
    second_outcome = assertion_outcomes[1]
    assert isinstance(second_outcome, dict)
    second_outcome["status"] = "failed"
    evaluation = TeamEvaluationV1.model_validate(evaluation_data)
    prior_candidate = ArtifactRef.model_validate(
        revision_payload()["parent_candidate_ref"]
    )
    request_data = block(FactoryPhase.IMPROVEMENT_REQUESTED).model_dump(
        mode="json",
        by_alias=True,
    )
    request_data.update(
        {
            "job_id": str(evaluation.job_id),
            "correlation_id": str(evaluation.correlation_id),
            "subject_version": evaluation.subject_version,
            "attempt": evaluation.attempt,
            "occurred_at": evaluation.occurred_at.isoformat(),
            "artifact_refs": [prior_candidate.model_dump(mode="json")],
            "evidence_refs": [evaluation.artifact_ref.model_dump(mode="json")],
        }
    )
    return FactoryImprovementAuthorizationV1(
        schema_name="captain.factory-improvement-authorization.v1",
        authorization_ref=ArtifactRef(
            uri="artifact://factory/improvement-request",
            sha256="8" * 64,
            media_type="application/json",
        ),
        authorized_attempt=2,
        request_block=FactoryEvidenceBlock.model_validate(request_data),
        failed_evaluation=evaluation,
        prior_candidate_ref=prior_candidate,
        prior_green_assertion_ids=("schema_valid",),
        prior_green_benchmark_metric_ids=("coverage",),
    )


def test_improvement_artifact_binds_benchmark_regression_guards() -> None:
    authorization = _improvement_authorization()
    revision = CandidateRevisionV1.model_validate(revision_payload()).model_copy(
        update={"regression_benchmark_metric_ids": ()}
    )

    with pytest.raises(FactoryDispatchError, match="authorized failed candidate"):
        _require_improvement_artifact_binding(
            revision,
            authorization=authorization,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("failed_benchmark_metric_ids", ()),
        ("regression_benchmark_metric_ids", ()),
    ),
)
def test_codex_brief_typed_benchmark_guards_match_authorization(
    field: str,
    value: tuple[str, ...],
) -> None:
    authorization = _improvement_authorization()
    brief = CodexBuildBriefV1.model_validate(brief_payload()).model_copy(
        update={field: value}
    )

    with pytest.raises(FactoryDispatchError, match="benchmark guards"):
        _require_improvement_artifact_binding(
            brief,
            authorization=authorization,
        )


@pytest.mark.asyncio
async def test_dispatch_rejects_changed_released_skill_bytes_before_hermes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, lease = _architect_dispatch()
    catalog = _catalog_for(tmp_path, FactorySkillStep.DISCOVER)
    (tmp_path / "captain-factory-discover" / "SKILL.md").write_text(
        "# changed\n",
        encoding="utf-8",
    )

    async def create_process(*_: str, **__: object) -> object:
        raise AssertionError("Hermes must not run after a digest mismatch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="digest"):
        await HermesCliFactory(
            settings=HermesCliSettings(
                skill_root=tmp_path,
                evidence_root=tmp_path / "evidence",
                working_directory=_CAPTAIN_WORKSPACE_ROOT,
            ),
            released_skill_catalog=catalog,
            clock=lambda: lease.issued_at,
        ).dispatch(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_uri",
    [
        "artifact://other-root/captain-factory-discover/v1",
        "artifact://released-skills/captain-factory-discover/v2",
    ],
)
async def test_dispatch_rejects_release_metadata_outside_exact_skill_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content_uri: str,
) -> None:
    request, lease = _architect_dispatch()
    catalog = _catalog_for(tmp_path, FactorySkillStep.DISCOVER)
    released = catalog.releases[FactorySkillStep.DISCOVER]
    released_data = released.model_dump(mode="json", by_alias=True)
    released_data["content_ref"] = {
        **released_data["content_ref"],
        "uri": content_uri,
    }
    catalog.releases[FactorySkillStep.DISCOVER] = ReleasedHermesSkill.model_validate(
        released_data
    )

    async def create_process(*_: str, **__: object) -> object:
        raise AssertionError("Hermes must not run for inconsistent release metadata")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="metadata"):
        await HermesCliFactory(
            settings=HermesCliSettings(
                skill_root=tmp_path,
                evidence_root=tmp_path / "evidence",
            ),
            released_skill_catalog=catalog,
            clock=lambda: lease.issued_at,
        ).dispatch(request)


@pytest.mark.asyncio
async def test_dispatch_rejects_result_for_the_wrong_skill_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, lease = _architect_dispatch()
    catalog = _catalog_for(tmp_path, FactorySkillStep.DISCOVER)

    class Process:
        returncode = 0

        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        async def communicate(self) -> tuple[bytes, bytes]:
            return json.dumps(
                _typed_payload(self.prompt, step=FactorySkillStep.REPORT_CAPTAIN)
            ).encode(), b""

    async def create_process(*command: str, **__: object) -> Process:
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="typed.*discover"):
        await HermesCliFactory(
            settings=HermesCliSettings(
                skill_root=tmp_path,
                evidence_root=tmp_path / "evidence",
                working_directory=_CAPTAIN_WORKSPACE_ROOT,
            ),
            released_skill_catalog=catalog,
            clock=lambda: lease.issued_at,
        ).dispatch(request)


@pytest.mark.asyncio
async def test_dispatch_rejects_expired_lease_before_hermes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, lease = _architect_dispatch()
    catalog = _catalog_for(tmp_path, FactorySkillStep.DISCOVER)

    async def create_process(*_: str, **__: object) -> object:
        raise AssertionError("Hermes must not run with an expired lease")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="active lease"):
        await HermesCliFactory(
            settings=HermesCliSettings(
                skill_root=tmp_path,
                evidence_root=tmp_path / "evidence",
            ),
            released_skill_catalog=catalog,
            clock=lambda: lease.expires_at,
        ).dispatch(request)


@pytest.mark.asyncio
async def test_dispatch_rejects_action_that_does_not_match_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, lease = _architect_dispatch()
    mismatched = FactoryDispatch(
        job=request.job,
        action=request.action.model_copy(
            update={"kind": FactoryActionKind.DISPATCH_QUALITY_WARDEN}
        ),
        role=request.role,
        lease=request.lease,
    )
    catalog = _catalog_for(tmp_path, FactorySkillStep.DISCOVER)

    async def create_process(*_: str, **__: object) -> object:
        raise AssertionError("Hermes must not run for a mismatched action")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="action.*role"):
        await HermesCliFactory(
            settings=HermesCliSettings(
                skill_root=tmp_path,
                evidence_root=tmp_path / "evidence",
            ),
            released_skill_catalog=catalog,
            clock=lambda: lease.issued_at,
        ).dispatch(mismatched)


@pytest.mark.asyncio
async def test_dispatch_timeout_terminates_hermes_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, lease = _architect_dispatch()
    catalog = _catalog_for(tmp_path, FactorySkillStep.DISCOVER)
    terminated: list[object] = []

    class Process:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Future()
            raise AssertionError("unreachable")

    process = Process()

    async def create_process(*_: str, **__: object) -> Process:
        return process

    async def terminate(candidate: object, *, executable: str) -> None:
        assert executable == "hermes"
        terminated.append(candidate)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        "agenten.agent_factory.hermes_cli._terminate_async_process_tree",
        terminate,
    )

    with pytest.raises(FactoryDispatchError, match="timed out"):
        await HermesCliFactory(
                settings=HermesCliSettings(
                    skill_root=tmp_path,
                    timeout_seconds=0.2,
                    evidence_root=tmp_path / "evidence",
                    working_directory=_CAPTAIN_WORKSPACE_ROOT,
                ),
            released_skill_catalog=catalog,
            clock=lambda: lease.issued_at,
        ).dispatch(request)

    assert terminated == [process]


@pytest.mark.asyncio
async def test_quality_sequence_reports_unresolved_evaluation_to_captain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_job = job()
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.QUALITY_WARDEN,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )
    request = FactoryDispatch(
        job=factory_job,
        action=FactoryAction(
            kind=FactoryActionKind.DISPATCH_QUALITY_WARDEN,
            attempt=1,
            job_id=factory_job.job_id,
        ),
        role=FactoryRole.QUALITY_WARDEN,
        lease=lease,
    )
    catalog = _catalog_for(
        tmp_path,
        FactorySkillStep.EVALUATE_TEAM,
        FactorySkillStep.REPORT_CAPTAIN,
    )
    invocations: list[dict[str, object]] = []

    class Process:
        returncode = 0

        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        async def communicate(self) -> tuple[bytes, bytes]:
            invocation = _invocation_from_prompt(self.prompt)
            invocations.append(invocation)
            payload = _typed_payload(self.prompt)
            if invocation["step"] == "evaluate_team":
                payload.update(
                    {
                        "failure_class": "unresolved",
                        "recommendation": "MANUAL_DECISION_REQUIRED",
                    }
                )
            else:
                payload.update(
                    {
                        "recommendation": "MANUAL_DECISION_REQUIRED",
                        "reason_codes": ["evaluation_unresolved"],
                    }
                )
            return json.dumps(payload).encode(), b""

    async def create_process(*command: str, **__: object) -> Process:
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    evidence = await HermesCliFactory(
        settings=HermesCliSettings(
            skill_root=tmp_path,
            evidence_root=tmp_path / "evidence",
        ),
        released_skill_catalog=catalog,
        clock=lambda: lease.issued_at,
    ).dispatch(request)

    assert [item["step"] for item in invocations] == [
        "evaluate_team",
        "report_captain",
    ]
    assert catalog.calls == [
        FactorySkillStep.EVALUATE_TEAM,
        FactorySkillStep.REPORT_CAPTAIN,
    ]
    assert evidence.phase is FactoryPhase.QUALITY_REVIEWED
    assert evidence.status.value == "recommended"


@pytest.mark.asyncio
async def test_quality_sequence_runs_evaluate_then_report_under_same_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_job = job()
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.QUALITY_WARDEN,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )
    request = FactoryDispatch(
        job=factory_job,
        action=FactoryAction(
            kind=FactoryActionKind.DISPATCH_QUALITY_WARDEN,
            attempt=1,
            job_id=factory_job.job_id,
        ),
        role=FactoryRole.QUALITY_WARDEN,
        lease=lease,
    )
    catalog = _catalog_for(
        tmp_path,
        FactorySkillStep.EVALUATE_TEAM,
        FactorySkillStep.REPORT_CAPTAIN,
    )
    invocations: list[dict[str, object]] = []

    class Process:
        returncode = 0

        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        async def communicate(self) -> tuple[bytes, bytes]:
            invocations.append(_invocation_from_prompt(self.prompt))
            return json.dumps(_typed_payload(self.prompt)).encode(), b""

    async def create_process(*command: str, **__: object) -> Process:
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    evidence = await HermesCliFactory(
        settings=HermesCliSettings(
            skill_root=tmp_path,
            evidence_root=tmp_path / "evidence",
        ),
        released_skill_catalog=catalog,
        clock=lambda: lease.issued_at,
    ).dispatch(request)

    assert [item["step"] for item in invocations] == [
        "evaluate_team",
        "report_captain",
    ]
    assert invocations[0]["lease"] == invocations[1]["lease"]
    assert invocations[1]["input_ref"] == evaluation_payload()["artifact_ref"]
    assert evidence.phase is FactoryPhase.QUALITY_REVIEWED
    assert evidence.status.value == "succeeded"
    assert len(evidence.artifact_refs) == 2


@pytest.mark.asyncio
async def test_retry_sequence_requires_captain_improvement_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_job = job()
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=2,
        workspace_ref="workspace://factory/support-triage",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )
    request = FactoryDispatch(
        job=factory_job,
        action=FactoryAction(
            kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
            attempt=2,
            job_id=factory_job.job_id,
        ),
        role=FactoryRole.TOOL_INTEGRATOR,
        lease=lease,
    )
    catalog = _catalog_for(
        tmp_path,
        FactorySkillStep.IMPROVE_TEAM,
        FactorySkillStep.BRIEF_CODEX,
        FactorySkillStep.SEAL_CODEX_BUILD,
    )
    async def create_process(*_: str, **__: object) -> object:
        raise AssertionError("Hermes must not run without Captain retry authority")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="IMPROVEMENT_REQUESTED"):
        await HermesCliFactory(
            settings=HermesCliSettings(
                skill_root=tmp_path,
                evidence_root=tmp_path / "evidence",
            ),
            released_skill_catalog=catalog,
            clock=lambda: lease.issued_at,
        ).dispatch(request)


@pytest.mark.asyncio
async def test_authorized_retry_runs_improve_before_brief_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _improvement_authorization()
    factory_job = job_v3(mode="demo").model_copy(
        update={
            "job_id": authorization.request_block.job_id,
            "correlation_id": authorization.request_block.correlation_id,
            "subject_version": authorization.request_block.subject_version,
        }
    )
    architect_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/support-triage/discovery",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=2,
        workspace_ref="workspace://factory/support-triage",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )
    request = FactoryDispatch(
        job=factory_job,
        action=FactoryAction(
            kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
            attempt=2,
            job_id=factory_job.job_id,
        ),
        role=FactoryRole.TOOL_INTEGRATOR,
        lease=lease,
        improvement_authorization=authorization,
    )
    catalog = _catalog_for(
        tmp_path,
        FactorySkillStep.DISCOVER,
        FactorySkillStep.IMPROVE_TEAM,
        FactorySkillStep.BRIEF_CODEX,
        FactorySkillStep.SEAL_CODEX_BUILD,
    )
    sealer = CaptainBuildSealer()
    replay_store = InMemoryFactorySkillReplayStore()
    invocations: list[dict[str, object]] = []
    commands: list[tuple[str, ...]] = []

    class PromptStore:
        def persist(self, job_id: UUID, content: bytes) -> ArtifactRef:
            digest = hashlib.sha256(content).hexdigest()
            return ArtifactRef(
                uri=f"artifact://factory-prompts/{job_id}/{digest}",
                sha256=digest,
                media_type="application/json",
            )

    class Process:
        returncode = 0

        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        async def communicate(self) -> tuple[bytes, bytes]:
            invocations.append(_invocation_from_prompt(self.prompt))
            if "captain_improvement_seed_sha256=" in self.prompt:
                digest_prefix = "captain_improvement_seed_sha256="
                digest = next(
                    line.removeprefix(digest_prefix)
                    for line in self.prompt.splitlines()
                    if line.startswith(digest_prefix)
                )
                return json.dumps(
                    {
                        "schema": "hermes.factory-improvement-attestation.v1",
                        "invocation_id": invocations[-1]["invocation_id"],
                        "seed_sha256": digest,
                        "changed_components": ["system_prompt", "tests"],
                        "accepted": True,
                    }
                ).encode(), b""
            if "captain_codex_brief_seed_sha256=" in self.prompt:
                digest_prefix = "captain_codex_brief_seed_sha256="
                digest = next(
                    line.removeprefix(digest_prefix)
                    for line in self.prompt.splitlines()
                    if line.startswith(digest_prefix)
                )
                return json.dumps(
                    {
                        "schema": "hermes.factory-codex-brief-attestation.v1",
                        "invocation_id": invocations[-1]["invocation_id"],
                        "seed_sha256": digest,
                        "accepted": True,
                    }
                ).encode(), b""
            return json.dumps(_typed_payload(self.prompt)).encode(), b""

    async def create_process(*command: str, **__: object) -> Process:
        commands.append(command)
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    factory = HermesCliFactory(
        settings=HermesCliSettings(
            skill_root=tmp_path,
            working_directory=_CAPTAIN_WORKSPACE_ROOT,
            evidence_root=tmp_path / "evidence",
        ),
        released_skill_catalog=catalog,
        replay_store=replay_store,
        codex_build_sealer=sealer,
        codex_prompt_artifact_store=PromptStore(),
        clock=lambda: lease.issued_at,
    )
    await factory.dispatch(
        FactoryDispatch(
            job=factory_job,
            action=FactoryAction(
                kind=FactoryActionKind.DISPATCH_AGENT_ARCHITECT,
                attempt=1,
                job_id=factory_job.job_id,
            ),
            role=FactoryRole.AGENT_ARCHITECT,
            lease=architect_lease,
        )
    )
    evidence = await factory.dispatch(request)

    assert [item["step"] for item in invocations] == [
        "discover",
        "improve_team",
        "brief_codex",
    ]
    assert invocations[1]["input_ref"] == authorization.authorization_ref.model_dump(
        mode="json"
    )
    assert invocations[2]["input_ref"] != authorization.prior_candidate_ref.model_dump(
        mode="json"
    )
    improve_prompt = commands[1][-1]
    assert "captain_improvement_seed=" in improve_prompt
    assert authorization.prior_candidate_ref.uri in improve_prompt
    assert authorization.failed_evaluation.artifact_ref.uri in improve_prompt
    assert "Return exactly one hermes.factory-improvement-attestation.v1" in improve_prompt
    assert "business_value are diagnostic inputs and are never changed_components" in improve_prompt
    assert "--no-tools" in commands[0]
    assert "--no-tools" in commands[1]
    assert "--no-tools" in commands[2]
    assert len(sealer.calls) == 1
    assert sealer.calls[0][1].step is FactorySkillStep.SEAL_CODEX_BUILD
    assert sealer.calls[0][1].input_ref == sealer.calls[0][2].artifact_ref
    assert evidence.phase is FactoryPhase.TOOL_CANDIDATE_TESTED
    assert len(evidence.artifact_refs) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_schema",
    ["captain.factory-skill-replay.v2", "captain.factory-skill-replay.v3"],
)
async def test_dispatch_replay_uses_identical_invocation_and_idempotency_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_schema: str,
) -> None:
    request, lease = _architect_dispatch()
    catalog = _catalog_for(tmp_path, FactorySkillStep.DISCOVER)
    invocations: list[dict[str, object]] = []

    class Process:
        returncode = 0

        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        async def communicate(self) -> tuple[bytes, bytes]:
            invocation = _invocation_from_prompt(self.prompt)
            invocations.append(invocation)
            return json.dumps(_typed_payload(self.prompt)).encode(), b""

    async def create_process(*command: str, **__: object) -> Process:
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    settings = HermesCliSettings(
        skill_root=tmp_path,
        evidence_root=tmp_path / "evidence",
        working_directory=_CAPTAIN_WORKSPACE_ROOT,
    )
    replay_store = FilesystemFactorySkillReplayStore(tmp_path / "replays")
    first_factory = HermesCliFactory(
        settings=settings,
        released_skill_catalog=catalog,
        replay_store=replay_store,
        clock=lambda: lease.issued_at,
    )

    first = await first_factory.dispatch(request)
    second = await HermesCliFactory(
        settings=settings,
        released_skill_catalog=catalog,
        replay_store=FilesystemFactorySkillReplayStore(tmp_path / "replays"),
        clock=lambda: lease.issued_at,
    ).dispatch(request)

    assert len(invocations) == 1
    assert first == second

    replay_path = next((tmp_path / "replays").glob("*.json"))
    legacy_payload = json.loads(replay_path.read_text(encoding="utf-8"))
    legacy_payload["schema"] = legacy_schema
    legacy_payload.pop("runtime_retry_authorization_ref")
    legacy_payload.pop("runtime_retry_authorization_binding_sha256")
    if legacy_schema.endswith(".v2"):
        legacy_payload.pop("checkpoint_ref")
        legacy_payload.pop("terminal_receipt_ref")
        legacy_payload.pop("resume_ordinal")
    replay_path.write_text(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    prior = await FilesystemFactorySkillReplayStore(
        tmp_path / "replays"
    ).completed(
        request.job,
        step=FactorySkillStep.DISCOVER,
        attempt=1,
    )
    assert isinstance(prior.artifact, CodebaseInventoryV1)

    invocation = FactorySkillInvocationV1.model_validate(invocations[0])
    accepted = await replay_store.claim(invocation)
    assert accepted.acquired is False
    assert accepted.record.state == "completed"
    assert accepted.record.artifact is not None
    with pytest.raises(FactoryDispatchError, match="no longer pending"):
        await replay_store.complete(
            accepted.record,
            artifact=accepted.record.artifact,
            transcript_ref=ArtifactRef(
                uri="artifact://factory-evidence/conflicting",
                sha256="f" * 64,
                media_type="application/json",
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize(
    "retryable_failure",
    ["CodexPolicyViolation", "CodexBuildProvenanceError"],
)
async def test_runtime_retry_replay_requires_atomic_authorized_resume(
    tmp_path: Path,
    durable: bool,
    retryable_failure: str,
) -> None:
    replay_store = (
        FilesystemFactorySkillReplayStore(tmp_path / "runtime-replays")
        if durable
        else InMemoryFactorySkillReplayStore()
    )
    invocation_data = seal_invocation_payload()
    invocation = FactorySkillInvocationV1.model_validate(invocation_data)
    checkpoint_ref = ArtifactRef(
        uri=f"artifact://factory/codex-checkpoint/{'c' * 64}",
        sha256="c" * 64,
        media_type="application/json",
    )
    receipt_ref = ArtifactRef(
        uri=f"artifact://factory/codex-terminal-receipt/{'d' * 64}",
        sha256="d" * 64,
        media_type="application/json",
    )
    claimed = await replay_store.claim(invocation)

    interrupted = await replay_store.interrupt(
        claimed.record,
        checkpoint_ref=checkpoint_ref,
        terminal_receipt_ref=receipt_ref,
        resume_ordinal=0,
    )

    assert interrupted.state == "interrupted"
    assert interrupted.failure_kind == "codex_runtime_interrupted"
    assert interrupted.checkpoint_ref == checkpoint_ref
    assert interrupted.terminal_receipt_ref == receipt_ref
    with pytest.raises(FactoryDispatchError, match="interrupted"):
        await replay_store.claim(invocation)

    authorization = FactoryRuntimeRetryAuthorizationV1(
        schema_name="captain.factory-runtime-retry-authorization.v1",
        authorization_ref=ArtifactRef(
            uri=f"artifact://factory/runtime-retry/{'a' * 64}",
            sha256="a" * 64,
            media_type="application/json",
        ),
        producer="captain",
        status="succeeded",
        job_id=invocation.job_id,
        correlation_id=invocation.correlation_id,
        subject_version=invocation.subject_version,
        attempt=invocation.attempt,
        invocation_id=invocation.invocation_id,
        idempotency_key=invocation.idempotency_key,
        lease_id=invocation.lease.lease_id,
        checkpoint_ref=checkpoint_ref,
        terminal_receipt_ref=receipt_ref,
        workspace_ref=invocation.lease.workspace_ref,
        base_revision="e" * 40,
        scaffold_manifest_sha256="f" * 64,
        brief_sha256="1" * 64,
        resume_ordinal=1,
        maximum_runtime_seconds=60,
        issued_at=invocation.lease.issued_at,
        expires_at=invocation.lease.issued_at + timedelta(minutes=1),
    )
    if durable:
        competing_store = FilesystemFactorySkillReplayStore(
            tmp_path / "runtime-replays"
        )
        outcomes = await asyncio.gather(
            replay_store.resume(interrupted, authorization=authorization),
            competing_store.resume(interrupted, authorization=authorization),
            return_exceptions=True,
        )
        acquired = [
            item for item in outcomes if isinstance(item, FactorySkillReplayClaim)
        ]
        rejected = [item for item in outcomes if isinstance(item, FactoryDispatchError)]
        assert len(acquired) == 1
        assert len(rejected) == 1
        resumed = acquired[0]
    else:
        resumed = await replay_store.resume(
            interrupted,
            authorization=authorization,
        )

    assert resumed.acquired is True
    assert resumed.record.state == "pending"
    assert resumed.record.resume_ordinal == 1
    assert resumed.record.runtime_retry_authorization_ref == (
        authorization.authorization_ref
    )
    assert resumed.record.runtime_retry_authorization_binding_sha256 == (
        hashlib.sha256(
            json.dumps(
                authorization.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    assert resumed.record.claim_token != interrupted.claim_token
    with pytest.raises(FactoryDispatchError, match="interrupted"):
        await replay_store.resume(interrupted, authorization=authorization)

    failed = await replay_store.fail(
        resumed.record,
        failure_kind=retryable_failure,
    )
    with pytest.raises(FactorySkillReplayRetryableFailureError) as retryable:
        await replay_store.claim(invocation)
    retried = await replay_store.retry_failed(
        retryable.value.record,
        authorization=authorization,
    )
    assert retried.acquired is True
    assert retried.record.state == "pending"
    assert retried.record.resume_ordinal == 1
    assert retried.record.runtime_retry_authorization_ref == (
        authorization.authorization_ref
    )
    with pytest.raises(FactoryDispatchError, match="failure changed"):
        await replay_store.retry_failed(failed, authorization=authorization)


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_evidence_failure_replay_requires_new_runtime_retry_authority(
    tmp_path: Path,
    durable: bool,
) -> None:
    replay_store = (
        FilesystemFactorySkillReplayStore(tmp_path / "runtime-replays")
        if durable
        else InMemoryFactorySkillReplayStore()
    )
    invocation = FactorySkillInvocationV1.model_validate(seal_invocation_payload())
    claimed = await replay_store.claim(invocation)
    failed = await replay_store.fail(
        claimed.record,
        failure_kind="FactoryCodexEvidenceFailure",
    )
    with pytest.raises(FactorySkillReplayRetryableFailureError) as retryable:
        await replay_store.claim(invocation)
    authorization = FactoryRuntimeRetryAuthorizationV1(
        schema_name="captain.factory-runtime-retry-authorization.v1",
        authorization_ref=ArtifactRef(
            uri=f"artifact://factory/runtime-retry/{'a' * 64}",
            sha256="a" * 64,
            media_type="application/json",
        ),
        producer="captain",
        status="succeeded",
        job_id=invocation.job_id,
        correlation_id=invocation.correlation_id,
        subject_version=invocation.subject_version,
        attempt=invocation.attempt,
        invocation_id=invocation.invocation_id,
        idempotency_key=invocation.idempotency_key,
        lease_id=invocation.lease.lease_id,
        checkpoint_ref=ArtifactRef(
            uri=f"artifact://factory/codex-checkpoint/{'c' * 64}",
            sha256="c" * 64,
            media_type="application/json",
        ),
        terminal_receipt_ref=ArtifactRef(
            uri=f"artifact://factory/codex-terminal-receipt/{'d' * 64}",
            sha256="d" * 64,
            media_type="application/json",
        ),
        workspace_ref=invocation.lease.workspace_ref,
        base_revision="e" * 40,
        scaffold_manifest_sha256="f" * 64,
        brief_sha256="1" * 64,
        resume_ordinal=1,
        maximum_runtime_seconds=60,
        issued_at=invocation.lease.issued_at,
        expires_at=invocation.lease.issued_at + timedelta(minutes=1),
    )

    retried = await replay_store.retry_failed(
        retryable.value.record,
        authorization=authorization,
    )

    assert retried.record.state == "pending"
    assert retried.record.resume_ordinal == 1
    assert retried.record.runtime_retry_authorization_ref == (
        authorization.authorization_ref
    )
    with pytest.raises(FactoryDispatchError, match="failure changed"):
        await replay_store.retry_failed(failed, authorization=authorization)


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize(
    "step",
    ["improve_team", "brief_codex"],
)
async def test_failed_improve_replay_requires_exact_budget_bound_captain_retry(
    tmp_path: Path,
    durable: bool,
    step: str,
) -> None:
    replay_root = tmp_path / "hermes-replays"
    replay_store = (
        FilesystemFactorySkillReplayStore(replay_root)
        if durable
        else InMemoryFactorySkillReplayStore()
    )
    payload = invocation_payload(step, attempt=2)
    lease_payload = payload["lease"]
    assert isinstance(lease_payload, dict)
    lease_payload["attempt"] = 2
    lease_issued_at = lease_payload["issued_at"]
    assert isinstance(lease_issued_at, datetime)
    lease_payload["workspace_ref"] = (
        "workspace://factory/improve/2/"
        + lease_issued_at.strftime("%Y%m%dT%H%M%S%fZ")
    )
    invocation = FactorySkillInvocationV1.model_validate(payload)
    claimed = await replay_store.claim(invocation)
    failed = await replay_store.fail(
        claimed.record,
        failure_kind="FactoryDispatchError",
    )
    with pytest.raises(FactorySkillReplayHermesRetryableFailureError):
        await replay_store.claim(invocation)
    successor_lease = invocation.lease.model_copy(
        update={
            "lease_id": "factory-successor-retry",
            "issued_at": invocation.lease.expires_at,
            "expires_at": invocation.lease.expires_at + timedelta(minutes=15),
            "workspace_ref": (
                invocation.lease.workspace_ref.rsplit("/", 1)[0]
                + "/"
                + invocation.lease.expires_at.strftime("%Y%m%dT%H%M%S%fZ")
            ),
        }
    )
    successor_invocation = invocation.model_copy(
        update={"lease": successor_lease}
    )
    with pytest.raises(FactorySkillReplayHermesRetryableFailureError):
        await replay_store.claim(successor_invocation)
    failure_ref = factory_skill_replay_failure_ref(failed)
    with pytest.raises(ValueError, match="user team cap"):
        build_factory_hermes_replay_retry_authorization(
            job_id=invocation.job_id,
            correlation_id=invocation.correlation_id,
            subject_version=invocation.subject_version,
            attempt=invocation.attempt,
            invocation_id=invocation.invocation_id,
            idempotency_key=invocation.idempotency_key,
            lease_id=invocation.lease.lease_id,
            step=invocation.step,
            failed_replay_ref=failure_ref,
            issued_at=invocation.lease.issued_at,
            expires_at=invocation.lease.issued_at + timedelta(minutes=5),
            user_total_cap_eur=Decimal("1.01"),
        )
    authorization = build_factory_hermes_replay_retry_authorization(
        job_id=invocation.job_id,
        correlation_id=invocation.correlation_id,
        subject_version=invocation.subject_version,
        attempt=invocation.attempt,
        invocation_id=invocation.invocation_id,
        idempotency_key=invocation.idempotency_key,
        lease_id=invocation.lease.lease_id,
        step=invocation.step,
        failed_replay_ref=failure_ref,
        issued_at=invocation.lease.issued_at,
        expires_at=invocation.lease.issued_at + timedelta(minutes=5),
    )

    retried = await replay_store.retry_failed_hermes(
        failed,
        requested_invocation=successor_invocation,
        authorization=authorization,
    )

    assert retried.acquired is True
    assert retried.record.state == "pending"
    assert retried.record.invocation.lease == successor_lease
    assert retried.record.resume_ordinal == 1
    assert retried.record.prior_failure_ref == failure_ref
    assert retried.record.hermes_retry_authorization_ref == (
        authorization.authorization_ref
    )
    if durable:
        archive = replay_root / "failure-history" / f"{failure_ref.sha256}.json"
        assert archive.is_file()
        assert factory_skill_replay_failure_ref(
            FilesystemFactorySkillReplayStore._read_record(archive)
        ) == failure_ref
    failed_again = await replay_store.fail(
        retried.record,
        failure_kind="FactoryDispatchError",
    )
    with pytest.raises(FactorySkillReplayHermesRetryableFailureError):
        await replay_store.claim(successor_invocation)
    failed_again_ref = factory_skill_replay_failure_ref(failed_again)
    second_authorization = build_factory_hermes_replay_retry_authorization(
        job_id=successor_invocation.job_id,
        correlation_id=successor_invocation.correlation_id,
        subject_version=successor_invocation.subject_version,
        attempt=successor_invocation.attempt,
        invocation_id=successor_invocation.invocation_id,
        idempotency_key=successor_invocation.idempotency_key,
        lease_id=successor_invocation.lease.lease_id,
        step=successor_invocation.step,
        failed_replay_ref=failed_again_ref,
        retry_ordinal=2,
        issued_at=successor_invocation.lease.issued_at,
        expires_at=successor_invocation.lease.issued_at + timedelta(minutes=5),
    )
    retried_again = await replay_store.retry_failed_hermes(
        failed_again,
        requested_invocation=successor_invocation,
        authorization=second_authorization,
    )

    assert retried_again.acquired is True
    assert retried_again.record.resume_ordinal == 2
    assert retried_again.record.prior_failure_ref == failed_again_ref
    failed_third = await replay_store.fail(
        retried_again.record,
        failure_kind="FactoryDispatchError",
    )
    failed_third_ref = factory_skill_replay_failure_ref(failed_third)
    third_authorization = build_factory_hermes_replay_retry_authorization(
        job_id=successor_invocation.job_id,
        correlation_id=successor_invocation.correlation_id,
        subject_version=successor_invocation.subject_version,
        attempt=successor_invocation.attempt,
        invocation_id=successor_invocation.invocation_id,
        idempotency_key=successor_invocation.idempotency_key,
        lease_id=successor_invocation.lease.lease_id,
        step=successor_invocation.step,
        failed_replay_ref=failed_third_ref,
        retry_ordinal=3,
        issued_at=successor_invocation.lease.issued_at,
        expires_at=successor_invocation.lease.issued_at + timedelta(minutes=5),
    )
    retried_third = await replay_store.retry_failed_hermes(
        failed_third,
        requested_invocation=successor_invocation,
        authorization=third_authorization,
    )

    assert retried_third.acquired is True
    assert retried_third.record.resume_ordinal == 3
    assert retried_third.record.prior_failure_ref == failed_third_ref
    with pytest.raises(FactoryDispatchError, match="failure changed"):
        await replay_store.retry_failed_hermes(
            failed,
            requested_invocation=successor_invocation,
            authorization=authorization,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_failed_execute_team_evidence_replay_is_captain_retryable(
    tmp_path: Path,
    durable: bool,
) -> None:
    replay_store = (
        FilesystemFactorySkillReplayStore(tmp_path / "team-replays")
        if durable
        else InMemoryFactorySkillReplayStore()
    )
    payload = invocation_payload("execute_team", attempt=3)
    lease_payload = payload["lease"]
    assert isinstance(lease_payload, dict)
    lease_payload["attempt"] = 3
    invocation = FactorySkillInvocationV1.model_validate(payload)
    claimed = await replay_store.claim(invocation)
    failed = await replay_store.fail(
        claimed.record,
        failure_kind="evidence_binding_failed",
    )

    with pytest.raises(FactorySkillReplayHermesRetryableFailureError):
        await replay_store.claim(invocation)

    authorization = build_factory_hermes_replay_retry_authorization(
        job_id=invocation.job_id,
        correlation_id=invocation.correlation_id,
        subject_version=invocation.subject_version,
        attempt=invocation.attempt,
        invocation_id=invocation.invocation_id,
        idempotency_key=invocation.idempotency_key,
        lease_id=invocation.lease.lease_id,
        step=invocation.step,
        reason="evidence_binding_repaired",
        failure_kind="evidence_binding_failed",
        failed_replay_ref=factory_skill_replay_failure_ref(failed),
        retry_ordinal=1,
        issued_at=invocation.lease.issued_at,
        expires_at=invocation.lease.issued_at + timedelta(minutes=5),
        maximum_additional_cost_usd=Decimal("0.03"),
        prior_attempt_reserve_usd=Decimal("0.40"),
        benchmark_reserve_usd=Decimal("0.20"),
        internal_total_cap_usd=Decimal("0.79"),
    )
    retried = await replay_store.retry_failed_hermes(
        failed,
        requested_invocation=invocation,
        authorization=authorization,
    )

    assert retried.acquired is True
    assert retried.record.state == "pending"
    assert retried.record.resume_ordinal == 1
    assert retried.record.prior_failure_ref == factory_skill_replay_failure_ref(
        failed
    )


@pytest.mark.asyncio
async def test_authorized_runtime_retry_resumes_only_seal_without_new_hermes_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 19, 10, tzinfo=timezone.utc)
    factory_job = job_v3(mode="demo").model_copy(
        update={"deadline_at": now + timedelta(minutes=30)}
    )
    architect_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/runtime-retry/discovery",
        now=now,
    )
    tool_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/runtime-retry/build",
        now=now,
    )
    checkpoint_ref = ArtifactRef(
        uri=f"artifact://factory/codex-checkpoint/{'c' * 64}",
        sha256="c" * 64,
        media_type="application/json",
    )
    receipt_ref = ArtifactRef(
        uri=f"artifact://factory/codex-terminal-receipt/{'d' * 64}",
        sha256="d" * 64,
        media_type="application/json",
    )
    prompts: list[str] = []

    class PromptStore:
        def __init__(self) -> None:
            self.refs: list[ArtifactRef] = []

        def persist(self, job_id: UUID, content: bytes) -> ArtifactRef:
            digest = hashlib.sha256(content).hexdigest()
            reference = ArtifactRef(
                uri=f"artifact://factory-prompts/{job_id}/{digest}",
                sha256=digest,
                media_type="application/json",
            )
            self.refs.append(reference)
            return reference

    prompt_store = PromptStore()

    class Process:
        returncode = 0

        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        async def communicate(self) -> tuple[bytes, bytes]:
            prompts.append(self.prompt)
            digest_prefix = "captain_codex_brief_seed_sha256="
            if digest_prefix in self.prompt:
                digest = next(
                    line.removeprefix(digest_prefix)
                    for line in self.prompt.splitlines()
                    if line.startswith(digest_prefix)
                )
                return json.dumps(
                    {
                        "schema": "hermes.factory-codex-brief-attestation.v1",
                        "invocation_id": _invocation_from_prompt(self.prompt)[
                            "invocation_id"
                        ],
                        "seed_sha256": digest,
                        "accepted": True,
                    }
                ).encode(), b""
            return json.dumps(_typed_payload(self.prompt)).encode(), b""

    async def create_process(*command: str, **__: object) -> Process:
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    class InterruptOnceSealer:
        def __init__(self) -> None:
            self.calls: list[
                tuple[FactoryDispatch, FactorySkillInvocationV1, CodexBuildBriefV1]
            ] = []
            self.success = CaptainBuildSealer()
            self.reject_terminal_lineage = False

        def validate_runtime_retry(
            self,
            request: FactoryDispatch,
            invocation: FactorySkillInvocationV1,
            brief: CodexBuildBriefV1,
        ) -> FactoryRuntimeRetryAuthorizationV1:
            assert brief.build_assignment.workspace_ref == invocation.lease.workspace_ref
            if self.reject_terminal_lineage:
                raise FactoryDispatchError(
                    "Factory Codex prior terminal evidence is invalid"
                )
            assert request.runtime_retry_authorization is not None
            assert request.runtime_retry_authorization.invocation_id == invocation.invocation_id
            return request.runtime_retry_authorization

        async def seal(
            self,
            request: FactoryDispatch,
            invocation: FactorySkillInvocationV1,
            brief: CodexBuildBriefV1,
        ) -> CodexBuildEvidenceV1:
            self.calls.append((request, invocation, brief))
            if len(self.calls) == 1:
                raise FactoryCodexBuildInterrupted(
                    reason="codex_timed_out",
                    exit_code=124,
                    checkpoint_ref=checkpoint_ref,
                    terminal_receipt_ref=receipt_ref,
                    resume_ordinal=0,
                )
            return await self.success.seal(request, invocation, brief)

    sealer = InterruptOnceSealer()
    replay_store = FilesystemFactorySkillReplayStore(tmp_path / "runtime-replays")
    factory = HermesCliFactory(
        settings=HermesCliSettings(
            skill_root=tmp_path,
            working_directory=_CAPTAIN_WORKSPACE_ROOT,
            evidence_root=tmp_path / "evidence",
        ),
        released_skill_catalog=_catalog_for(
            tmp_path,
            FactorySkillStep.DISCOVER,
            FactorySkillStep.BRIEF_CODEX,
            FactorySkillStep.SEAL_CODEX_BUILD,
        ),
        replay_store=replay_store,
        codex_build_sealer=sealer,
        codex_prompt_artifact_store=prompt_store,
        clock=lambda: now,
    )
    await factory.dispatch(
        FactoryDispatch(
            job=factory_job,
            action=FactoryAction(
                kind=FactoryActionKind.DISPATCH_AGENT_ARCHITECT,
                attempt=1,
                job_id=factory_job.job_id,
            ),
            role=FactoryRole.AGENT_ARCHITECT,
            lease=architect_lease,
        )
    )
    tool_request = FactoryDispatch(
        job=factory_job,
        action=FactoryAction(
            kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
            attempt=1,
            job_id=factory_job.job_id,
        ),
        role=FactoryRole.TOOL_INTEGRATOR,
        lease=tool_lease,
    )

    with pytest.raises(FactoryCodexBuildInterrupted):
        await factory.dispatch(tool_request)

    assert len(prompts) == 2
    assert len(sealer.calls) == 1
    assert len(prompt_store.refs) == 1
    now = tool_lease.expires_at + timedelta(seconds=1)
    with pytest.raises(FactoryDispatchError, match="active lease"):
        await factory.dispatch(tool_request)
    assert len(prompts) == 2
    assert len(sealer.calls) == 1

    invocation = sealer.calls[0][1]
    authorization = FactoryRuntimeRetryAuthorizationV1(
        schema_name="captain.factory-runtime-retry-authorization.v1",
        authorization_ref=ArtifactRef(
            uri=f"artifact://factory/runtime-retry/{'a' * 64}",
            sha256="a" * 64,
            media_type="application/json",
        ),
        producer="captain",
        status="succeeded",
        job_id=factory_job.job_id,
        correlation_id=factory_job.correlation_id,
        subject_version=factory_job.subject_version,
        attempt=1,
        invocation_id=invocation.invocation_id,
        idempotency_key=invocation.idempotency_key,
        lease_id=tool_lease.lease_id,
        checkpoint_ref=checkpoint_ref,
        terminal_receipt_ref=receipt_ref,
        workspace_ref=tool_lease.workspace_ref,
        base_revision="e" * 40,
        scaffold_manifest_sha256="f" * 64,
        brief_sha256="1" * 64,
        resume_ordinal=1,
        maximum_runtime_seconds=60,
        issued_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    authorized_request = FactoryDispatch(
        job=tool_request.job,
        action=tool_request.action,
        role=tool_request.role,
        lease=tool_request.lease,
        runtime_retry_authorization=authorization,
    )

    sealer.reject_terminal_lineage = True
    with pytest.raises(FactoryDispatchError, match="terminal evidence"):
        await factory.dispatch(authorized_request)
    assert len(sealer.calls) == 1
    sealer.reject_terminal_lineage = False
    evidence = await factory.dispatch(authorized_request)

    assert evidence.phase is FactoryPhase.TOOL_CANDIDATE_TESTED
    assert len(prompts) == 2
    assert len(sealer.calls) == 2
    assert len(prompt_store.refs) == 1
    assert sealer.calls[1][1] == sealer.calls[0][1]
    assert sealer.calls[1][2] == sealer.calls[0][2]
    assert factory_job.private_holdout_refs == tool_request.job.private_holdout_refs

    now = authorization.expires_at + timedelta(seconds=1)
    historical = await factory.dispatch(authorized_request)

    assert historical == evidence
    assert len(prompts) == 2
    assert len(sealer.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("after_job_deadline", [False, True])
async def test_restart_reconciles_durable_codex_failure_to_failed_replay_without_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_job_deadline: bool,
) -> None:
    now = datetime(2026, 7, 19, 10, tzinfo=timezone.utc)
    factory_job = job_v3(mode="demo").model_copy(
        update={"deadline_at": now + timedelta(minutes=30)}
    )
    architect_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/failed-recovery/discovery",
        now=now,
    )
    tool_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/failed-recovery/build",
        now=now,
    )
    checkpoint_ref = ArtifactRef(
        uri=f"artifact://factory/codex-checkpoint/{'c' * 64}",
        sha256="c" * 64,
        media_type="application/json",
    )
    receipt_ref = ArtifactRef(
        uri=f"artifact://factory/codex-terminal-receipt/{'d' * 64}",
        sha256="d" * 64,
        media_type="application/json",
    )
    durable_failure = FactoryCodexBuildFailed(
        reason="runtime_failed",
        checkpoint_ref=checkpoint_ref,
        terminal_receipt_ref=receipt_ref,
    )
    prompts: list[str] = []

    class PromptStore:
        def persist(self, job_id: UUID, content: bytes) -> ArtifactRef:
            digest = hashlib.sha256(content).hexdigest()
            return ArtifactRef(
                uri=f"artifact://factory-prompts/{job_id}/{digest}",
                sha256=digest,
                media_type="application/json",
            )

    class Process:
        returncode = 0

        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        async def communicate(self) -> tuple[bytes, bytes]:
            prompts.append(self.prompt)
            digest_prefix = "captain_codex_brief_seed_sha256="
            if digest_prefix in self.prompt:
                digest = next(
                    line.removeprefix(digest_prefix)
                    for line in self.prompt.splitlines()
                    if line.startswith(digest_prefix)
                )
                return json.dumps(
                    {
                        "schema": "hermes.factory-codex-brief-attestation.v1",
                        "invocation_id": _invocation_from_prompt(self.prompt)[
                            "invocation_id"
                        ],
                        "seed_sha256": digest,
                        "accepted": True,
                    }
                ).encode(), b""
            return json.dumps(_typed_payload(self.prompt)).encode(), b""

    async def create_process(*command: str, **__: object) -> Process:
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    class DurableFailureSealer:
        def __init__(self) -> None:
            self.seal_calls = 0
            self.failure_reconcile_calls = 0

        async def seal(self, *_args: object) -> CodexBuildEvidenceV1:
            self.seal_calls += 1
            raise durable_failure

        def reconcile_failed(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> FactoryCodexBuildFailed:
            self.failure_reconcile_calls += 1
            return durable_failure

        async def reconcile_pending(self, *_args: object) -> CodexBuildEvidenceV1:
            raise AssertionError("expired failure recovery must not reconcile work")

    replay_root = tmp_path / "failed-replays"

    class CrashBeforeReplayFailureStore(FilesystemFactorySkillReplayStore):
        async def fail(self, *args: object, **kwargs: object):
            del args, kwargs
            raise RuntimeError("simulated host crash before replay fail")

    sealer = DurableFailureSealer()
    catalog = _catalog_for(
        tmp_path,
        FactorySkillStep.DISCOVER,
        FactorySkillStep.BRIEF_CODEX,
        FactorySkillStep.SEAL_CODEX_BUILD,
    )
    settings = HermesCliSettings(
        skill_root=tmp_path,
        working_directory=_CAPTAIN_WORKSPACE_ROOT,
        evidence_root=tmp_path / "evidence",
    )
    prompt_store = PromptStore()
    await HermesCliFactory(
        settings=settings,
        released_skill_catalog=catalog,
        replay_store=CrashBeforeReplayFailureStore(replay_root),
        codex_build_sealer=sealer,
        codex_prompt_artifact_store=prompt_store,
        clock=lambda: now,
    ).dispatch(
        FactoryDispatch(
            job=factory_job,
            action=FactoryAction(
                kind=FactoryActionKind.DISPATCH_AGENT_ARCHITECT,
                attempt=1,
                job_id=factory_job.job_id,
            ),
            role=FactoryRole.AGENT_ARCHITECT,
            lease=architect_lease,
        )
    )
    tool_request = FactoryDispatch(
        job=factory_job,
        action=FactoryAction(
            kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
            attempt=1,
            job_id=factory_job.job_id,
        ),
        role=FactoryRole.TOOL_INTEGRATOR,
        lease=tool_lease,
    )
    first_factory = HermesCliFactory(
        settings=settings,
        released_skill_catalog=catalog,
        replay_store=CrashBeforeReplayFailureStore(replay_root),
        codex_build_sealer=sealer,
        codex_prompt_artifact_store=prompt_store,
        clock=lambda: now,
    )

    with pytest.raises(FactoryDispatchError, match="failure state could not be persisted"):
        await first_factory.dispatch(tool_request)

    pending = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in replay_root.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["state"] == "pending"
    ]
    assert len(pending) == 1

    restart_now = (
        factory_job.deadline_at + timedelta(seconds=1)
        if after_job_deadline
        else tool_lease.expires_at + timedelta(seconds=1)
    )
    assert restart_now >= tool_lease.expires_at
    assert tool_request.runtime_retry_authorization is None

    restarted = HermesCliFactory(
        settings=settings,
        released_skill_catalog=catalog,
        replay_store=FilesystemFactorySkillReplayStore(replay_root),
        codex_build_sealer=sealer,
        codex_prompt_artifact_store=prompt_store,
        clock=lambda: restart_now,
    )
    restarted.validate_dispatch_configuration(tool_request)
    with pytest.raises(FactoryCodexBuildFailed) as recovered:
        await restarted.dispatch(tool_request)

    assert recovered.value.reason == "runtime_failed"
    assert sealer.seal_calls == 1
    assert sealer.failure_reconcile_calls == 1
    assert len(prompts) == 2
    failed = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in replay_root.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["state"] == "failed"
    ]
    assert len(failed) == 1
    assert failed[0]["failure_kind"] == "FactoryCodexBuildFailed"

    with pytest.raises(FactoryDispatchError, match="previously failed"):
        await restarted.dispatch(tool_request)
    assert sealer.seal_calls == 1
    assert sealer.failure_reconcile_calls == 1
    assert len(prompts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_ordinal", [1, 2])
async def test_expired_restart_terminalizes_failed_authorized_resume_without_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resume_ordinal: int,
) -> None:
    """Dropping persisted retry lineage would strand a resumed terminal failure."""

    now = datetime(2026, 7, 19, 10, tzinfo=timezone.utc)
    factory_job = job_v3(mode="demo").model_copy(
        update={"deadline_at": now + timedelta(minutes=30)}
    )
    architect_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/resumed-failure/discovery",
        now=now,
    )
    tool_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/resumed-failure/build",
        now=now,
    )
    prompts: list[str] = []

    class PromptStore:
        def persist(self, job_id: UUID, content: bytes) -> ArtifactRef:
            digest = hashlib.sha256(content).hexdigest()
            return ArtifactRef(
                uri=f"artifact://factory-prompts/{job_id}/{digest}",
                sha256=digest,
                media_type="application/json",
            )

    class Process:
        returncode = 0

        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        async def communicate(self) -> tuple[bytes, bytes]:
            prompts.append(self.prompt)
            prefix = "captain_codex_brief_seed_sha256="
            if prefix in self.prompt:
                digest = next(
                    line.removeprefix(prefix)
                    for line in self.prompt.splitlines()
                    if line.startswith(prefix)
                )
                return json.dumps(
                    {
                        "schema": "hermes.factory-codex-brief-attestation.v1",
                        "invocation_id": _invocation_from_prompt(self.prompt)[
                            "invocation_id"
                        ],
                        "seed_sha256": digest,
                        "accepted": True,
                    }
                ).encode(), b""
            return json.dumps(_typed_payload(self.prompt)).encode(), b""

    async def create_process(*command: str, **__: object) -> Process:
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    def checkpoint_ref(ordinal: int) -> ArtifactRef:
        digest = f"{ordinal + 1:x}" * 64
        return ArtifactRef(
            uri=f"artifact://factory/codex-checkpoint/{digest}",
            sha256=digest,
            media_type="application/json",
        )

    def receipt_ref(ordinal: int) -> ArtifactRef:
        digest = f"{ordinal + 9:x}" * 64
        return ArtifactRef(
            uri=f"artifact://factory/codex-terminal-receipt/{digest}",
            sha256=digest,
            media_type="application/json",
        )

    durable_failure = FactoryCodexBuildFailed(
        reason="runtime_failed",
        checkpoint_ref=checkpoint_ref(resume_ordinal),
        terminal_receipt_ref=receipt_ref(resume_ordinal),
    )

    class ResumeThenFailSealer:
        def __init__(self) -> None:
            self.seal_calls = 0
            self.failure_reconcile_calls = 0
            self.expected_authorization: FactoryRuntimeRetryAuthorizationV1 | None = None
            self.invocation: FactorySkillInvocationV1 | None = None

        def validate_runtime_retry(
            self,
            request: FactoryDispatch,
            _invocation: FactorySkillInvocationV1,
            _brief: CodexBuildBriefV1,
        ) -> FactoryRuntimeRetryAuthorizationV1:
            assert request.runtime_retry_authorization is not None
            return request.runtime_retry_authorization

        async def seal(
            self,
            _request: FactoryDispatch,
            invocation: FactorySkillInvocationV1,
            _brief: CodexBuildBriefV1,
        ) -> CodexBuildEvidenceV1:
            self.invocation = invocation
            ordinal = self.seal_calls
            self.seal_calls += 1
            if ordinal < resume_ordinal:
                raise FactoryCodexBuildInterrupted(
                    reason="codex_timed_out",
                    exit_code=124,
                    checkpoint_ref=checkpoint_ref(ordinal),
                    terminal_receipt_ref=receipt_ref(ordinal),
                    resume_ordinal=ordinal,
                )
            raise durable_failure

        def reconcile_failed(
            self,
            *_args: object,
            persisted_resume_ordinal: int | None = None,
            persisted_retry_authorization_ref: ArtifactRef | None = None,
            persisted_retry_authorization_binding_sha256: str | None = None,
        ) -> FactoryCodexBuildFailed:
            authorization = self.expected_authorization
            assert authorization is not None
            expected_digest = hashlib.sha256(
                json.dumps(
                    authorization.model_dump(mode="json", by_alias=True),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if (
                persisted_resume_ordinal != resume_ordinal
                or persisted_retry_authorization_ref
                != authorization.authorization_ref
                or persisted_retry_authorization_binding_sha256
                != expected_digest
            ):
                raise FactoryDispatchError(
                    "persisted Factory retry authority lineage changed"
                )
            self.failure_reconcile_calls += 1
            return durable_failure

        async def reconcile_pending(self, *_args: object) -> CodexBuildEvidenceV1:
            raise AssertionError("expired failure recovery must not reconcile work")

    replay_root = tmp_path / "resumed-failure-replays"

    class CrashBeforeReplayFailureStore(FilesystemFactorySkillReplayStore):
        async def fail(self, *args: object, **kwargs: object):
            del args, kwargs
            raise RuntimeError("simulated host crash before replay fail")

    sealer = ResumeThenFailSealer()
    catalog = _catalog_for(
        tmp_path,
        FactorySkillStep.DISCOVER,
        FactorySkillStep.BRIEF_CODEX,
        FactorySkillStep.SEAL_CODEX_BUILD,
    )
    settings = HermesCliSettings(
        skill_root=tmp_path,
        working_directory=_CAPTAIN_WORKSPACE_ROOT,
        evidence_root=tmp_path / "evidence",
    )
    prompt_store = PromptStore()
    await HermesCliFactory(
        settings=settings,
        released_skill_catalog=catalog,
        replay_store=CrashBeforeReplayFailureStore(replay_root),
        codex_build_sealer=sealer,
        codex_prompt_artifact_store=prompt_store,
        clock=lambda: now,
    ).dispatch(
        FactoryDispatch(
            job=factory_job,
            action=FactoryAction(
                kind=FactoryActionKind.DISPATCH_AGENT_ARCHITECT,
                attempt=1,
                job_id=factory_job.job_id,
            ),
            role=FactoryRole.AGENT_ARCHITECT,
            lease=architect_lease,
        )
    )
    tool_request = FactoryDispatch(
        job=factory_job,
        action=FactoryAction(
            kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
            attempt=1,
            job_id=factory_job.job_id,
        ),
        role=FactoryRole.TOOL_INTEGRATOR,
        lease=tool_lease,
    )
    first_factory = HermesCliFactory(
        settings=settings,
        released_skill_catalog=catalog,
        replay_store=CrashBeforeReplayFailureStore(replay_root),
        codex_build_sealer=sealer,
        codex_prompt_artifact_store=prompt_store,
        clock=lambda: now,
    )

    with pytest.raises(FactoryCodexBuildInterrupted):
        await first_factory.dispatch(tool_request)

    authorized_request = tool_request
    for ordinal in range(1, resume_ordinal + 1):
        now = tool_lease.expires_at + timedelta(seconds=ordinal)
        invocation = sealer.invocation
        assert invocation is not None
        authorization = FactoryRuntimeRetryAuthorizationV1(
            schema_name="captain.factory-runtime-retry-authorization.v1",
            authorization_ref=ArtifactRef(
                uri=f"artifact://factory/runtime-retry/{ordinal:x}{'a' * 63}",
                sha256=f"{ordinal:x}{'a' * 63}",
                media_type="application/json",
            ),
            producer="captain",
            status="succeeded",
            job_id=factory_job.job_id,
            correlation_id=factory_job.correlation_id,
            subject_version=factory_job.subject_version,
            attempt=1,
            invocation_id=invocation.invocation_id,
            idempotency_key=invocation.idempotency_key,
            lease_id=tool_lease.lease_id,
            checkpoint_ref=checkpoint_ref(ordinal - 1),
            terminal_receipt_ref=receipt_ref(ordinal - 1),
            workspace_ref=tool_lease.workspace_ref,
            base_revision="e" * 40,
            scaffold_manifest_sha256="f" * 64,
            brief_sha256="1" * 64,
            resume_ordinal=ordinal,
            maximum_runtime_seconds=60,
            issued_at=now,
            expires_at=now + timedelta(minutes=1),
        )
        authorized_request = FactoryDispatch(
            job=tool_request.job,
            action=tool_request.action,
            role=tool_request.role,
            lease=tool_request.lease,
            runtime_retry_authorization=authorization,
        )
        if ordinal < resume_ordinal:
            with pytest.raises(FactoryCodexBuildInterrupted):
                await first_factory.dispatch(authorized_request)
        else:
            sealer.expected_authorization = authorization
            with pytest.raises(
                FactoryDispatchError,
                match="failure state could not be persisted",
            ):
                await first_factory.dispatch(authorized_request)

    pending_path = next(
        path
        for path in replay_root.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["state"] == "pending"
    )
    original_pending = pending_path.read_bytes()
    pending_payload = json.loads(original_pending)
    assert pending_payload["resume_ordinal"] == resume_ordinal
    assert pending_payload["runtime_retry_authorization_ref"] == (
        authorization.authorization_ref.model_dump(mode="json")
    )

    now = factory_job.deadline_at + timedelta(seconds=1)
    restarted = HermesCliFactory(
        settings=settings,
        released_skill_catalog=catalog,
        replay_store=FilesystemFactorySkillReplayStore(replay_root),
        codex_build_sealer=sealer,
        codex_prompt_artifact_store=prompt_store,
        clock=lambda: now,
    )
    with pytest.raises(FactoryDispatchError, match="active lease"):
        await restarted.dispatch(authorized_request)
    assert pending_path.read_bytes() == original_pending
    assert sealer.failure_reconcile_calls == 0

    pending_payload["runtime_retry_authorization_binding_sha256"] = "0" * 64
    pending_path.write_text(
        json.dumps(
            pending_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    with pytest.raises(FactoryDispatchError, match="retry authority lineage"):
        await restarted.dispatch(tool_request)
    assert json.loads(pending_path.read_text(encoding="utf-8"))["state"] == "pending"
    assert sealer.failure_reconcile_calls == 0

    pending_path.write_bytes(original_pending)
    with pytest.raises(FactoryCodexBuildFailed) as recovered:
        await restarted.dispatch(tool_request)

    assert recovered.value.reason == "runtime_failed"
    assert sealer.seal_calls == resume_ordinal + 1
    assert sealer.failure_reconcile_calls == 1
    assert len(prompts) == 2
    assert json.loads(pending_path.read_text(encoding="utf-8"))["state"] == "failed"


@pytest.mark.asyncio
async def test_expired_lease_recovery_requires_completed_predecessors_before_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued_at = datetime(2026, 7, 19, 10, tzinfo=timezone.utc)
    current = issued_at + timedelta(minutes=16)
    factory_job = job_v3(mode="demo").model_copy(
        update={"deadline_at": issued_at + timedelta(minutes=30)}
    )
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/runtime-retry/missing-predecessors",
        now=issued_at,
    )
    authorization = FactoryRuntimeRetryAuthorizationV1(
        schema_name="captain.factory-runtime-retry-authorization.v1",
        authorization_ref=ArtifactRef(
            uri=f"artifact://factory/runtime-retry/{'a' * 64}",
            sha256="a" * 64,
            media_type="application/json",
        ),
        producer="captain",
        status="succeeded",
        job_id=factory_job.job_id,
        correlation_id=factory_job.correlation_id,
        subject_version=factory_job.subject_version,
        attempt=1,
        invocation_id=UUID("00000000-0000-0000-0000-000000000999"),
        idempotency_key="b" * 64,
        lease_id=lease.lease_id,
        checkpoint_ref=ArtifactRef(
            uri=f"artifact://factory/codex-checkpoint/{'c' * 64}",
            sha256="c" * 64,
            media_type="application/json",
        ),
        terminal_receipt_ref=ArtifactRef(
            uri=f"artifact://factory/codex-terminal-receipt/{'d' * 64}",
            sha256="d" * 64,
            media_type="application/json",
        ),
        workspace_ref=lease.workspace_ref,
        base_revision="e" * 40,
        scaffold_manifest_sha256="f" * 64,
        brief_sha256="1" * 64,
        resume_ordinal=1,
        maximum_runtime_seconds=60,
        issued_at=current,
        expires_at=current + timedelta(minutes=2),
    )
    calls = 0

    async def forbidden_process(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("recovery must not invoke Hermes without predecessors")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_process)
    factory = HermesCliFactory(
        settings=HermesCliSettings(
            skill_root=tmp_path,
            working_directory=_CAPTAIN_WORKSPACE_ROOT,
            evidence_root=tmp_path / "evidence",
        ),
        released_skill_catalog=_catalog_for(
            tmp_path,
            FactorySkillStep.BRIEF_CODEX,
            FactorySkillStep.SEAL_CODEX_BUILD,
        ),
        replay_store=FilesystemFactorySkillReplayStore(tmp_path / "replays"),
        codex_build_sealer=object(),
        clock=lambda: current,
    )

    with pytest.raises(FactoryDispatchError, match="completed discover replay"):
        await factory.dispatch(
            FactoryDispatch(
                job=factory_job,
                action=FactoryAction(
                    kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
                    attempt=1,
                    job_id=factory_job.job_id,
                ),
                role=FactoryRole.TOOL_INTEGRATOR,
                lease=lease,
                runtime_retry_authorization=authorization,
            )
        )

    assert calls == 0


@pytest.mark.asyncio
async def test_concurrent_dispatch_claims_logical_step_before_spawning_hermes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, lease = _architect_dispatch()
    catalog = _catalog_for(tmp_path, FactorySkillStep.DISCOVER)
    communicating = asyncio.Event()
    release_process = asyncio.Event()
    spawn_count = 0

    class Process:
        returncode = 0

        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        async def communicate(self) -> tuple[bytes, bytes]:
            communicating.set()
            await release_process.wait()
            return json.dumps(_typed_payload(self.prompt)).encode(), b""

    async def create_process(*command: str, **__: object) -> Process:
        nonlocal spawn_count
        spawn_count += 1
        if spawn_count > 1:
            raise AssertionError("the pending logical step must fence a second spawn")
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    settings = HermesCliSettings(
        skill_root=tmp_path,
        evidence_root=tmp_path / "evidence",
        working_directory=_CAPTAIN_WORKSPACE_ROOT,
    )
    first = asyncio.create_task(
        HermesCliFactory(
            settings=settings,
            released_skill_catalog=catalog,
            replay_store=FilesystemFactorySkillReplayStore(tmp_path / "replays"),
            clock=lambda: lease.issued_at,
        ).dispatch(request)
    )
    await communicating.wait()

    try:
        with pytest.raises(FactoryDispatchError, match="pending.*recovery"):
            await HermesCliFactory(
                settings=settings,
                released_skill_catalog=catalog,
                replay_store=FilesystemFactorySkillReplayStore(
                    tmp_path / "replays"
                ),
                clock=lambda: lease.issued_at,
            ).dispatch(request)
    finally:
        release_process.set()
        await first
    assert spawn_count == 1


@pytest.mark.asyncio
async def test_filesystem_replay_exposes_pending_claim_after_restart(
    tmp_path: Path,
) -> None:
    invocation = FactorySkillInvocationV1.model_validate(
        invocation_payload(FactorySkillStep.DISCOVER.value)
    )
    acquired = await FilesystemFactorySkillReplayStore(
        tmp_path / "replays"
    ).claim(invocation)
    assert acquired.acquired is True
    assert acquired.record.state == "pending"

    restarted = FilesystemFactorySkillReplayStore(tmp_path / "replays")
    with pytest.raises(FactorySkillReplayPendingError) as caught:
        await restarted.claim(invocation)

    assert caught.value.record == acquired.record


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_completed_replay_accepts_exact_successor_lease_without_new_effect(
    tmp_path: Path,
    durable: bool,
) -> None:
    store = (
        FilesystemFactorySkillReplayStore(tmp_path / "replays")
        if durable
        else InMemoryFactorySkillReplayStore()
    )
    invocation = FactorySkillInvocationV1.model_validate(invocation_payload("discover"))
    invocation = invocation.model_copy(
        update={
            "lease": invocation.lease.model_copy(
                update={
                    "workspace_ref": (
                        "workspace://factory/workflow/"
                        + invocation.lease.issued_at.strftime("%Y%m%dT%H%M%S%fZ")
                    )
                }
            )
        }
    )
    claimed = await store.claim(invocation)
    inventory = CodebaseInventoryV1.model_validate(inventory_payload()).model_copy(
        update={"invocation": invocation}
    )
    completed = await store.complete(
        claimed.record,
        artifact=inventory,
        transcript_ref=ArtifactRef(
            uri="artifact://factory-evidence/successor-replay",
            sha256="e" * 64,
            media_type="application/json",
        ),
    )
    successor = invocation.model_copy(
        update={
            "lease": invocation.lease.model_copy(
                update={
                    "lease_id": "factory-successor-replay",
                    "issued_at": invocation.lease.expires_at,
                    "expires_at": invocation.lease.expires_at + timedelta(minutes=15),
                    "workspace_ref": (
                        invocation.lease.workspace_ref.rsplit("/", 1)[0]
                        + "/"
                        + invocation.lease.expires_at.strftime("%Y%m%dT%H%M%S%fZ")
                    ),
                }
            )
        }
    )

    replay = await store.claim(successor)

    assert replay.acquired is False
    assert replay.record == completed
    assert replay.record.invocation.lease == invocation.lease


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_pending_replay_exposes_original_claim_to_exact_successor_lease(
    tmp_path: Path,
    durable: bool,
) -> None:
    store = (
        FilesystemFactorySkillReplayStore(tmp_path / "replays")
        if durable
        else InMemoryFactorySkillReplayStore()
    )
    invocation = FactorySkillInvocationV1.model_validate(seal_invocation_payload())
    invocation = invocation.model_copy(
        update={
            "lease": invocation.lease.model_copy(
                update={
                    "workspace_ref": (
                        "workspace://factory/workflow/"
                        + invocation.lease.issued_at.strftime("%Y%m%dT%H%M%S%fZ")
                    )
                }
            )
        }
    )
    claimed = await store.claim(invocation)
    successor = invocation.model_copy(
        update={
            "lease": invocation.lease.model_copy(
                update={
                    "lease_id": "factory-successor-pending",
                    "issued_at": invocation.lease.expires_at,
                    "expires_at": invocation.lease.expires_at + timedelta(minutes=15),
                    "workspace_ref": (
                        invocation.lease.workspace_ref.rsplit("/", 1)[0]
                        + "/"
                        + invocation.lease.expires_at.strftime("%Y%m%dT%H%M%S%fZ")
                    ),
                }
            )
        }
    )

    with pytest.raises(FactorySkillReplayPendingError) as caught:
        await store.claim(successor)

    assert caught.value.record == claimed.record
    assert caught.value.record.invocation.lease == invocation.lease


@pytest.mark.asyncio
async def test_resume_claim_restart_restores_only_the_exact_prior_interruption(
    tmp_path: Path,
) -> None:
    invocation = FactorySkillInvocationV1.model_validate(seal_invocation_payload())
    store = FilesystemFactorySkillReplayStore(tmp_path / "replays")
    checkpoint_ref = ArtifactRef(
        uri=f"artifact://factory/codex-checkpoint/{'c' * 64}",
        sha256="c" * 64,
        media_type="application/json",
    )
    receipt_ref = ArtifactRef(
        uri=f"artifact://factory/codex-terminal-receipt/{'d' * 64}",
        sha256="d" * 64,
        media_type="application/json",
    )
    claimed = await store.claim(invocation)
    interrupted = await store.interrupt(
        claimed.record,
        checkpoint_ref=checkpoint_ref,
        terminal_receipt_ref=receipt_ref,
        resume_ordinal=0,
    )
    authorization = FactoryRuntimeRetryAuthorizationV1(
        schema_name="captain.factory-runtime-retry-authorization.v1",
        authorization_ref=ArtifactRef(
            uri=f"artifact://factory/runtime-retry/{'a' * 64}",
            sha256="a" * 64,
            media_type="application/json",
        ),
        producer="captain",
        status="succeeded",
        job_id=invocation.job_id,
        correlation_id=invocation.correlation_id,
        subject_version=invocation.subject_version,
        attempt=invocation.attempt,
        invocation_id=invocation.invocation_id,
        idempotency_key=invocation.idempotency_key,
        lease_id=invocation.lease.lease_id,
        checkpoint_ref=checkpoint_ref,
        terminal_receipt_ref=receipt_ref,
        workspace_ref=invocation.lease.workspace_ref,
        base_revision="e" * 40,
        scaffold_manifest_sha256="f" * 64,
        brief_sha256="1" * 64,
        resume_ordinal=1,
        maximum_runtime_seconds=60,
        issued_at=invocation.lease.issued_at,
        expires_at=invocation.lease.issued_at + timedelta(minutes=1),
    )
    resumed = await store.resume(interrupted, authorization=authorization)

    restored = await store.reconcile_interrupted(
        resumed.record,
        checkpoint_ref=checkpoint_ref,
        terminal_receipt_ref=receipt_ref,
        resume_ordinal=0,
    )

    assert restored.state == "interrupted"
    assert restored.resume_ordinal == 0
    assert restored.checkpoint_ref == checkpoint_ref
    assert restored.terminal_receipt_ref == receipt_ref
    with pytest.raises(FactoryDispatchError, match="no longer pending"):
        await store.reconcile_interrupted(
            restored,
            checkpoint_ref=checkpoint_ref,
            terminal_receipt_ref=receipt_ref,
            resume_ordinal=0,
        )


@pytest.mark.asyncio
async def test_in_memory_replay_claim_is_serialized() -> None:
    invocation = FactorySkillInvocationV1.model_validate(
        invocation_payload(FactorySkillStep.DISCOVER.value)
    )
    store = InMemoryFactorySkillReplayStore()

    results = await asyncio.gather(
        store.claim(invocation),
        store.claim(invocation),
        return_exceptions=True,
    )

    acquired = [
        result
        for result in results
        if not isinstance(result, BaseException) and result.acquired
    ]
    pending = [
        result for result in results if isinstance(result, FactorySkillReplayPendingError)
    ]
    assert len(acquired) == 1
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_changed_input_conflicts_on_same_logical_step_without_new_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, lease = _architect_dispatch()
    catalog = _catalog_for(tmp_path, FactorySkillStep.DISCOVER)
    spawn_count = 0

    class Process:
        returncode = 0

        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        async def communicate(self) -> tuple[bytes, bytes]:
            invocation = _invocation_from_prompt(self.prompt)
            invocations.append(invocation)
            return json.dumps(_typed_payload(self.prompt)).encode(), b""

    async def create_process(*command: str, **__: object) -> Process:
        nonlocal spawn_count
        spawn_count += 1
        if spawn_count > 1:
            raise AssertionError("changed input must not create a second logical effect")
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    invocations: list[dict[str, object]] = []
    settings = HermesCliSettings(
        skill_root=tmp_path,
        evidence_root=tmp_path / "evidence",
        working_directory=_CAPTAIN_WORKSPACE_ROOT,
    )
    first = HermesCliFactory(
        settings=settings,
        released_skill_catalog=catalog,
        replay_store=FilesystemFactorySkillReplayStore(tmp_path / "replays"),
        clock=lambda: lease.issued_at,
    )
    await first.dispatch(request)
    changed_input = ArtifactRef(
        uri="artifact://factory/changed-input",
        sha256="f" * 64,
        media_type="application/json",
    )
    changed_request = request.__class__(
        job=request.job.model_copy(update={"input_ref": changed_input}),
        action=request.action,
        role=request.role,
        lease=request.lease,
    )

    with pytest.raises(FactoryDispatchError, match="invocation conflicts"):
        await HermesCliFactory(
            settings=settings,
            released_skill_catalog=catalog,
            replay_store=FilesystemFactorySkillReplayStore(tmp_path / "replays"),
            clock=lambda: lease.issued_at,
        ).dispatch(changed_request)

    assert spawn_count == 1
    assert len(list((tmp_path / "replays").glob("*.json"))) == 1
    logical_binding = {
        "job_id": str(request.job.job_id),
        "correlation_id": str(request.job.correlation_id),
        "subject_version": request.job.subject_version,
        "attempt": request.action.attempt,
        "step": FactorySkillStep.DISCOVER.value,
    }
    expected_key = hashlib.sha256(
        json.dumps(
            logical_binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert invocations[0]["idempotency_key"] == expected_key


@pytest.mark.asyncio
async def test_factory_uses_durable_filesystem_replay_store_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, lease = _architect_dispatch()
    catalog = _catalog_for(tmp_path, FactorySkillStep.DISCOVER)
    spawn_count = 0

    class Process:
        returncode = 0

        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        async def communicate(self) -> tuple[bytes, bytes]:
            return json.dumps(_typed_payload(self.prompt)).encode(), b""

    async def create_process(*command: str, **__: object) -> Process:
        nonlocal spawn_count
        spawn_count += 1
        if spawn_count > 1:
            raise AssertionError("default replay storage must survive adapter restart")
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    settings = HermesCliSettings(
        skill_root=tmp_path,
        evidence_root=tmp_path / "evidence",
        working_directory=_CAPTAIN_WORKSPACE_ROOT,
    )

    first = await HermesCliFactory(
        settings=settings,
        released_skill_catalog=catalog,
        clock=lambda: lease.issued_at,
    ).dispatch(request)
    replayed = await HermesCliFactory(
        settings=settings,
        released_skill_catalog=catalog,
        clock=lambda: lease.issued_at,
    ).dispatch(request)

    assert first == replayed
    assert spawn_count == 1
    assert len(list((settings.evidence_root / "skill-replays").glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_failed_effect_is_durable_and_never_respawned_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, lease = _architect_dispatch()
    catalog = _catalog_for(tmp_path, FactorySkillStep.DISCOVER)
    spawn_count = 0

    class Process:
        returncode = 7

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"provider failed"

    async def create_process(*_: str, **__: object) -> Process:
        nonlocal spawn_count
        spawn_count += 1
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    settings = HermesCliSettings(
        skill_root=tmp_path,
        evidence_root=tmp_path / "evidence",
        working_directory=_CAPTAIN_WORKSPACE_ROOT,
    )

    with pytest.raises(FactoryDispatchError, match="provider failed"):
        await HermesCliFactory(
            settings=settings,
            released_skill_catalog=catalog,
            clock=lambda: lease.issued_at,
        ).dispatch(request)
    replay_path = next((settings.evidence_root / "skill-replays").glob("*.json"))
    failed_record = json.loads(replay_path.read_text(encoding="utf-8"))
    assert failed_record["state"] == "failed"
    assert failed_record["failure_kind"] == "FactoryDispatchError"

    with pytest.raises(FactoryDispatchError, match="recovery authority"):
        await HermesCliFactory(
            settings=settings,
            released_skill_catalog=catalog,
            clock=lambda: lease.issued_at,
        ).dispatch(request)

    assert spawn_count == 1


@pytest.mark.asyncio
async def test_dispatch_accepts_one_json_block_followed_by_hermes_tool_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_job = job()
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )
    request = FactoryDispatch(
        job=factory_job,
        action=FactoryAction(kind=FactoryActionKind.DISPATCH_AGENT_ARCHITECT, attempt=1, job_id=factory_job.job_id),
        role=FactoryRole.AGENT_ARCHITECT,
        lease=lease,
    )
    catalog = _catalog_for(tmp_path, FactorySkillStep.DISCOVER)

    class EvidenceStore:
        async def persist(self, _, content: bytes) -> ArtifactRef:
            return ArtifactRef(uri="artifact://factory-evidence/test/transcript", sha256="a" * 64, media_type="application/json")

    class Process:
        returncode = 0

        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        async def communicate(self) -> tuple[bytes, bytes]:
            payload = json.dumps(_typed_payload(self.prompt))
            return f"{payload}\n  [tool] (computing...)\n".encode(), b""

    async def create_process(*command: str, **__: object) -> Process:
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    evidence = await HermesCliFactory(
        settings=HermesCliSettings(
            skill_root=tmp_path,
            evidence_root=tmp_path / "evidence",
            working_directory=_CAPTAIN_WORKSPACE_ROOT,
        ),
        evidence_store=EvidenceStore(),
        released_skill_catalog=catalog,
        clock=lambda: lease.issued_at,
    ).dispatch(request)

    assert evidence.phase is FactoryPhase.BLUEPRINT_CREATED


def _released_skill_request(skill_path: Path, content: bytes) -> HermesSkillEvaluationRequest:
    relative = skill_path.as_posix()
    digest = hashlib.sha256(content).hexdigest()
    released_skill = {
        **request_payload()["released_skill"],
        "content_ref": {
            "uri": f"artifact://released-skills/{relative}",
            "sha256": digest,
            "media_type": "text/markdown",
        },
        "content_sha256": digest,
    }
    return HermesSkillEvaluationRequest.model_validate(
        request_payload(released_skill=released_skill)
    )


def _skill_evaluation_payload(request: HermesSkillEvaluationRequest) -> dict[str, object]:
    payload = evidence_payload()
    released_skill = request.released_skill.model_dump(mode="json", by_alias=True)
    receipt = {
        **payload["receipt"],
        "released_skill": released_skill,
        "used_skill_id": request.released_skill.skill_id,
        "used_skill_version": request.released_skill.version,
        "used_skill_sha256": request.released_skill.content_sha256,
    }
    candidate = {**payload["candidate"], "parent_released_skill": released_skill}
    return {
        **payload,
        "request": request.model_dump(mode="json", by_alias=True),
        "receipt": receipt,
        "candidate": candidate,
    }


def _usage_receipt(request: HermesSkillEvaluationRequest) -> HermesSkillUsageReceipt:
    return HermesSkillUsageReceipt.model_validate(
        {
            **receipt_payload(),
            "request_id": str(request.request_id),
            "job_id": str(request.job_id),
            "correlation_id": str(request.correlation_id),
            "lease_id": request.lease.lease_id,
            "released_skill": request.released_skill.model_dump(mode="json", by_alias=True),
            "used_skill_id": request.released_skill.skill_id,
            "used_skill_version": request.released_skill.version,
            "used_skill_sha256": request.released_skill.content_sha256,
        }
    )


def _candidate_result(request: HermesSkillEvaluationRequest) -> FactoryCandidateEvaluationResult:
    return FactoryCandidateEvaluationResult(
        status="succeeded",
        trace_id=str(request.correlation_id),
        assertion_ids=request.acceptance_assertion_ids,
        tool_names=("support_triage",),
        checks=(
            FactoryEvaluationCheck(name="build", status="passed", detail="command exited 0"),
            FactoryEvaluationCheck(name="real_case", status="passed", detail="assertions verified"),
        ),
    )


def test_settings_preserve_positional_constructor_order() -> None:
    settings = HermesCliSettings(
        "custom-hermes",
        Path("legacy-skill"),
        17,
        Path("legacy-evidence"),
    )

    assert settings.executable == "custom-hermes"
    assert settings.skill_root == Path("legacy-skill")
    assert settings.timeout_seconds == 17
    assert settings.evidence_root == Path("legacy-evidence")
    assert settings.released_skill_root == Path("agenten/agent_factory/released-skills")


@pytest.mark.asyncio
async def test_skill_evaluation_prompt_binds_exactly_one_released_skill_and_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_root = tmp_path / "released-skills"
    relative_skill = Path("factory_skill_evaluator/v1/SKILL.md")
    content = b"# Released skill\n"
    skill_path = skill_root / relative_skill
    skill_path.parent.mkdir(parents=True)
    skill_path.write_bytes(content)
    request = _released_skill_request(relative_skill, content)
    observed: tuple[str, ...] = ()

    class Process:
        returncode = 0

        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        async def communicate(self) -> tuple[bytes, bytes]:
            captain_request = json.loads(
                next(
                    line.removeprefix("captain_request_json=")
                    for line in self.prompt.splitlines()
                    if line.startswith("captain_request_json=")
                )
            )
            response_shape = json.loads(
                next(
                    line.removeprefix("response_shape_json=")
                    for line in self.prompt.splitlines()
                    if line.startswith("response_shape_json=")
                )
            )
            payload = _skill_evaluation_payload(request)
            payload.update(
                {
                    "request_id": captain_request["request_id"],
                    "job_id": captain_request["job_id"],
                    "correlation_id": captain_request["correlation_id"],
                    "subject_id": captain_request["subject_id"],
                    "subject_version": captain_request["subject_version"],
                    "request": captain_request,
                    "receipt": response_shape["receipt"],
                }
            )
            return (
                HermesSkillEvaluationEvidence.model_validate(
                    payload
                ).model_dump_json(by_alias=True).encode(),
                b"",
            )

    async def create_process(*command: str, **_: object) -> Process:
        nonlocal observed
        observed = command
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    settings = HermesCliSettings(released_skill_root=skill_root)

    receipt = _usage_receipt(request)
    evidence = await HermesCliFactory(settings=settings).evaluate_skill(
        request,
        receipt=receipt,
        candidate_result=_candidate_result(request),
        candidate_id="support_triage_v1",
        candidate_source_ref=request.candidate_source_ref,
        max_seconds=30,
    )

    prompt = observed[-1]
    expected_path = skill_path.resolve().as_posix()
    assert prompt.count(expected_path) == 1
    captain_request = json.loads(
        next(line.removeprefix("captain_request_json=") for line in prompt.splitlines() if line.startswith("captain_request_json="))
    )
    response_shape = json.loads(
        next(line.removeprefix("response_shape_json=") for line in prompt.splitlines() if line.startswith("response_shape_json="))
    )
    assert captain_request == request.model_dump(mode="json", by_alias=True)
    assert {
        "schema",
        "evidence_id",
        "request_id",
        "job_id",
        "correlation_id",
        "subject_id",
        "subject_version",
        "occurred_at",
        "producer",
        "request",
        "receipt",
        "candidate",
        "tool_gaps",
        "checks",
        "assertion_ids",
        "outcome",
    } == set(response_shape)
    assert response_shape["request"] == captain_request
    assert response_shape["receipt"] == receipt.model_dump(mode="json", by_alias=True)
    assert set(response_shape["checks"][0]) == {
        "check_id",
        "kind",
        "command",
        "status",
        "occurred_at",
        "evidence_ref",
        "assertion_ids",
    }
    assert response_shape["tool_gaps"][0]["schema"] == "TODO_TOOL.v1"
    assert request.released_skill.content_ref.uri in prompt
    assert request.released_skill.content_sha256 in prompt
    assert str(request.request_id) in prompt
    assert str(request.job_id) in prompt
    assert str(request.correlation_id) in prompt
    assert request.subject_id in prompt
    assert str(request.subject_version) in prompt
    assert request.candidate_source_ref.uri in prompt
    assert str(request.max_iterations) in prompt
    assert "api_key" not in prompt.lower()
    assert "authorization" not in prompt.lower()
    assert "http://" not in prompt.lower()
    assert "https://" not in prompt.lower()
    assert "TODO_TOOL.v1" in prompt
    assert "private candidate" in prompt
    assert "never publish" in prompt.lower()
    assert "never write Captain's ledger" in prompt
    assert evidence.request_id == request.request_id


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["outside", "missing", "digest"])
async def test_skill_evaluation_rejects_invalid_released_skill_before_spawning_hermes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    skill_root = tmp_path / "released-skills"
    skill_root.mkdir()
    content = b"# Released skill\n"
    relative_skill = Path("factory_skill_evaluator/v1/SKILL.md")
    request = _released_skill_request(relative_skill, content)
    if case == "outside":
        request = _released_skill_request(Path("../outside/SKILL.md"), content)
    elif case == "digest":
        skill_path = skill_root / relative_skill
        skill_path.parent.mkdir(parents=True)
        skill_path.write_bytes(b"altered")

    spawned = False

    async def create_process(*_: str, **__: object) -> object:
        nonlocal spawned
        spawned = True
        raise AssertionError("Hermes must not be spawned")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="released skill"):
        await HermesCliFactory(
            settings=HermesCliSettings(released_skill_root=skill_root)
        ).issue_skill_usage(request, max_seconds=30)

    assert spawned is False


@pytest.mark.asyncio
async def test_skill_evaluation_rejects_malformed_hermes_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_root = tmp_path / "released-skills"
    relative_skill = Path("factory_skill_evaluator/v1/SKILL.md")
    content = b"# Released skill\n"
    skill_path = skill_root / relative_skill
    skill_path.parent.mkdir(parents=True)
    skill_path.write_bytes(content)
    request = _released_skill_request(relative_skill, content)

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b'{"schema":"hermes.skill-evaluation-evidence.v1"', b""

    async def create_process(*_: str, **__: object) -> Process:
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="typed skill evaluation JSON"):
        await HermesCliFactory(
            settings=HermesCliSettings(released_skill_root=skill_root)
        ).evaluate_skill(
            request,
            receipt=_usage_receipt(request),
            candidate_result=_candidate_result(request),
            candidate_id="support_triage_v1",
            candidate_source_ref=request.candidate_source_ref,
            max_seconds=30,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "unsafe"),
    [
        ("workspace", "workspace://factory/build?api_key=top-secret"),
        ("workspace", "workspace://factory/http://localhost:5678/api/v1"),
        ("workspace", "workspace://factory/n8n.internal:5678/api/v1"),
        ("assertion", "schema_valid authorization=Bearer-hidden"),
        ("assertion", "real_case_green=https://localhost:5678/webhook"),
        ("assertion", "x-authorization=benign-looking"),
        ("assertion", "n8n_endpoint=n8n.internal:5678"),
    ],
)
async def test_skill_prompt_rejects_secret_like_and_raw_endpoint_bypasses_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    unsafe: str,
) -> None:
    skill_root = tmp_path / "released-skills"
    relative_skill = Path("factory_skill_evaluator/v1/SKILL.md")
    content = b"# Released skill\n"
    skill_path = skill_root / relative_skill
    skill_path.parent.mkdir(parents=True)
    skill_path.write_bytes(content)
    request = _released_skill_request(relative_skill, content)
    if field == "workspace":
        request = request.model_copy(
            update={"lease": request.lease.model_copy(update={"workspace_ref": unsafe})}
        )
    else:
        request = request.model_copy(update={"acceptance_assertion_ids": (unsafe,)})
    spawned = False

    async def create_process(*_: str, **__: object) -> object:
        nonlocal spawned
        spawned = True
        raise AssertionError("Hermes must not be spawned")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="unsafe prompt value"):
        await HermesCliFactory(
            settings=HermesCliSettings(released_skill_root=skill_root)
        ).issue_skill_usage(request, max_seconds=30)

    assert spawned is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "lease_capability",
        "lease_raw_bearer",
        "released_skill_ref",
        "candidate_ref",
        "candidate_raw_key",
    ],
)
async def test_skill_prompt_recursively_rejects_unsafe_nested_request_strings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    skill_root = tmp_path / "released-skills"
    relative_skill = Path("factory_skill_evaluator/v1/SKILL.md")
    content = b"# Released skill\n"
    skill_path = skill_root / relative_skill
    skill_path.parent.mkdir(parents=True)
    skill_path.write_bytes(content)
    request = _released_skill_request(relative_skill, content)
    if field in {"lease_capability", "lease_raw_bearer"}:
        unsafe = (
            "n8n_endpoint=n8n.internal:5678"
            if field == "lease_capability"
            else "Bearer abcdefghijklmnop"
        )
        request = request.model_copy(
            update={
                "lease": request.lease.model_copy(
                    update={"capabilities": ("codex.run", unsafe)}
                )
            }
        )
    elif field == "released_skill_ref":
        unsafe_ref = request.released_skill.content_ref.model_copy(
            update={"uri": f"artifact://released-skills/{relative_skill.as_posix()}?api_key=hidden"}
        )
        request = request.model_copy(
            update={
                "released_skill": request.released_skill.model_copy(
                    update={"content_ref": unsafe_ref}
                )
            }
        )
    else:
        unsafe_uri = (
            "artifact://factory/source/sk-abcdefghijk12345"
            if field == "candidate_raw_key"
            else "artifact://factory/source/n8n.internal:5678/api/v1"
        )
        request = request.model_copy(
            update={
                "candidate_source_ref": request.candidate_source_ref.model_copy(
                    update={"uri": unsafe_uri}
                )
            }
        )
    spawned = False

    async def create_process(*_: str, **__: object) -> object:
        nonlocal spawned
        spawned = True
        raise AssertionError("Hermes must not be spawned")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="unsafe prompt value"):
        await HermesCliFactory(
            settings=HermesCliSettings(released_skill_root=skill_root)
        ).issue_skill_usage(request, max_seconds=30)

    assert spawned is False


@pytest.mark.asyncio
async def test_skill_prompt_recursively_rejects_unsafe_receipt_artifact_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_root = tmp_path / "released-skills"
    relative_skill = Path("factory_skill_evaluator/v1/SKILL.md")
    content = b"# Released skill\n"
    skill_path = skill_root / relative_skill
    skill_path.parent.mkdir(parents=True)
    skill_path.write_bytes(content)
    request = _released_skill_request(relative_skill, content)
    receipt = _usage_receipt(request)
    unsafe_ref = receipt.evidence_refs[0].model_copy(
        update={"uri": "artifact://factory/receipt?authorization=Bearer-hidden"}
    )
    receipt = receipt.model_copy(update={"evidence_refs": (unsafe_ref, *receipt.evidence_refs[1:])})
    spawned = False

    async def create_process(*_: str, **__: object) -> object:
        nonlocal spawned
        spawned = True
        raise AssertionError("Hermes must not be spawned")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="unsafe prompt value"):
        await HermesCliFactory(
            settings=HermesCliSettings(released_skill_root=skill_root)
        ).evaluate_skill(
            request,
            receipt=receipt,
            candidate_result=_candidate_result(request),
            candidate_id="support_triage_v1",
            candidate_source_ref=request.candidate_source_ref,
            max_seconds=30,
        )

    assert spawned is False


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_phase", ["resolution", "parsing"])
async def test_skill_usage_uses_one_deadline_through_resolution_and_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slow_phase: str,
) -> None:
    import agenten.agent_factory.hermes_cli as hermes_cli

    skill_root = tmp_path / "released-skills"
    relative_skill = Path("factory_skill_evaluator/v1/SKILL.md")
    content = b"# Released skill\n"
    skill_path = skill_root / relative_skill
    skill_path.parent.mkdir(parents=True)
    skill_path.write_bytes(content)
    request = _released_skill_request(relative_skill, content)
    original_resolve = hermes_cli._resolve_released_skill
    original_parse = hermes_cli._parse_evidence_payload

    if slow_phase == "resolution":
        def slow_resolve(*args: object, **kwargs: object) -> Path:
            time.sleep(0.03)
            return original_resolve(*args, **kwargs)

        monkeypatch.setattr(hermes_cli, "_resolve_released_skill", slow_resolve)
    else:
        def slow_parse(stdout: bytes) -> object:
            time.sleep(0.03)
            return original_parse(stdout)

        monkeypatch.setattr(hermes_cli, "_parse_evidence_payload", slow_parse)

    class Process:
        returncode = 0
        pid = 101

        async def communicate(self) -> tuple[bytes, bytes]:
            return _usage_receipt(request).model_dump_json(by_alias=True).encode(), b""

    async def create_process(*_: str, **__: object) -> Process:
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="timed out|remaining lease time"):
        await HermesCliFactory(
            settings=HermesCliSettings(released_skill_root=skill_root)
        ).issue_skill_usage(request, max_seconds=0.01)


@pytest.mark.asyncio
async def test_skill_usage_timeout_terminates_the_verified_hermes_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agenten.agent_factory.hermes_cli as hermes_cli

    skill_root = tmp_path / "released-skills"
    relative_skill = Path("factory_skill_evaluator/v1/SKILL.md")
    content = b"# Released skill\n"
    skill_path = skill_root / relative_skill
    skill_path.parent.mkdir(parents=True)
    skill_path.write_bytes(content)
    request = _released_skill_request(relative_skill, content)
    terminated: list[int] = []

    class Process:
        returncode = None
        pid = 4242

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        def terminate(self) -> None:
            pass

        async def wait(self) -> int:
            return -15

    async def create_process(*_: str, **__: object) -> Process:
        return Process()

    async def terminate_tree(process: Process, *, executable: str) -> None:
        assert executable == "hermes"
        terminated.append(process.pid)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        hermes_cli,
        "_terminate_async_process_tree",
        terminate_tree,
        raising=False,
    )

    with pytest.raises(FactoryDispatchError, match="timed out"):
        await HermesCliFactory(
            settings=HermesCliSettings(released_skill_root=skill_root)
        ).issue_skill_usage(request, max_seconds=0.5)

    assert terminated == [4242]


@pytest.mark.asyncio
async def test_cancelled_skill_usage_terminates_the_verified_hermes_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agenten.agent_factory.hermes_cli as hermes_cli

    skill_root = tmp_path / "released-skills"
    relative_skill = Path("factory_skill_evaluator/v1/SKILL.md")
    content = b"# Released skill\n"
    skill_path = skill_root / relative_skill
    skill_path.parent.mkdir(parents=True)
    skill_path.write_bytes(content)
    request = _released_skill_request(relative_skill, content)
    communicating = asyncio.Event()
    terminated: list[int] = []

    class Process:
        returncode = None
        pid = 4545

        async def communicate(self) -> tuple[bytes, bytes]:
            communicating.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def create_process(*_: str, **__: object) -> Process:
        return Process()

    async def terminate_tree(process: Process, *, executable: str) -> None:
        assert executable == "hermes"
        terminated.append(process.pid)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(hermes_cli, "_terminate_async_process_tree", terminate_tree)
    task = asyncio.create_task(
        HermesCliFactory(
            settings=HermesCliSettings(released_skill_root=skill_root)
        ).issue_skill_usage(request, max_seconds=30)
    )
    await communicating.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert terminated == [4545]


@pytest.mark.asyncio
async def test_posix_hermes_tree_cleanup_escalates_even_after_the_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agenten.agent_factory.hermes_cli as hermes_cli

    signals: list[int] = []

    class Process:
        pid = 4646
        returncode = 0

        async def wait(self) -> int:
            self.returncode = -signal.SIGTERM
            return self.returncode

    monkeypatch.setattr(
        hermes_cli,
        "os",
        SimpleNamespace(
            name="posix",
            killpg=lambda _pid, sent: signals.append(sent),
        ),
    )

    await hermes_cli._terminate_async_process_tree(Process(), executable="hermes")

    assert signals == [signal.SIGTERM, 9]


@pytest.mark.asyncio
async def test_posix_hermes_tree_cleanup_bounds_both_waits_around_group_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agenten.agent_factory.hermes_cli as hermes_cli

    signals: list[int] = []
    wait_timeouts: list[float] = []

    class Process:
        pid = 4747
        returncode = None

        async def wait(self) -> int:
            self.returncode = -signal.SIGTERM
            return self.returncode

    async def bounded_wait(awaitable, *, timeout: float):
        wait_timeouts.append(timeout)
        if len(wait_timeouts) == 1:
            awaitable.close()
            raise TimeoutError
        return await awaitable

    monkeypatch.setattr(
        hermes_cli,
        "os",
        SimpleNamespace(
            name="posix",
            killpg=lambda _pid, sent: signals.append(sent),
        ),
    )
    monkeypatch.setattr(hermes_cli.asyncio, "wait_for", bounded_wait)

    await hermes_cli._terminate_async_process_tree(Process(), executable="hermes")

    assert signals == [signal.SIGTERM, 9]
    assert wait_timeouts == [5, 5]


@pytest.mark.asyncio
async def test_skill_usage_timeout_terminates_the_hermes_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_root = tmp_path / "released-skills"
    relative_skill = Path("factory_skill_evaluator/v1/SKILL.md")
    content = b"# Released skill\n"
    skill_path = skill_root / relative_skill
    skill_path.parent.mkdir(parents=True)
    skill_path.write_bytes(content)
    request = _released_skill_request(relative_skill, content)
    terminated = False

    class Process:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        def terminate(self) -> None:
            nonlocal terminated
            terminated = True

        async def wait(self) -> int:
            return -15

    async def create_process(*_: str, **__: object) -> Process:
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="timed out"):
        await HermesCliFactory(
            settings=HermesCliSettings(released_skill_root=skill_root)
        ).issue_skill_usage(request, max_seconds=0.5)

    assert terminated is True
