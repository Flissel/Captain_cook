from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from agenten.agent_factory.hermes_cli import InMemoryFactorySkillReplayStore
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_factory.skill_workflow_contracts import FactorySkillInvocationV1
from gateway.factory_hermes_retry_authority import (
    FilesystemFactoryHermesRetryAuthority,
)
from tests.agent_factory.test_skill_workflow_contracts import invocation_payload


@pytest.mark.asyncio
async def test_filesystem_authority_round_trips_strict_json_and_exact_failure(
    tmp_path: Path,
) -> None:
    payload = invocation_payload("improve_team", attempt=2)
    lease = payload["lease"]
    assert isinstance(lease, dict)
    lease["attempt"] = 2
    invocation = FactorySkillInvocationV1.model_validate(payload)
    replay_store = InMemoryFactorySkillReplayStore()
    claim = await replay_store.claim(invocation)
    failed = await replay_store.fail(
        claim.record,
        failure_kind="FactoryDispatchError",
    )
    authority = FilesystemFactoryHermesRetryAuthority(tmp_path / "authorities")

    issued = authority.issue(failed, now=invocation.lease.issued_at)
    loaded = authority.active(
        failed,
        requested_invocation=invocation,
        now=invocation.lease.issued_at + timedelta(seconds=1),
    )

    assert loaded == issued
    assert loaded.maximum_additional_cost_usd + loaded.prior_attempt_reserve_usd + loaded.benchmark_reserve_usd == loaded.internal_total_cap_usd
    assert loaded.user_total_cap_eur == 1

    retried = await replay_store.retry_failed_hermes(
        failed,
        requested_invocation=invocation,
        authorization=issued,
    )
    failed_again = await replay_store.fail(
        retried.record,
        failure_kind="FactoryDispatchError",
    )

    issued_again = authority.issue(
        failed_again,
        now=invocation.lease.issued_at + timedelta(seconds=2),
    )
    loaded_again = authority.active(
        failed_again,
        requested_invocation=invocation,
        now=invocation.lease.issued_at + timedelta(seconds=3),
    )

    assert issued_again.retry_ordinal == 2
    assert loaded_again == issued_again


@pytest.mark.asyncio
async def test_filesystem_authority_accepts_failed_brief_attestation(
    tmp_path: Path,
) -> None:
    payload = invocation_payload("brief_codex", attempt=5)
    lease = payload["lease"]
    assert isinstance(lease, dict)
    lease["attempt"] = 5
    invocation = FactorySkillInvocationV1.model_validate(payload)
    replay_store = InMemoryFactorySkillReplayStore()
    claim = await replay_store.claim(invocation)
    failed = await replay_store.fail(
        claim.record,
        failure_kind="FactoryDispatchError",
    )

    issued = FilesystemFactoryHermesRetryAuthority(
        tmp_path / "authorities"
    ).issue(failed, now=invocation.lease.issued_at)

    assert issued.step.value == "brief_codex"
    assert issued.attempt == 5


@pytest.mark.asyncio
async def test_filesystem_authority_accepts_only_execute_team_evidence_repair(
    tmp_path: Path,
) -> None:
    payload = invocation_payload("execute_team", attempt=3)
    lease = payload["lease"]
    assert isinstance(lease, dict)
    lease["attempt"] = 3
    invocation = FactorySkillInvocationV1.model_validate(payload)
    replay_store = InMemoryFactorySkillReplayStore()
    claim = await replay_store.claim(invocation)
    failed = await replay_store.fail(
        claim.record,
        failure_kind="evidence_binding_failed",
    )

    issued = FilesystemFactoryHermesRetryAuthority(
        tmp_path / "authorities"
    ).issue(
        failed,
        now=invocation.lease.issued_at,
        maximum_additional_cost_usd=Decimal("0.03"),
        prior_attempt_reserve_usd=Decimal("0.40"),
        benchmark_reserve_usd=Decimal("0.20"),
        internal_total_cap_usd=Decimal("0.79"),
    )

    assert issued.reason == "evidence_binding_repaired"
    assert issued.failure_kind == "evidence_binding_failed"
    assert issued.step.value == "execute_team"
    assert issued.internal_total_cap_usd == Decimal("0.79")

    invalid_payload = invocation_payload("brief_codex", attempt=3)
    invalid_lease = invalid_payload["lease"]
    assert isinstance(invalid_lease, dict)
    invalid_lease["attempt"] = 3
    invalid_invocation = FactorySkillInvocationV1.model_validate(invalid_payload)
    invalid_replay_store = InMemoryFactorySkillReplayStore()
    invalid_claim = await invalid_replay_store.claim(invalid_invocation)
    invalid_failed = await invalid_replay_store.fail(
        invalid_claim.record,
        failure_kind="evidence_binding_failed",
    )
    with pytest.raises(FactoryDispatchError, match="not retry-eligible"):
        FilesystemFactoryHermesRetryAuthority(
            tmp_path / "invalid-authorities"
        ).issue(invalid_failed, now=invalid_invocation.lease.issued_at)
