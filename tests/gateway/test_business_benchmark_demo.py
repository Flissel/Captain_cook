from __future__ import annotations

from agenten.agent_factory.business_benchmark_demo_provisioning import (
    BusinessBenchmarkDemoProvisioner,
)
import pytest

from gateway.business_benchmark_demo import (
    GatewayBusinessBenchmarkDemoAuthority,
    GatewayBusinessBenchmarkDemoError,
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
