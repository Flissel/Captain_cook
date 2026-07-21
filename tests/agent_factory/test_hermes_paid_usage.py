from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.execution_budget import (
    FactoryUsageReceiptV1,
    InMemoryFactoryBudgetLedger,
)
from agenten.agent_factory.hermes_cli import (
    HermesCliFactory,
    HermesCliSettings,
    InMemoryFactorySkillReplayStore,
)
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.service import InMemoryFactoryWorkflowArtifactSink
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_hermes_cli import _catalog_for, _typed_payload
from tests.agent_factory.test_skill_workflow_contracts import inventory_payload
from tests.agent_factory.test_state_machine import NOW, job_v3
from gateway.factory_repository import (
    GatewayFactoryBudgetLedger,
    GatewayFactoryWorkflowArtifactSink,
)


def _request() -> FactoryDispatch:
    factory_job = job_v3()
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=NOW,
    )
    return FactoryDispatch(
        job=factory_job,
        action=FactoryAction(
            kind=FactoryActionKind.DISPATCH_AGENT_ARCHITECT,
            attempt=1,
            job_id=factory_job.job_id,
        ),
        role=FactoryRole.AGENT_ARCHITECT,
        lease=lease,
    )


def _usage_report(**updates: object) -> dict[str, object]:
    report: dict[str, object] = {
        "estimated_cost_usd": 0.0101,
        "cost_status": "estimated",
        "cost_source": "pricing-table",
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 10,
        "cache_write_tokens": 0,
        "reasoning_tokens": 5,
        "total_tokens": 120,
        "api_calls": 1,
        "model": "approved-model-id",
        "provider": "approved-provider",
        "session_id": "hermes-session-1",
        "completed": True,
        "failed": False,
        "service_tier": None,
    }
    report.update(updates)
    return report


class _EvidenceStore:
    def __init__(self) -> None:
        self.contents: list[bytes] = []

    async def persist(self, _job: object, content: bytes) -> ArtifactRef:
        self.contents.append(content)
        digest = hashlib.sha256(content).hexdigest()
        return ArtifactRef(
            uri=f"artifact://factory-evidence/{digest}",
            sha256=digest,
            media_type="application/json",
        )


@pytest.mark.asyncio
async def test_paid_hermes_claims_then_reserves_and_records_exact_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    budget = InMemoryFactoryBudgetLedger()
    replay = InMemoryFactorySkillReplayStore()
    sink = InMemoryFactoryWorkflowArtifactSink()
    evidence_store = _EvidenceStore()
    observed: tuple[str, ...] = ()

    class Process:
        returncode = 0

        def __init__(self, command: tuple[str, ...]) -> None:
            self.command = command

        async def communicate(self) -> tuple[bytes, bytes]:
            # Reservation must exist before the paid process can do work.
            assert budget.projection(request.job.job_id).reserved_usd == Decimal("5")
            usage_path = Path(self.command[self.command.index("--usage-file") + 1])
            assert not usage_path.is_relative_to(Path.cwd())
            usage_path.write_text(json.dumps(_usage_report()), encoding="utf-8")
            return json.dumps(_typed_payload(self.command[-1])).encode(), b""

    async def create_process(*command: str, **_: object) -> Process:
        nonlocal observed
        observed = command
        return Process(command)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    block = await HermesCliFactory(
        settings=HermesCliSettings(skill_root=tmp_path),
        evidence_store=evidence_store,
        released_skill_catalog=_catalog_for(tmp_path, FactorySkillStep.DISCOVER),
        replay_store=replay,
        budget=budget,
        workflow_artifact_sink=sink,
        clock=lambda: NOW,
    ).dispatch(request)

    assert observed[:2] == ("hermes", "-z")
    assert "--usage-file" in observed
    projection = budget.projection(request.job.job_id)
    assert projection.consumed_usd == Decimal("0.02")
    assert projection.reserved_usd == Decimal("0")
    receipts = [event.receipt for event in budget.events if hasattr(event, "receipt")]
    reservations = [
        event.reservation for event in budget.events if hasattr(event, "reservation")
    ]
    assert len(receipts) == 1
    assert reservations[0].invocation_id == receipts[0].invocation_id
    assert receipts[0].invocation_id == sink.artifacts(request.job.job_id)[0].invocation_id
    assert receipts[0].provider == "approved-provider"
    assert receipts[0].model == "approved-model-id"
    assert receipts[0].input_units == 100
    assert receipts[0].output_units == 20
    assert receipts[0].cost_usd == Decimal("0.02")
    assert len(evidence_store.contents) == 3  # usage report, usage receipt, transcript
    assert len(sink.artifacts(request.job.job_id)) == 1
    assert set(block.evidence_refs).issuperset(
        {receipts[0].evidence_ref}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report",
    [
        None,
        _usage_report(cost_status="unknown"),
        _usage_report(estimated_cost_usd=None),
        _usage_report(completed=False),
        _usage_report(failed=True),
        _usage_report(api_calls=0),
        _usage_report(unexpected="field"),
    ],
)
async def test_unresolved_usage_keeps_full_reservation_and_emits_no_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report: dict[str, object] | None,
) -> None:
    request = _request()
    budget = InMemoryFactoryBudgetLedger()
    sink = InMemoryFactoryWorkflowArtifactSink()

    class Process:
        returncode = 0

        def __init__(self, command: tuple[str, ...]) -> None:
            self.command = command

        async def communicate(self) -> tuple[bytes, bytes]:
            if report is not None:
                usage_path = Path(self.command[self.command.index("--usage-file") + 1])
                usage_path.write_text(json.dumps(report), encoding="utf-8")
            return json.dumps(_typed_payload(self.command[-1])).encode(), b""

    async def create_process(*command: str, **_: object) -> Process:
        return Process(command)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(FactoryDispatchError, match="provider_cost_unresolved"):
        await HermesCliFactory(
            settings=HermesCliSettings(skill_root=tmp_path),
            evidence_store=_EvidenceStore(),
            released_skill_catalog=_catalog_for(tmp_path, FactorySkillStep.DISCOVER),
            replay_store=InMemoryFactorySkillReplayStore(),
            budget=budget,
            workflow_artifact_sink=sink,
            clock=lambda: NOW,
        ).dispatch(request)

    projection = budget.projection(request.job.job_id)
    assert projection.consumed_usd == Decimal("0")
    assert projection.reserved_usd == Decimal("5")
    assert sink.artifacts(request.job.job_id) == ()


@pytest.mark.asyncio
async def test_artifact_sink_failure_prevents_lifecycle_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    budget = InMemoryFactoryBudgetLedger()
    replay = InMemoryFactorySkillReplayStore()
    process_calls = 0

    class Sink:
        def __init__(self) -> None:
            self.calls = 0

        async def persist(self, _artifact: object) -> bool:
            self.calls += 1
            if self.calls == 1:
                raise OSError("gateway unavailable")
            return True

    class Process:
        returncode = 0

        def __init__(self, command: tuple[str, ...]) -> None:
            self.command = command

        async def communicate(self) -> tuple[bytes, bytes]:
            usage_path = Path(self.command[self.command.index("--usage-file") + 1])
            usage_path.write_text(json.dumps(_usage_report()), encoding="utf-8")
            return json.dumps(_typed_payload(self.command[-1])).encode(), b""

    async def create_process(*command: str, **_: object) -> Process:
        nonlocal process_calls
        process_calls += 1
        return Process(command)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    sink = Sink()
    factory = HermesCliFactory(
        settings=HermesCliSettings(skill_root=tmp_path),
        evidence_store=_EvidenceStore(),
        released_skill_catalog=_catalog_for(tmp_path, FactorySkillStep.DISCOVER),
        replay_store=replay,
        budget=budget,
        workflow_artifact_sink=sink,
        clock=lambda: NOW,
    )
    with pytest.raises(OSError, match="gateway unavailable"):
        await factory.dispatch(request)

    assert budget.projection(request.job.job_id).consumed_usd == Decimal("0.02")
    recovered = await factory.dispatch(request)
    assert recovered.artifact_refs
    assert process_calls == 1


@pytest.mark.asyncio
async def test_completed_replay_rechecks_sink_without_second_paid_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    budget = InMemoryFactoryBudgetLedger()
    replay = InMemoryFactorySkillReplayStore()
    sink = InMemoryFactoryWorkflowArtifactSink()
    calls = 0

    class Process:
        returncode = 0

        def __init__(self, command: tuple[str, ...]) -> None:
            self.command = command

        async def communicate(self) -> tuple[bytes, bytes]:
            usage_path = Path(self.command[self.command.index("--usage-file") + 1])
            usage_path.write_text(json.dumps(_usage_report()), encoding="utf-8")
            return json.dumps(_typed_payload(self.command[-1])).encode(), b""

    async def create_process(*command: str, **_: object) -> Process:
        nonlocal calls
        calls += 1
        return Process(command)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    factory = HermesCliFactory(
        settings=HermesCliSettings(skill_root=tmp_path),
        evidence_store=_EvidenceStore(),
        released_skill_catalog=_catalog_for(tmp_path, FactorySkillStep.DISCOVER),
        replay_store=replay,
        budget=budget,
        workflow_artifact_sink=sink,
        clock=lambda: NOW,
    )

    first = await factory.dispatch(request)
    second = await factory.dispatch(request)

    assert first == second
    assert calls == 1
    assert len(sink.artifacts(request.job.job_id)) == 1
    assert budget.projection(request.job.job_id).consumed_usd == Decimal("0.02")


def test_gateway_budget_accepts_usage_bound_to_exact_paid_hermes_lease() -> None:
    request = _request()
    assert request.lease is not None
    local = InMemoryFactoryBudgetLedger()
    reservation = local.reserve(
        request.job,
        attempt=1,
        requested_usd=Decimal("5.00"),
        now=NOW,
    )
    usage = FactoryUsageReceiptV1(
        schema_name="captain.factory-usage-receipt.v1",
        receipt_id=UUID("20000000-0000-0000-0000-000000000001"),
        reservation_id=reservation.reservation_id,
        job_id=request.job.job_id,
        correlation_id=request.job.correlation_id,
        attempt=1,
        lease_id=request.lease.lease_id,
        provider="approved-provider",
        model="approved-model-id",
        input_units=100,
        output_units=20,
        cost_usd=Decimal("0.02"),
        started_at=NOW,
        ended_at=NOW,
        evidence_ref=ArtifactRef(
            uri="artifact://factory-usage/exact-lease",
            sha256="e" * 64,
            media_type="application/json",
        ),
    )

    class Store:
        def __init__(self) -> None:
            self.submission = None

        def factory_job(self, _job_id):
            return SimpleNamespace(leases=(request.lease,))

        def record_factory_usage(self, submission):
            self.submission = submission
            return SimpleNamespace(
                event_id=UUID("30000000-0000-0000-0000-000000000001"),
                job_id=request.job.job_id,
                replayed=False,
            )

    store = Store()
    GatewayFactoryBudgetLedger(store).record_usage(
        request.job,
        reservation,
        usage,
    )

    assert store.submission.lease_id == request.lease.lease_id


@pytest.mark.asyncio
async def test_gateway_workflow_sink_exposes_idempotent_store_result() -> None:
    artifact = __import__(
        "agenten.agent_factory.skill_workflow_contracts",
        fromlist=["CodebaseInventoryV1"],
    ).CodebaseInventoryV1.model_validate(inventory_payload())

    class Store:
        def __init__(self) -> None:
            self.artifacts = []

        def record_factory_workflow_artifact(self, candidate):
            self.artifacts.append(candidate)
            return SimpleNamespace(replayed=len(self.artifacts) > 1)

    store = Store()
    sink = GatewayFactoryWorkflowArtifactSink(store)

    assert await sink.persist(artifact) is True
    assert await sink.persist(artifact) is False
    assert store.artifacts == [artifact, artifact]
