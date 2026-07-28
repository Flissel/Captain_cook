from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkRunReceiptV1,
    canonical_business_benchmark_model_bytes,
)
from agenten.agent_factory.business_benchmark_handoff import (
    CaptainHumanReviewRequestV1,
    CaptainHumanReviewReceiptV1,
    validate_captain_human_review_receipt,
)
from agenten.agent_factory.business_benchmark_provider_state import (
    BusinessBenchmarkProviderBindingV1,
    BusinessBenchmarkProviderStateV1,
    BusinessBenchmarkProviderStateConflictError,
    BusinessBenchmarkProviderStateStore,
    BusinessBenchmarkProviderStateUncertainError,
    BusinessBenchmarkStaleProviderFenceError,
    default_business_benchmark_provider_state_root,
)
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_runtime.contracts import ArtifactRef


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
JOB_ID = UUID("00000000-0000-0000-0000-000000000401")
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000402")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000000403")
CLAIM_ID = UUID("00000000-0000-0000-0000-000000000404")


def state_root(tmp_path: Path) -> Path:
    return tmp_path / ".captain-cook" / "private" / "business-benchmark-provider-state"


def artifact(label: str) -> ArtifactRef:
    digest = label.encode("utf-8").hex().ljust(64, "0")[:64]
    return ArtifactRef(
        uri=f"artifact://benchmark/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def binding(*, fence: int = 1, model_version: str = "gpt-5-business-v1") -> BusinessBenchmarkProviderBindingV1:
    return BusinessBenchmarkProviderBindingV1(
        schema="captain.business-benchmark-provider-binding.v1",
        effect_id="a" * 64,
        runtime_session_id="benchmark-session-401",
        claim_id=CLAIM_ID if fence == 1 else UUID(int=CLAIM_ID.int + fence - 1),
        fence=fence,
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        attempt=2,
        request_id=REQUEST_ID,
        case_sha256="b" * 64,
        variant="candidate",
        model_version=model_version,
    )


def receipt(*, cost_micro_usd: int = 17) -> BusinessBenchmarkRunReceiptV1:
    return BusinessBenchmarkRunReceiptV1(
        schema="captain.business-benchmark-run-receipt.v1",
        run_id=UUID("00000000-0000-0000-0000-000000000405"),
        request_id=REQUEST_ID,
        execution_policy_sha256="c" * 64,
        runtime_session_id="benchmark-session-401",
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=3,
        attempt=2,
        suite_ref=PrivateHoldoutRef(
            holdout_id="holdout-123456abcdef",
            uri="holdout://holdout-123456abcdef",
            sha256="d" * 64,
        ),
        suite_id="claims-suite-v2",
        case_id="claims-case-01",
        case_sha256="b" * 64,
        variant="candidate",
        candidate_ref=artifact("candidate"),
        model_version="gpt-5-business-v1",
        allowed_tool_intents=(),
        maximum_cost_micro_usd=100,
        maximum_latency_ms=10_000,
        status="succeeded",
        observed_decision="route_standard_review",
        observed_rationale_fact_ids=("fact-policy-state",),
        observed_tool_intents=(),
        unsafe_tool_use=False,
        human_handoff_completed=False,
        cost_micro_usd=cost_micro_usd,
        latency_ms=41,
        evidence_refs=(artifact("provider-evidence"),),
        completed_at=NOW,
    )


def test_crash_before_dispatch_recovers_as_no_effect(tmp_path: Path) -> None:
    store = BusinessBenchmarkProviderStateStore(state_root(tmp_path))
    store.register_fence(binding(), registered_at=NOW)

    recovery = BusinessBenchmarkProviderStateStore(state_root(tmp_path)).recover(binding())

    assert recovery.outcome == "no_effect"
    assert recovery.receipt is None


def test_crash_after_dispatch_recovers_uncertain_and_blocks_repeat_dispatch(tmp_path: Path) -> None:
    store = BusinessBenchmarkProviderStateStore(state_root(tmp_path))
    store.register_fence(binding(), registered_at=NOW)
    store.begin_dispatch(binding(), started_at=NOW)

    recovery = BusinessBenchmarkProviderStateStore(state_root(tmp_path)).recover(binding())

    assert recovery.outcome == "uncertain"
    with pytest.raises(BusinessBenchmarkProviderStateUncertainError, match="uncertain"):
        store.begin_dispatch(binding(), started_at=NOW)


def test_provider_terminal_without_finalize_remains_uncertain(tmp_path: Path) -> None:
    store = BusinessBenchmarkProviderStateStore(state_root(tmp_path))
    store.register_fence(binding(), registered_at=NOW)
    store.begin_dispatch(binding(), started_at=NOW)
    store.record_provider_terminal(binding(), receipt(), recorded_at=NOW)

    assert (
        BusinessBenchmarkProviderStateStore(state_root(tmp_path)).recover(binding()).outcome
        == "uncertain"
    )


def test_finalized_restart_returns_exact_stored_receipt_bytes(tmp_path: Path) -> None:
    store = BusinessBenchmarkProviderStateStore(state_root(tmp_path))
    store.register_fence(binding(), registered_at=NOW)
    store.begin_dispatch(binding(), started_at=NOW)
    store.record_provider_terminal(binding(), receipt(), recorded_at=NOW)
    expected = store.finalize(binding(), receipt(), finalized_at=NOW)

    restarted = BusinessBenchmarkProviderStateStore(state_root(tmp_path))
    recovery = restarted.recover(binding())

    assert recovery.outcome == "terminal"
    assert recovery.receipt == expected
    receipt_path = next(tmp_path.rglob("receipt.json"))
    assert receipt_path.read_bytes() == canonical_business_benchmark_model_bytes(expected)


def test_largest_fence_wins_and_stale_fence_fails_closed(tmp_path: Path) -> None:
    store = BusinessBenchmarkProviderStateStore(state_root(tmp_path))
    store.register_fence(binding(fence=1), registered_at=NOW)
    store.register_fence(binding(fence=2), registered_at=NOW)

    assert store.assert_current(binding(fence=2)).binding.fence == 2
    with pytest.raises(BusinessBenchmarkStaleProviderFenceError, match="stale"):
        store.assert_current(binding(fence=1))
    with pytest.raises(BusinessBenchmarkStaleProviderFenceError, match="stale"):
        store.begin_dispatch(binding(fence=1), started_at=NOW)


def test_same_stage_with_changed_bytes_is_a_conflict(tmp_path: Path) -> None:
    store = BusinessBenchmarkProviderStateStore(state_root(tmp_path))
    store.register_fence(binding(), registered_at=NOW)

    with pytest.raises(BusinessBenchmarkProviderStateConflictError, match="different content"):
        store.register_fence(
            binding(model_version="gpt-5-business-v2"),
            registered_at=NOW,
        )


def test_restart_rejects_a_self_consistent_state_with_a_forged_chain_digest(
    tmp_path: Path,
) -> None:
    store = BusinessBenchmarkProviderStateStore(state_root(tmp_path))
    store.register_fence(binding(), registered_at=NOW)
    store.begin_dispatch(binding(), started_at=NOW)
    forged = BusinessBenchmarkProviderStateV1.create(
        binding=binding(),
        stage="dispatching",
        recorded_at=NOW,
        previous_state_sha256="e" * 64,
    )
    path = next(tmp_path.rglob("10-dispatching.json"))
    path.write_text(
        json.dumps(
            forged.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    with pytest.raises(BusinessBenchmarkProviderStateConflictError, match="chain"):
        BusinessBenchmarkProviderStateStore(state_root(tmp_path)).recover(binding())


def test_finalized_receipt_cannot_change(tmp_path: Path) -> None:
    store = BusinessBenchmarkProviderStateStore(state_root(tmp_path))
    store.register_fence(binding(), registered_at=NOW)
    store.begin_dispatch(binding(), started_at=NOW)
    store.record_provider_terminal(binding(), receipt(), recorded_at=NOW)
    store.finalize(binding(), receipt(), finalized_at=NOW)

    with pytest.raises(BusinessBenchmarkProviderStateConflictError, match="different receipt"):
        store.finalize(binding(), receipt(cost_micro_usd=18), finalized_at=NOW)


def test_provider_state_never_persists_private_case_or_provider_fields(tmp_path: Path) -> None:
    store = BusinessBenchmarkProviderStateStore(state_root(tmp_path))
    store.register_fence(binding(), registered_at=NOW)
    store.begin_dispatch(binding(), started_at=NOW)
    store.record_provider_terminal(binding(), receipt(), recorded_at=NOW)
    store.finalize(binding(), receipt(), finalized_at=NOW)

    persisted = b"\n".join(path.read_bytes() for path in tmp_path.rglob("*.json"))
    lowered = persisted.lower()
    assert b"case_body" not in lowered
    assert b"prompt" not in lowered
    assert b"transcript" not in lowered
    assert b"provider_output" not in lowered


def test_provider_state_root_must_be_captain_owned_and_gitignored(
    tmp_path: Path,
) -> None:
    assert default_business_benchmark_provider_state_root(tmp_path) == state_root(tmp_path)
    with pytest.raises(ValueError, match=".captain-cook"):
        BusinessBenchmarkProviderStateStore(tmp_path / "arbitrary-provider-state")


def test_human_review_receipt_requires_captain_authority_and_exact_fence() -> None:
    request = CaptainHumanReviewRequestV1(
        schema="captain.business-benchmark-human-review-request.v1",
        review_request_id=UUID("00000000-0000-0000-0000-000000000406"),
        binding=binding(),
        reason_code="mandatory-escalation",
        requested_at=NOW,
    )
    accepted = CaptainHumanReviewReceiptV1(
        schema="captain.business-benchmark-human-review-receipt.v1",
        review_request_id=request.review_request_id,
        binding=binding(),
        authority="captain_human_review",
        status="accepted",
        evidence_ref=artifact("human-review"),
        recorded_at=NOW,
    )

    assert validate_captain_human_review_receipt(request, accepted) == accepted
    with pytest.raises(ValueError, match="authority"):
        CaptainHumanReviewReceiptV1.model_validate(
            {**accepted.model_dump(mode="json", by_alias=True), "authority": "autogen_handoff"}
        )
    with pytest.raises(ValueError, match="exact effect and fence"):
        validate_captain_human_review_receipt(
            request,
            accepted.model_copy(update={"binding": binding(fence=2)}),
        )
    with pytest.raises(ValidationError, match="authority"):
        validate_captain_human_review_receipt(
            request,
            accepted.model_copy(update={"authority": "autogen_handoff"}),
        )
    with pytest.raises(ValidationError, match="status"):
        validate_captain_human_review_receipt(
            request,
            accepted.model_copy(update={"status": "transferred"}),
        )
    with pytest.raises(ValueError, match="review request"):
        validate_captain_human_review_receipt(
            request,
            accepted.model_copy(
                update={"review_request_id": UUID(int=request.review_request_id.int + 1)}
            ),
        )
    with pytest.raises(ValueError, match="before the request"):
        validate_captain_human_review_receipt(
            request,
            accepted.model_copy(update={"recorded_at": NOW - timedelta(seconds=1)}),
        )
