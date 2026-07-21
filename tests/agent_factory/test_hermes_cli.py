from __future__ import annotations

import asyncio
import hashlib
import json
import signal
import time
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from agenten.agent_factory.contracts import FactoryPhase, FactoryRole
from agenten.agent_factory.candidate_evaluation import (
    FactoryCandidateEvaluationResult,
    FactoryEvaluationCheck,
)
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_factory.hermes_cli import HermesCliFactory, HermesCliSettings
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.skill_evaluation import (
    HermesSkillEvaluationEvidence,
    HermesSkillEvaluationRequest,
    HermesSkillUsageReceipt,
    ReleasedHermesSkill,
)
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from tests.agent_factory.test_skill_evaluation_contracts import (
    evidence_payload,
    receipt_payload,
    request_payload,
)
from tests.agent_factory.test_state_machine import block, job
from tests.agent_factory.test_skill_workflow_contracts import (
    brief_payload,
    evaluation_payload,
    feedback_payload,
    inventory_payload,
    revision_payload,
)


_STEP_SKILL_NAMES = {
    FactorySkillStep.DISCOVER: "captain-factory-discover",
    FactorySkillStep.BRIEF_CODEX: "captain-factory-brief-codex",
    FactorySkillStep.EXECUTE_TEAM: "captain-factory-execute-team",
    FactorySkillStep.EVALUATE_TEAM: "captain-factory-evaluate-team",
    FactorySkillStep.IMPROVE_TEAM: "captain-factory-improve-team",
    FactorySkillStep.REPORT_CAPTAIN: "captain-factory-report-captain",
}


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
        payload = inventory_payload()
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
    catalog = _catalog_for(tmp_path, FactorySkillStep.DISCOVER)

    class EvidenceStore:
        async def persist(self, _, content: bytes) -> ArtifactRef:
            return ArtifactRef(
                uri="artifact://factory-evidence/test/transcript",
                sha256="a" * 64,
                media_type="application/json",
            )

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
        settings=HermesCliSettings(skill_root=tmp_path),
        evidence_store=EvidenceStore(),
        released_skill_catalog=catalog,
        clock=lambda: lease.issued_at,
    ).dispatch(request)

    assert observed[:2] == ("hermes", "-z")
    assert "chat" not in observed
    assert "/captain-factory-discover" in observed[-1]
    assert "captain_invocation_json=" in observed[-1]
    assert f'"lease_id":"{lease.lease_id}"' in observed[-1]
    assert evidence.phase is FactoryPhase.BLUEPRINT_CREATED
    assert any(
        ref.uri == "artifact://factory-evidence/test/transcript"
        for ref in evidence.evidence_refs
    )


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
            settings=HermesCliSettings(skill_root=tmp_path),
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
            settings=HermesCliSettings(skill_root=tmp_path),
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
            settings=HermesCliSettings(skill_root=tmp_path),
            released_skill_catalog=catalog,
            clock=lambda: lease.expires_at,
        ).dispatch(request)


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
            settings=HermesCliSettings(skill_root=tmp_path, timeout_seconds=0.01),
            released_skill_catalog=catalog,
            clock=lambda: lease.issued_at,
        ).dispatch(request)

    assert terminated == [process]


@pytest.mark.asyncio
async def test_quality_sequence_stops_after_unresolved_evaluation(
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
            payload = _typed_payload(self.prompt)
            payload.update(
                {
                    "failure_class": "unresolved",
                    "recommendation": "MANUAL_DECISION_REQUIRED",
                }
            )
            return json.dumps(payload).encode(), b""

    async def create_process(*command: str, **__: object) -> Process:
        return Process(command[-1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    evidence = await HermesCliFactory(
        settings=HermesCliSettings(skill_root=tmp_path),
        released_skill_catalog=catalog,
        clock=lambda: lease.issued_at,
    ).dispatch(request)

    assert [item["step"] for item in invocations] == ["evaluate_team"]
    assert catalog.calls == [FactorySkillStep.EVALUATE_TEAM]
    assert evidence.phase is FactoryPhase.QUALITY_REVIEWED
    assert evidence.status.value == "failed"


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
        settings=HermesCliSettings(skill_root=tmp_path),
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
    )

    async def create_process(*_: str, **__: object) -> object:
        raise AssertionError("Hermes must not run without Captain retry authority")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="IMPROVEMENT_REQUESTED"):
        await HermesCliFactory(
            settings=HermesCliSettings(skill_root=tmp_path),
            released_skill_catalog=catalog,
            clock=lambda: lease.issued_at,
        ).dispatch(request)


@pytest.mark.asyncio
async def test_authorized_retry_runs_improve_before_brief_codex(
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
        improvement_authorized=True,
    )
    catalog = _catalog_for(
        tmp_path,
        FactorySkillStep.IMPROVE_TEAM,
        FactorySkillStep.BRIEF_CODEX,
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
        settings=HermesCliSettings(skill_root=tmp_path),
        released_skill_catalog=catalog,
        clock=lambda: lease.issued_at,
    ).dispatch(request)

    assert [item["step"] for item in invocations] == [
        "improve_team",
        "brief_codex",
    ]
    assert invocations[1]["input_ref"] == revision_payload()["artifact_ref"]
    assert evidence.phase is FactoryPhase.TOOL_CANDIDATE_TESTED
    assert len(evidence.artifact_refs) == 2


@pytest.mark.asyncio
async def test_dispatch_replay_uses_identical_invocation_and_idempotency_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    factory = HermesCliFactory(
        settings=HermesCliSettings(skill_root=tmp_path),
        released_skill_catalog=catalog,
        clock=lambda: lease.issued_at,
    )

    first = await factory.dispatch(request)
    second = await factory.dispatch(request)

    assert invocations[0]["invocation_id"] == invocations[1]["invocation_id"]
    assert invocations[0]["idempotency_key"] == invocations[1]["idempotency_key"]
    assert first == second


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
        settings=HermesCliSettings(skill_root=tmp_path),
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
        ).issue_skill_usage(request, max_seconds=0.1)

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
        ).issue_skill_usage(request, max_seconds=0.1)

    assert terminated is True
