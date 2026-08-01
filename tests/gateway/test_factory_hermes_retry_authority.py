from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agenten.agent_factory.hermes_cli import InMemoryFactorySkillReplayStore
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
