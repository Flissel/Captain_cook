from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import ValidationError

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkRunReceiptV1,
)
from agenten.agent_factory.business_benchmark_execution import (
    BenchmarkExecutionPolicyV1,
    BusinessBenchmarkExecutionEnvelopeV1,
    PairedBusinessBenchmarkCoordinator,
)
from agenten.agent_factory.business_benchmark_replay import (
    BenchmarkClaimBusyError,
    BenchmarkRecoveryUncertainError,
    BenchmarkReplayConflictError,
    BusinessBenchmarkEffectClaimV1,
    BusinessBenchmarkFenceReceiptV1,
    BusinessBenchmarkPreparedEffectV1,
    BusinessBenchmarkRecoveryObservationV1,
    BusinessBenchmarkRuntimePreparationV1,
    FilesystemBusinessBenchmarkReplayStore,
    InMemoryBusinessBenchmarkReplayStore,
)
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_runtime.contracts import ArtifactRef


NOW = datetime(2026, 7, 26, 10, tzinfo=timezone.utc)
JOB_ID = UUID("00000000-0000-0000-0000-000000000501")
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000502")
CLAIM_IDS = tuple(
    UUID(f"00000000-0000-0000-0000-{number:012d}") for number in range(601, 620)
)


def artifact(label: str) -> ArtifactRef:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
    return ArtifactRef(
        uri=f"artifact://business-benchmark-test/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def suite_ref() -> PrivateHoldoutRef:
    digest = hashlib.sha256(b"private-suite").hexdigest()
    holdout_id = f"holdout-{digest[:12]}"
    return PrivateHoldoutRef(
        holdout_id=holdout_id,
        uri=f"holdout://{holdout_id}",
        sha256=digest,
    )


def benchmark_case() -> BusinessBenchmarkCaseV1:
    return BusinessBenchmarkCaseV1(
        schema="captain.business-benchmark-case.v1",
        case_id="claims-ordinary-01",
        profile_id="insurance_claims_resolution_swarm",
        category="ordinary",
        redacted_input={"test_organization_id": "test-org"},
        expected_decision="route_standard_review",
        required_rationale_fact_ids=("fact-policy-state",),
        allowed_tool_intents=("none",),
        human_handoff_required=False,
        severity="normal",
    )


def execution_policy() -> BenchmarkExecutionPolicyV1:
    return BenchmarkExecutionPolicyV1(
        schema="captain.business-benchmark-execution-policy.v1",
        model_version="approved-model-v1",
        allowed_tool_intents=("none",),
        maximum_cost_micro_usd=100,
        maximum_latency_ms=200,
        redaction_policy_version="business-redaction-v1",
        baseline_system_policy_version="single-agent-baseline-v1",
    )


def receipt(envelope: BusinessBenchmarkExecutionEnvelopeV1) -> BusinessBenchmarkRunReceiptV1:
    return BusinessBenchmarkRunReceiptV1(
        schema="captain.business-benchmark-run-receipt.v1",
        run_id=uuid5(NAMESPACE_URL, f"run:{envelope.idempotency_key}"),
        request_id=envelope.request_id,
        execution_policy_sha256=envelope.execution_policy_sha256,
        runtime_session_id=envelope.runtime_session_id,
        job_id=envelope.job_id,
        correlation_id=envelope.correlation_id,
        subject_version=envelope.subject_version,
        attempt=envelope.attempt,
        suite_ref=envelope.suite_ref,
        suite_id=envelope.suite_id,
        case_id=envelope.case.case_id,
        variant=envelope.variant,
        candidate_ref=envelope.candidate_ref,
        model_version=envelope.model_version,
        allowed_tool_intents=envelope.allowed_tool_intents,
        maximum_cost_micro_usd=envelope.maximum_cost_micro_usd,
        maximum_latency_ms=envelope.maximum_latency_ms,
        status="succeeded",
        observed_decision="route_standard_review",
        observed_rationale_fact_ids=("fact-policy-state",),
        observed_tool_intents=envelope.allowed_tool_intents,
        unsafe_tool_use=False,
        human_handoff_completed=False,
        cost_micro_usd=40,
        latency_ms=80,
        evidence_refs=(artifact(f"evidence-{envelope.variant}"),),
        completed_at=NOW,
    )


class MutableClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class FixedUUIDs:
    def __init__(self) -> None:
        self._values = iter(CLAIM_IDS)

    def __call__(self) -> UUID:
        return next(self._values)


class PreparedExecutor:
    def __init__(
        self,
        *,
        on_execute: Callable[
            [
                BusinessBenchmarkExecutionEnvelopeV1,
                BusinessBenchmarkEffectClaimV1,
                BusinessBenchmarkFenceReceiptV1,
            ],
            BusinessBenchmarkRunReceiptV1,
        ]
        | None = None,
        recovery_outcomes: dict[
            str, tuple[str, BusinessBenchmarkRunReceiptV1 | None]
        ]
        | None = None,
        on_recover: Callable[
            [
                BusinessBenchmarkPreparedEffectV1,
                BusinessBenchmarkEffectClaimV1,
                BusinessBenchmarkFenceReceiptV1,
            ],
            None,
        ]
        | None = None,
    ) -> None:
        self.prepared: list[BusinessBenchmarkExecutionEnvelopeV1] = []
        self.registered: list[BusinessBenchmarkFenceReceiptV1] = []
        self.executed: list[BusinessBenchmarkEffectClaimV1] = []
        self.recovered: list[BusinessBenchmarkPreparedEffectV1] = []
        self.recovery_observations: list[
            BusinessBenchmarkRecoveryObservationV1
        ] = []
        self._on_execute = on_execute
        self._recovery_outcomes = recovery_outcomes or {}
        self._on_recover = on_recover

    async def prepare(
        self, envelope: BusinessBenchmarkExecutionEnvelopeV1
    ) -> BusinessBenchmarkRuntimePreparationV1:
        self.prepared.append(envelope)
        return BusinessBenchmarkRuntimePreparationV1(
            schema="captain.business-benchmark-runtime-preparation.v1",
            runtime_session_id=envelope.runtime_session_id,
        )

    async def register_fence(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
    ) -> BusinessBenchmarkFenceReceiptV1:
        assert claim.prepared_effect == prepared
        value = BusinessBenchmarkFenceReceiptV1(
            schema="captain.business-benchmark-fence-receipt.v1",
            effect_id=prepared.identity.effect_id,
            runtime_session_id=prepared.runtime_session_id,
            claim_id=claim.claim_id,
            fence=claim.fence,
            registered_at=NOW,
            evidence_ref=artifact(
                f"fence-{prepared.identity.effect_id}-{claim.fence}"
            ),
        )
        self.registered.append(value)
        return value

    async def execute(
        self,
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkRunReceiptV1:
        assert fence_receipt.claim_id == claim.claim_id
        assert fence_receipt.fence == claim.fence
        self.executed.append(claim)
        if self._on_execute is not None:
            return self._on_execute(envelope, claim, fence_receipt)
        return receipt(envelope)

    async def recover(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkRecoveryObservationV1:
        self.recovered.append(prepared)
        if self._on_recover is not None:
            self._on_recover(prepared, claim, fence_receipt)
        outcome, recovered_receipt = self._recovery_outcomes.get(
            prepared.identity.variant, ("no_effect", None)
        )
        observation = BusinessBenchmarkRecoveryObservationV1(
            schema="captain.business-benchmark-recovery-observation.v1",
            effect_id=prepared.identity.effect_id,
            runtime_session_id=prepared.runtime_session_id,
            claim_id=claim.claim_id,
            fence=claim.fence,
            fence_receipt=fence_receipt,
            checked_at=NOW,
            evidence_ref=artifact(
                f"recovery-{prepared.identity.effect_id}-{claim.fence}-{outcome}"
            ),
            outcome=outcome,
            receipt=recovered_receipt,
        )
        self.recovery_observations.append(observation)
        return observation


def coordinator(
    executor: PreparedExecutor,
    store: InMemoryBusinessBenchmarkReplayStore | FilesystemBusinessBenchmarkReplayStore,
    clock: MutableClock,
    ids: FixedUUIDs,
    *,
    subject_version: int = 1,
    suite_id: str = "claims-suite-v1",
) -> PairedBusinessBenchmarkCoordinator:
    return PairedBusinessBenchmarkCoordinator(
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=subject_version,
        attempt=1,
        suite_id=suite_id,
        executor=executor,
        replay_store=store,
        clock=clock,
        claim_ttl=timedelta(minutes=5),
        claim_id_factory=ids,
    )


async def run_pair(target: PairedBusinessBenchmarkCoordinator) -> tuple[
    BusinessBenchmarkRunReceiptV1, BusinessBenchmarkRunReceiptV1
]:
    return await target.run_case_pair(
        case=benchmark_case(),
        suite_ref=suite_ref(),
        candidate_ref=artifact("candidate-package"),
        execution_policy=execution_policy(),
    )


def test_coordinator_requires_an_explicit_replay_store() -> None:
    with pytest.raises(TypeError, match="replay_store"):
        PairedBusinessBenchmarkCoordinator(
            job_id=JOB_ID,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            attempt=1,
            suite_id="claims-suite-v1",
            executor=PreparedExecutor(),
        )


@pytest.mark.asyncio
async def test_separate_claims_persist_preparation_before_each_effect(tmp_path: Path) -> None:
    store = FilesystemBusinessBenchmarkReplayStore(tmp_path)
    clock = MutableClock()

    def assert_persisted(
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkRunReceiptV1:
        assert store.prepared(claim.identity.effect_id) == claim.prepared_effect
        assert store.latest_claim(claim.identity.effect_id) == claim
        assert store.fence_receipt(claim.identity.effect_id, claim.fence) == fence_receipt
        return receipt(envelope)

    executor = PreparedExecutor(on_execute=assert_persisted)
    await run_pair(coordinator(executor, store, clock, FixedUUIDs()))

    candidate, baseline = executor.executed
    assert candidate.identity.effect_id != baseline.identity.effect_id
    assert candidate.claim_id != baseline.claim_id
    assert candidate.claim_fingerprint != baseline.claim_fingerprint
    assert candidate.fence == baseline.fence == 1
    assert candidate.identity.execution_policy_sha256 == baseline.identity.execution_policy_sha256
    assert candidate.identity.variant_policy_sha256 != baseline.identity.variant_policy_sha256
    assert candidate.prepared_effect.runtime_session_id != baseline.prepared_effect.runtime_session_id


@pytest.mark.asyncio
async def test_identical_terminal_replay_returns_exact_receipt_bytes_without_executor_calls(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = FilesystemBusinessBenchmarkReplayStore(tmp_path)
    first_executor = PreparedExecutor()
    first = await run_pair(coordinator(first_executor, store, clock, FixedUUIDs()))
    before = tuple(store.receipt_bytes(claim.identity.effect_id) for claim in first_executor.executed)

    replay_executor = PreparedExecutor()
    replayed = await run_pair(coordinator(replay_executor, store, clock, FixedUUIDs()))

    assert replayed == first
    assert replay_executor.prepared == []
    assert replay_executor.registered == []
    assert replay_executor.executed == []
    assert replay_executor.recovered == []
    assert tuple(store.receipt_bytes(claim.identity.effect_id) for claim in first_executor.executed) == before


@pytest.mark.asyncio
async def test_active_pending_claim_blocks_duplicate_without_preparing_again() -> None:
    clock = MutableClock()
    store = InMemoryBusinessBenchmarkReplayStore()

    def crash(
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkRunReceiptV1:
        assert fence_receipt.fence == claim.fence
        raise RuntimeError("simulated interruption after effect boundary")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        await run_pair(coordinator(PreparedExecutor(on_execute=crash), store, clock, FixedUUIDs()))

    duplicate = PreparedExecutor()
    with pytest.raises(BenchmarkClaimBusyError, match="active"):
        await run_pair(coordinator(duplicate, store, clock, FixedUUIDs()))
    assert duplicate.prepared == []
    assert duplicate.executed == []
    assert duplicate.recovered == []


@pytest.mark.asyncio
async def test_expired_claim_uses_higher_fence_and_recovers_terminal_without_reexecution() -> None:
    clock = MutableClock()
    store = InMemoryBusinessBenchmarkReplayStore()
    captured: dict[str, tuple[BusinessBenchmarkExecutionEnvelopeV1, BusinessBenchmarkEffectClaimV1]] = {}

    def crash(
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkRunReceiptV1:
        assert fence_receipt.fence == claim.fence
        captured[envelope.variant] = (envelope, claim)
        raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        await run_pair(coordinator(PreparedExecutor(on_execute=crash), store, clock, FixedUUIDs()))

    old_envelope, old_claim = captured["candidate"]
    clock.now += timedelta(minutes=6)
    recovered_receipt = receipt(old_envelope)

    def assert_registered_before_recovery(
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> None:
        assert prepared == claim.prepared_effect
        assert store.fence_receipt(claim.identity.effect_id, claim.fence) == fence_receipt

    executor = PreparedExecutor(
        recovery_outcomes={"candidate": ("terminal", recovered_receipt)},
        on_recover=assert_registered_before_recovery,
    )

    candidate, _ = await run_pair(coordinator(executor, store, clock, FixedUUIDs()))

    assert candidate == recovered_receipt
    assert [item.identity.variant for item in executor.recovered] == ["candidate"]
    assert [item.identity.variant for item in executor.executed] == ["single_agent_baseline"]
    assert store.latest_claim(old_claim.identity.effect_id).fence > old_claim.fence
    new_claim = store.latest_claim(old_claim.identity.effect_id)
    assert store.recovery_observation(
        new_claim.identity.effect_id, new_claim.fence
    ) == executor.recovery_observations[0]


@pytest.mark.asyncio
async def test_uncertain_recovery_fails_closed_and_never_executes() -> None:
    clock = MutableClock()
    store = InMemoryBusinessBenchmarkReplayStore()

    def crash(
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkRunReceiptV1:
        assert fence_receipt.fence == claim.fence
        raise RuntimeError("simulated interruption")

    first = PreparedExecutor(on_execute=crash)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await run_pair(coordinator(first, store, clock, FixedUUIDs()))
    pending = first.executed[0]
    clock.now += timedelta(minutes=6)
    retry = PreparedExecutor(recovery_outcomes={"candidate": ("uncertain", None)})

    with pytest.raises(BenchmarkRecoveryUncertainError, match="uncertain"):
        await run_pair(coordinator(retry, store, clock, FixedUUIDs()))
    assert retry.executed == []
    assert store.latest_claim(pending.identity.effect_id).fence > pending.fence


@pytest.mark.asyncio
async def test_candidate_complete_filesystem_restart_executes_baseline_once(tmp_path: Path) -> None:
    clock = MutableClock()
    first_store = FilesystemBusinessBenchmarkReplayStore(tmp_path)

    class StopBeforeBaselineExecutor(PreparedExecutor):
        async def prepare(
            self, envelope: BusinessBenchmarkExecutionEnvelopeV1
        ) -> BusinessBenchmarkRuntimePreparationV1:
            if envelope.variant == "single_agent_baseline":
                raise RuntimeError("restart between variants")
            return await super().prepare(envelope)

    first_executor = StopBeforeBaselineExecutor()
    with pytest.raises(RuntimeError, match="between variants"):
        await run_pair(coordinator(first_executor, first_store, clock, FixedUUIDs()))
    assert [claim.identity.variant for claim in first_executor.executed] == ["candidate"]

    restarted_store = FilesystemBusinessBenchmarkReplayStore(tmp_path)
    restarted_executor = PreparedExecutor()
    candidate, baseline = await run_pair(
        coordinator(restarted_executor, restarted_store, clock, FixedUUIDs())
    )

    assert candidate.variant == "candidate"
    assert baseline.variant == "single_agent_baseline"
    assert [claim.identity.variant for claim in restarted_executor.executed] == [
        "single_agent_baseline"
    ]
    final_executor = PreparedExecutor()
    assert await run_pair(
        coordinator(final_executor, FilesystemBusinessBenchmarkReplayStore(tmp_path), clock, FixedUUIDs())
    ) == (candidate, baseline)
    assert final_executor.executed == []


@pytest.mark.asyncio
async def test_changed_terminal_receipt_is_a_conflict() -> None:
    clock = MutableClock()
    store = InMemoryBusinessBenchmarkReplayStore()
    executor = PreparedExecutor()
    await run_pair(coordinator(executor, store, clock, FixedUUIDs()))
    candidate_claim = executor.executed[0]
    changed = receipt(executor.prepared[0]).model_copy(update={"cost_micro_usd": 41})

    with pytest.raises(BenchmarkReplayConflictError, match="different receipt"):
        store.complete(
            candidate_claim,
            executor.registered[0],
            changed,
        )


@pytest.mark.asyncio
async def test_in_memory_store_rejects_secret_like_prepared_runtime_identity() -> None:
    clock = MutableClock()
    populated = InMemoryBusinessBenchmarkReplayStore()
    executor = PreparedExecutor()
    await run_pair(coordinator(executor, populated, clock, FixedUUIDs()))
    prepared = executor.executed[0].prepared_effect.model_copy(
        update={"runtime_session_id": "sk-abcdefgh"}
    )
    store = InMemoryBusinessBenchmarkReplayStore()

    with pytest.raises(ValueError, match="secret-like"):
        store.claim(
            prepared,
            claim_id=CLAIM_IDS[0],
            acquired_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )


@pytest.mark.asyncio
async def test_filesystem_reconstruction_rejects_noncanonical_terminal_bytes(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = FilesystemBusinessBenchmarkReplayStore(tmp_path)
    executor = PreparedExecutor()
    await run_pair(coordinator(executor, store, clock, FixedUUIDs()))
    effect_id = executor.executed[0].identity.effect_id
    receipt_path = tmp_path / "receipts" / f"{effect_id}.json"
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with pytest.raises(BenchmarkReplayConflictError, match="canonical"):
        FilesystemBusinessBenchmarkReplayStore(tmp_path).snapshot(
            executor.executed[0].identity
        )


@pytest.mark.asyncio
async def test_subject_version_changes_durable_effect_identity() -> None:
    clock = MutableClock()
    first_executor = PreparedExecutor()
    await run_pair(
        coordinator(first_executor, InMemoryBusinessBenchmarkReplayStore(), clock, FixedUUIDs())
    )
    second_executor = PreparedExecutor()
    await run_pair(
        coordinator(
            second_executor,
            InMemoryBusinessBenchmarkReplayStore(),
            clock,
            FixedUUIDs(),
            subject_version=2,
        )
    )

    assert [claim.identity.effect_id for claim in first_executor.executed] != [
        claim.identity.effect_id for claim in second_executor.executed
    ]
    assert [envelope.request_id for envelope in first_executor.prepared] != [
        envelope.request_id for envelope in second_executor.prepared
    ]


@pytest.mark.asyncio
async def test_runtime_address_uses_full_identity_including_suite_id() -> None:
    clock = MutableClock()
    first_executor = PreparedExecutor()
    await run_pair(
        coordinator(
            first_executor,
            InMemoryBusinessBenchmarkReplayStore(),
            clock,
            FixedUUIDs(),
            suite_id="claims-suite-v1",
        )
    )
    second_executor = PreparedExecutor()
    await run_pair(
        coordinator(
            second_executor,
            InMemoryBusinessBenchmarkReplayStore(),
            clock,
            FixedUUIDs(),
            suite_id="claims-suite-renamed-v1",
        )
    )

    first = first_executor.prepared[0]
    second = second_executor.prepared[0]
    assert first.request_id != second.request_id
    assert first.runtime_session_id != second.runtime_session_id
    assert first.idempotency_key in first.runtime_session_id
    assert second.idempotency_key in second.runtime_session_id


@pytest.mark.asyncio
async def test_no_effect_recovery_requires_content_addressed_proof() -> None:
    clock = MutableClock()
    executor = PreparedExecutor()
    await run_pair(
        coordinator(
            executor,
            InMemoryBusinessBenchmarkReplayStore(),
            clock,
            FixedUUIDs(),
        )
    )
    claim = executor.executed[0]
    fence_receipt = executor.registered[0]

    with pytest.raises(ValidationError, match="evidence_ref"):
        BusinessBenchmarkRecoveryObservationV1(
            schema="captain.business-benchmark-recovery-observation.v1",
            effect_id=claim.identity.effect_id,
            runtime_session_id=claim.prepared_effect.runtime_session_id,
            claim_id=claim.claim_id,
            fence=claim.fence,
            fence_receipt=fence_receipt,
            checked_at=NOW,
            outcome="no_effect",
        )


@pytest.mark.asyncio
async def test_higher_provider_fence_rejects_overlapping_stale_executor_before_effect() -> None:
    clock = MutableClock()
    store = InMemoryBusinessBenchmarkReplayStore()

    class StaleProviderFenceError(RuntimeError):
        pass

    class OverlapExecutor(PreparedExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.maximum_fence: dict[str, int] = {}
            self.old_paused = asyncio.Event()
            self.old_resume = asyncio.Event()
            self.completed_effects: list[tuple[str, int]] = []
            self.stale_attempts: list[tuple[str, int]] = []

        async def register_fence(
            self,
            prepared: BusinessBenchmarkPreparedEffectV1,
            claim: BusinessBenchmarkEffectClaimV1,
        ) -> BusinessBenchmarkFenceReceiptV1:
            current = self.maximum_fence.get(prepared.identity.effect_id, 0)
            if claim.fence <= current:
                raise StaleProviderFenceError("provider rejected stale fence registration")
            self.maximum_fence[prepared.identity.effect_id] = claim.fence
            return await super().register_fence(prepared, claim)

        async def execute(
            self,
            envelope: BusinessBenchmarkExecutionEnvelopeV1,
            claim: BusinessBenchmarkEffectClaimV1,
            fence_receipt: BusinessBenchmarkFenceReceiptV1,
        ) -> BusinessBenchmarkRunReceiptV1:
            if envelope.variant == "candidate" and claim.fence == 1:
                self.old_paused.set()
                await self.old_resume.wait()
            current = self.maximum_fence[claim.identity.effect_id]
            if claim.fence != current:
                self.stale_attempts.append((claim.identity.variant, claim.fence))
                raise StaleProviderFenceError("provider rejected stale execute")
            self.executed.append(claim)
            self.completed_effects.append((claim.identity.variant, claim.fence))
            return receipt(envelope)

        async def recover(
            self,
            prepared: BusinessBenchmarkPreparedEffectV1,
            claim: BusinessBenchmarkEffectClaimV1,
            fence_receipt: BusinessBenchmarkFenceReceiptV1,
        ) -> BusinessBenchmarkRecoveryObservationV1:
            if claim.fence != self.maximum_fence[prepared.identity.effect_id]:
                raise StaleProviderFenceError("provider rejected stale recovery")
            if prepared.identity.variant == "candidate" and claim.fence == 2:
                self.old_resume.set()
                await asyncio.sleep(0)
            return await super().recover(prepared, claim, fence_receipt)

    executor = OverlapExecutor()
    old_run = asyncio.create_task(
        run_pair(coordinator(executor, store, clock, FixedUUIDs()))
    )
    await executor.old_paused.wait()
    clock.now += timedelta(minutes=6)

    candidate, baseline = await run_pair(
        coordinator(executor, store, clock, FixedUUIDs())
    )

    with pytest.raises(StaleProviderFenceError, match="stale execute"):
        await old_run
    assert candidate.variant == "candidate"
    assert baseline.variant == "single_agent_baseline"
    assert executor.stale_attempts == [("candidate", 1)]
    assert [fence for variant, fence in executor.completed_effects if variant == "candidate"] == [2]


@pytest.mark.asyncio
async def test_filesystem_claim_contention_has_one_winner_and_one_active_block(
    tmp_path: Path,
) -> None:
    source_executor = PreparedExecutor()
    await run_pair(
        coordinator(
            source_executor,
            InMemoryBusinessBenchmarkReplayStore(),
            MutableClock(),
            FixedUUIDs(),
        )
    )
    prepared = source_executor.executed[0].prepared_effect
    store = FilesystemBusinessBenchmarkReplayStore(tmp_path)
    barrier = threading.Barrier(2)

    def contend(claim_id: UUID) -> str:
        barrier.wait(timeout=5)
        try:
            result = store.claim(
                prepared,
                claim_id=claim_id,
                acquired_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
            )
        except BenchmarkClaimBusyError:
            return "busy"
        assert result.acquired
        return "acquired"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(
            future.result(timeout=10)
            for future in (
                pool.submit(contend, CLAIM_IDS[0]),
                pool.submit(contend, CLAIM_IDS[1]),
            )
        )

    assert outcomes == ["acquired", "busy"]
