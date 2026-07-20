from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenten.agent_factory.contracts import FactoryPhase, FactoryRole
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_factory.hermes_cli import HermesCliFactory, HermesCliSettings
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.skill_evaluation import (
    HermesSkillEvaluationEvidence,
    HermesSkillEvaluationRequest,
)
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from tests.agent_factory.test_skill_evaluation_contracts import evidence_payload, request_payload
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

        async def communicate(self) -> tuple[bytes, bytes]:
            return (
                HermesSkillEvaluationEvidence.model_validate(
                    _skill_evaluation_payload(request)
                ).model_dump_json(by_alias=True).encode(),
                b"",
            )

    async def create_process(*command: str, **_: object) -> Process:
        nonlocal observed
        observed = command
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    settings = HermesCliSettings(released_skill_root=skill_root)

    evidence = await HermesCliFactory(settings=settings).evaluate_skill(request)

    prompt = observed[-1]
    expected_path = skill_path.resolve().as_posix()
    assert prompt.count(expected_path) == 1
    assert prompt.count(request.released_skill.content_ref.uri) == 1
    assert prompt.count(request.released_skill.content_sha256) == 1
    assert prompt.count(request.lease.lease_id) == 1
    assert prompt.count(request.lease.workspace_ref) == 1
    assert all(prompt.count(assertion_id) == 1 for assertion_id in request.acceptance_assertion_ids)
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
        ).evaluate_skill(request)

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
        ).evaluate_skill(request)
