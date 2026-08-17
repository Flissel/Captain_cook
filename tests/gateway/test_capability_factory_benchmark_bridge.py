from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

from agenten.agent_factory.contracts import FactoryLease, FactoryPhase, FactoryRole
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from gateway.capability_factory_benchmark_bridge import AssemblyOwnedGenericBenchmark
from gateway.store import GatewayStore
from agenten.agent_factory.state_machine import FactoryLifecycleStatus, apply_block
from tests.agent_factory.test_generic_benchmark_quality import NOW, _quality
from tests.agent_factory.test_state_machine import job_v3


def _request() -> FactoryDispatch:
    job = job_v3()
    lease = FactoryLease.model_construct(
        lease_id="quality-lease",
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=1,
        role=FactoryRole.QUALITY_WARDEN,
        issued_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
    )
    return FactoryDispatch(
        job=job,
        action=FactoryAction(
            kind=FactoryActionKind.DISPATCH_QUALITY_WARDEN,
            attempt=1,
            job_id=job.job_id,
        ),
        role=FactoryRole.QUALITY_WARDEN,
        lease=lease,
    )


def test_assembly_benchmark_materializes_exact_durable_quality_block() -> None:
    quality = _quality()
    calls: list[tuple[object, int]] = []
    store = SimpleNamespace(
        generic_benchmark_quality_evidence=lambda job_id, attempt: (
            calls.append((job_id, attempt)) or quality
        )
    )
    bridge = AssemblyOwnedGenericBenchmark(
        gateway_store=store,
        clock=lambda: NOW,
    )

    block = asyncio.run(bridge.dispatch(_request()))

    assert calls == [(job_v3().job_id, 1)]
    assert block.phase is FactoryPhase.QUALITY_REVIEWED
    assert block.generic_benchmark_quality == quality
    assert block.assertion_ids == job_v3().acceptance_assertion_ids
    assert block.evidence_refs == (quality.summary_ref, *quality.evaluation_refs)


def test_assembly_benchmark_missing_provisioning_stays_incomplete() -> None:
    store = SimpleNamespace(
        generic_benchmark_quality_evidence=lambda *_: None,
    )
    bridge = AssemblyOwnedGenericBenchmark(
        gateway_store=store,
        clock=lambda: NOW,
    )

    with pytest.raises(FactoryDispatchError, match="unavailable"):
        asyncio.run(bridge.dispatch(_request()))


def test_gateway_readback_materializes_15_job_specific_public_cases() -> None:
    quality = _quality()
    job = job_v3()
    cases = [
        {
            "case_id": quality.case_ids[index],
            "case_digest": quality.case_digests[index],
            "category": quality.categories[index],
            "assertion_ids": list(job.acceptance_assertion_ids),
            "team_evaluation_ref": quality.evaluation_refs[index * 2].model_dump(
                mode="json"
            ),
            "baseline_evaluation_ref": quality.evaluation_refs[index * 2 + 1].model_dump(
                mode="json"
            ),
        }
        for index in range(15)
    ]
    payload = {
        "schema": "captain.capability-generic-benchmark-summary.v1",
        "job_id": str(job.job_id),
        "correlation_id": str(job.correlation_id),
        "suite_ref": quality.suite_ref.model_dump(mode="json"),
        "suite_digest": quality.suite_ref.sha256,
        "case_receipts": cases,
        "passed": True,
    }
    row = {
        "suite_sha256": quality.suite_ref.sha256,
        "correlation_id": str(job.correlation_id),
        "summary_sha256": quality.summary_ref.sha256,
        "payload": json.dumps(payload),
    }

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, *_):
            return None

        def fetchall(self):
            return (row,)

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def cursor(self):
            return Cursor()

    store = object.__new__(GatewayStore)
    store.storage = SimpleNamespace(transaction=lambda: Transaction())
    store._runtime_block_by_json_value = lambda *_args, **_kwargs: {
        "data": job.model_dump(mode="json", by_alias=True)
    }

    materialized = store.generic_benchmark_quality_evidence(job.job_id, 1)

    assert materialized is not None
    assert materialized.case_ids == quality.case_ids
    assert materialized.acceptance_assertion_ids == job.acceptance_assertion_ids
    assert materialized.summary_ref == quality.summary_ref


def test_shipped_gateway_promotion_is_atomic_immutable_and_replay_stable() -> None:
    job = job_v3()
    quality_block = asyncio.run(AssemblyOwnedGenericBenchmark(
        gateway_store=SimpleNamespace(
            generic_benchmark_quality_evidence=lambda *_: _quality()
        ),
        clock=lambda: NOW,
    ).dispatch(_request()))
    from agenten.agent_factory.state_machine import FactoryProjection

    before = FactoryProjection(
        job=job,
        status=FactoryLifecycleStatus.RUNNING,
        phase=FactoryPhase.REAL_CASE_EVIDENCE,
        attempt=1,
        observed_assertion_ids=job.acceptance_assertion_ids,
    )
    quality_projection = apply_block(before, quality_block)
    aggregate = {
        "case_count": 15,
        "team_quality_count": 15,
        "baseline_quality_count": 12,
        "unsafe_team_count": 0,
        "mandatory_handoff_misses": 0,
        "team_cost_usd": "1.0",
        "baseline_cost_usd": "2.0",
        "team_latency_ms": 10,
        "baseline_latency_ms": 20,
    }
    summary = {
        "schema": "captain.capability-generic-benchmark-summary.v1",
        "job_id": str(job.job_id),
        "correlation_id": str(job.correlation_id),
        "suite_digest": _quality().suite_ref.sha256,
        "passed": True,
        "aggregate": aggregate,
    }
    initial = {
        "projection": quality_projection,
        "blocks": [quality_block],
        "promotions": {},
        "summary": {
            "summary_sha256": _quality().summary_ref.sha256,
            "correlation_id": str(job.correlation_id),
            "payload": json.dumps(summary),
        },
    }

    class AtomicCursor:
        def __init__(self, state):
            self.state = state
            self.result = None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, parameters=()):
            normalized = " ".join(query.split())
            if normalized.startswith("SELECT summary_sha256"):
                self.result = self.state["summary"]
            elif normalized.startswith("SELECT suite_sha256"):
                self.result = self.state["promotions"].get(str(job.job_id))
            elif normalized.startswith("SELECT projection_event"):
                expected_event_id = str(parameters[0])
                self.result = next(
                    (
                        {"projection_event": item["projection_event"]}
                        for item in self.state["promotions"].values()
                        if item["event_id"] == expected_event_id
                    ),
                    None,
                )
            elif normalized.startswith("INSERT INTO capability_generic_promotions"):
                self.state["promotions"][str(job.job_id)] = {
                    "event_id": parameters[0],
                    "suite_sha256": parameters[2],
                    "summary_sha256": parameters[3],
                    "projection_event": parameters[4],
                }

        def fetchone(self):
            return self.result

    class AtomicConnection:
        def __init__(self, state):
            self.state = state

        def cursor(self):
            return AtomicCursor(self.state)

    class AtomicStorage:
        def __init__(self):
            self.committed = initial

        @contextmanager
        def transaction(self):
            staged = deepcopy(self.committed)
            connection = AtomicConnection(staged)
            yield connection
            self.committed = staged

    storage = AtomicStorage()
    store = object.__new__(GatewayStore)
    store.storage = storage
    store._runtime_block_by_json_value = lambda *_a, **_k: {
        "index": 1, "data": job.model_dump(mode="json", by_alias=True)
    }
    store._factory_projection = lambda cursor, _job: cursor.state["projection"]
    store._factory_blocks = lambda cursor, _job_id, **_k: tuple(cursor.state["blocks"])
    store._factory_leases = lambda *_a, **_k: ()
    store._factory_block_context = lambda *_a, **_k: (None, None, None, None, None)
    store._next_index = lambda cursor: len(cursor.state["blocks"]) + 2
    store._new_block = lambda _cursor, **values: values

    def insert(cursor, value):
        if value["block_type"] == "agent_factory_block":
            block = __import__(
                "agenten.agent_factory.contracts", fromlist=["FactoryEvidenceBlock"]
            ).FactoryEvidenceBlock.model_validate(value["data"])
            cursor.state["blocks"].append(block)
            cursor.state["projection"] = apply_block(
                cursor.state["projection"], block
            )

    store._insert = insert

    first = store.record_generic_capability_promotion(
        job_id=job.job_id,
        suite_sha256=_quality().suite_ref.sha256,
        occurred_at=NOW,
    )
    replay = store.record_generic_capability_promotion(
        job_id=job.job_id,
        suite_sha256=_quality().suite_ref.sha256,
        occurred_at=NOW,
    )

    assert replay == first
    readback = store.generic_capability_promotion(
        UUID(first["event_id"])
    )
    assert readback == first
    assert storage.committed["projection"].status is FactoryLifecycleStatus.READY_TO_USE
    assert sum(
        block.phase is FactoryPhase.CAPABILITY_PROMOTED
        for block in storage.committed["blocks"]
    ) == 1
    with pytest.raises(HTTPException, match="generic promotion conflicts"):
        store.record_generic_capability_promotion(
            job_id=job.job_id,
            suite_sha256=_quality().suite_ref.sha256,
            occurred_at=NOW + timedelta(seconds=1),
        )
    assert sum(
        block.phase is FactoryPhase.CAPABILITY_PROMOTED
        for block in storage.committed["blocks"]
    ) == 1
