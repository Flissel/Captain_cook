from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from agenten.agent_factory.contracts import FactoryPhase, FactoryRole
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_factory.hermes_cli import HermesCliFactory
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
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
