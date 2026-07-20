from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

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
)
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from tests.agent_factory.test_skill_evaluation_contracts import (
    evidence_payload,
    receipt_payload,
    request_payload,
)
from tests.agent_factory.test_state_machine import block, job


@pytest.mark.asyncio
async def test_dispatch_uses_oneshot_mode_for_parseable_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
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

    class EvidenceStore:
        async def persist(self, _, content: bytes) -> ArtifactRef:
            return ArtifactRef(
                uri="artifact://factory-evidence/test/transcript",
                sha256="a" * 64,
                media_type="application/json",
            )

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return block(FactoryPhase.BLUEPRINT_CREATED).model_dump_json(by_alias=True).encode(), b""

    async def create_process(*command: str, **_: object) -> Process:
        nonlocal observed
        observed = command
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    evidence = await HermesCliFactory(evidence_store=EvidenceStore()).dispatch(request)

    assert observed[:2] == ("hermes", "-z")
    assert "chat" not in observed
    assert '"phase":"blueprint_created"' in observed[-1]
    assert f'"lease_id":"{lease.lease_id}"' in observed[-1]
    assert evidence.phase is FactoryPhase.BLUEPRINT_CREATED
    assert evidence.evidence_refs[0].uri == "artifact://factory-evidence/test/transcript"


@pytest.mark.asyncio
async def test_dispatch_accepts_one_json_block_followed_by_hermes_tool_telemetry(
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

    class EvidenceStore:
        async def persist(self, _, content: bytes) -> ArtifactRef:
            return ArtifactRef(uri="artifact://factory-evidence/test/transcript", sha256="a" * 64, media_type="application/json")

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            payload = block(FactoryPhase.BLUEPRINT_CREATED).model_dump_json(by_alias=True)
            return f"{payload}\n  [tool] (computing...)\n".encode(), b""

    async def create_process(*_: str, **__: object) -> Process:
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    evidence = await HermesCliFactory(evidence_store=EvidenceStore()).dispatch(request)

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


def test_settings_preserve_legacy_positional_constructor_order() -> None:
    settings = HermesCliSettings(
        "custom-hermes",
        Path("legacy-skill"),
        17,
        Path("legacy-evidence"),
    )

    assert settings.executable == "custom-hermes"
    assert settings.skill_path == Path("legacy-skill")
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
        ).issue_skill_usage(request, max_seconds=0.01)

    assert terminated is True
