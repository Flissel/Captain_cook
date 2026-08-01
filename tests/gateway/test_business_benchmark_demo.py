from __future__ import annotations

from types import SimpleNamespace

from agenten.agent_factory.business_benchmark_demo_provisioning import (
    BusinessBenchmarkDemoProvisioner,
)
import pytest
from fastapi import HTTPException

from gateway.business_benchmark_demo import (
    GatewayBusinessBenchmarkDemoAuthority,
    GatewayBusinessBenchmarkDemoError,
    resolve_current_factory_attempts,
)
from tests.scripts.test_business_benchmark_demo_provisioning import (
    settings,
)


class RecordingStore:
    def __init__(self) -> None:
        self.requests = []

    def append(self, request, claim_token):
        self.requests.append((request, claim_token))
        return {
            "index": 7,
            "parent_index": request.parent_index,
            "block_type": request.block_type,
            "data": request.data,
            "status": request.status,
            "metadata": request.metadata,
        }


def test_gateway_authority_persists_renewal_batch_as_idempotent_root_write(
    tmp_path,
) -> None:
    renewal = BusinessBenchmarkDemoProvisioner(settings(tmp_path)).plan().teams[1]
    assert renewal.work_batch is not None
    store = RecordingStore()
    authority = object.__new__(GatewayBusinessBenchmarkDemoAuthority)
    authority._store = store

    authority.persist_work_batch(renewal.job, renewal.work_batch)
    authority.persist_work_batch(renewal.job, renewal.work_batch)

    assert len(store.requests) == 2
    for request, claim_token in store.requests:
        assert claim_token is None
        assert request.block_type == "work_batch"
        assert request.parent_index is None
        assert request.status == "pending"
        assert request.data == renewal.work_batch.model_dump(mode="json")
        assert request.metadata == {
            "factory_job_id": str(renewal.job.job_id),
            "purpose": "business_benchmark_renewal_n8n",
        }


def test_gateway_authority_rejects_work_batch_bound_to_another_factory_job(
    tmp_path,
) -> None:
    claims, renewal = BusinessBenchmarkDemoProvisioner(settings(tmp_path)).plan().teams
    assert renewal.work_batch is not None
    store = RecordingStore()
    authority = object.__new__(GatewayBusinessBenchmarkDemoAuthority)
    authority._store = store

    with pytest.raises(GatewayBusinessBenchmarkDemoError, match="factory job"):
        authority.persist_work_batch(claims.job, renewal.work_batch)

    assert store.requests == []


def test_gateway_authority_reads_existing_v3_job_for_typed_resume(tmp_path) -> None:
    expected = BusinessBenchmarkDemoProvisioner(settings(tmp_path)).plan().teams[0].job

    class Store:
        def factory_job(self, job_id):
            assert job_id == expected.job_id
            return SimpleNamespace(
                job=expected,
                projection=SimpleNamespace(phase=None, attempt=1),
            )

    authority = object.__new__(GatewayBusinessBenchmarkDemoAuthority)
    authority._store = Store()

    state = authority.resume_state(expected.job_id)
    assert state is not None
    assert state.job == expected
    assert state.phase is None
    assert state.attempt == 1


def test_gateway_authority_returns_none_only_for_missing_resume_job(tmp_path) -> None:
    expected = BusinessBenchmarkDemoProvisioner(settings(tmp_path)).plan().teams[0].job

    class Store:
        def factory_job(self, job_id):
            raise HTTPException(status_code=404, detail="factory job not found")

    authority = object.__new__(GatewayBusinessBenchmarkDemoAuthority)
    authority._store = Store()

    assert authority.resume_state(expected.job_id) is None


def test_gateway_resolves_current_attempts_for_exact_jobs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    teams = BusinessBenchmarkDemoProvisioner(settings(tmp_path)).plan().teams
    expected = {teams[0].job.job_id: 4, teams[1].job.job_id: 3}

    class Authority:
        def __init__(self, dsn: str) -> None:
            assert dsn == "mariadb://captain:test@127.0.0.1:33316/captain_test"

        def resume_state(self, job_id):
            return SimpleNamespace(attempt=expected[job_id])

    monkeypatch.setattr(
        "gateway.business_benchmark_demo.GatewayBusinessBenchmarkDemoAuthority",
        Authority,
    )

    assert resolve_current_factory_attempts(
        "mariadb://captain:test@127.0.0.1:33316/captain_test",
        tuple(expected),
    ) == expected
