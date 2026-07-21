from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from agenten.agent_factory.capability_controlled_recovery import (
    ContentAddressedControlledRecoveryEffectStore,
    PreparedControlledRecoveryTeamDispatcher,
    build_controlled_recovery_port,
    build_production_controlled_recovery_port,
)
from agenten.agent_factory.capability_live_adapters import ContentAddressedArtifactStore
from agenten.agent_factory.capability_v3_evidence_bridge import build_v3_job_from_package_c
from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.factory_live_runner import InMemoryFactoryLiveEffectLedger
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.service import InMemoryFactoryRepository
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_factory.team_execution import TeamExecutionCandidateAdapter

from tests.agent_factory import test_capability_v3_evidence_bridge as fixtures


@dataclass
class _CountingService:
    delegate: object
    calls: int = 0

    async def execute(self, *args: object, **kwargs: object):
        self.calls += 1
        return await self.delegate.execute(*args, **kwargs)


@pytest.mark.asyncio
async def test_controlled_recovery_reserves_then_recovers_without_provider_replay(
    tmp_path: Path,
) -> None:
    fixtures.AUTHORITY = fixtures._Authority()
    v2 = fixtures._job()
    v3 = build_v3_job_from_package_c(v2, fixtures._policy())
    result = fixtures._result(v2)
    candidate_contract = fixtures._candidate(v2, result)
    candidate = fixtures._resolved_candidate(tmp_path, candidate_contract)
    artifacts = ContentAddressedArtifactStore(tmp_path / "artifacts")
    repository = InMemoryFactoryRepository()
    repository.register(v3)
    ledger = InMemoryFactoryLiveEffectLedger()
    service = _CountingService(fixtures._TeamService(fixtures.AUTHORITY, artifacts))

    def invocation_for(dispatch: FactoryDispatch):
        return fixtures._invocation(v3, dispatch)

    team = TeamExecutionCandidateAdapter(
        service_for=lambda _job, _invocation: service,
        invocation_for=invocation_for,
    )
    prepared = PreparedControlledRecoveryTeamDispatcher(
        team_execution=team,
        service_for=lambda _job, _invocation: service,
    )
    effect_store = ContentAddressedControlledRecoveryEffectStore(artifacts)
    port = build_controlled_recovery_port(
        repository=repository,
        effect_ledger=ledger,
        prepared_dispatch=prepared,
        effect_store=effect_store,
        clock=lambda: fixtures.NOW + timedelta(seconds=30),
    )
    lease = issue_factory_lease(
        job=v3,
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        workspace_ref="workspace://captain/recovery",
        now=fixtures.NOW,
    )
    dispatch = FactoryDispatch(
        job=v3,
        action=FactoryAction(
            kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
            attempt=1,
            job_id=v3.job_id,
        ),
        role=FactoryRole.REAL_CASE_TESTER,
        lease=lease,
    )

    recovered = await port.execute(v3, dispatch, candidate)

    assert service.calls == 1
    assert recovered.interrupted.status == "infrastructure_recovery_required"
    assert recovered.interrupted.effects[0].status == "reserved"
    assert recovered.resumed.effects[0].status == "succeeded"
    assert recovered.resumed.effects[0].completion_origin == "recover"
    assert recovered.interrupted.effects[0].effect_id == recovered.resumed.effects[0].effect_id
    assert recovered.execution.invocation_id not in {
        team.invocation_for(dispatch).invocation_id
    }
    records = ledger.history(v3.job_id)
    assert len(records) == 1
    assert records[0].outcome is not None
    assert records[0].outcome.completion_origin == "recover"
    stored = effect_store.load(records[0].request.effect_id)
    assert stored is not None
    assert stored.record.execution == recovered.execution
    assert stored.reference == recovered.provider_effect_receipt_ref

    replayed = await port.execute(v3, dispatch, candidate)
    assert replayed == recovered
    assert service.calls == 1


def test_production_builder_rejects_non_durable_test_ledger(tmp_path: Path) -> None:
    artifacts = ContentAddressedArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ValueError, match="durable Gateway effect ledger"):
        build_production_controlled_recovery_port(
            repository=InMemoryFactoryRepository(),
            effect_ledger=InMemoryFactoryLiveEffectLedger(),
            prepared_dispatch=object(),
            effect_store=ContentAddressedControlledRecoveryEffectStore(artifacts),
            clock=lambda: fixtures.NOW,
        )
