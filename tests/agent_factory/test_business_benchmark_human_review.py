from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from agenten.agent_factory.business_benchmark_handoff import (
    CaptainHumanReviewRequestV1,
)
from agenten.agent_factory.business_benchmark_human_review import (
    CaptainHumanReviewConflictError,
    CaptainHumanReviewStore,
    CaptainHumanReviewStaleFenceError,
    default_captain_human_review_root,
)
from agenten.agent_factory.business_benchmark_provider_state import (
    BusinessBenchmarkProviderBindingV1,
)
from agenten.agent_runtime.contracts import ArtifactRef


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
JOB_ID = UUID("00000000-0000-0000-0000-000000000501")
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000502")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000000503")
CLAIM_ID = UUID("00000000-0000-0000-0000-000000000504")
REVIEW_ID = UUID("00000000-0000-0000-0000-000000000505")


def review_root(tmp_path: Path) -> Path:
    return tmp_path / ".captain-cook" / "private" / "business-benchmark-human-review"


def binding(
    *,
    fence: int = 1,
    case_sha256: str = "b" * 64,
    variant: str = "candidate",
) -> BusinessBenchmarkProviderBindingV1:
    return BusinessBenchmarkProviderBindingV1(
        schema="captain.business-benchmark-provider-binding.v1",
        effect_id="a" * 64,
        runtime_session_id="benchmark-session-501",
        claim_id=CLAIM_ID if fence == 1 else UUID(int=CLAIM_ID.int + fence - 1),
        fence=fence,
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        attempt=2,
        request_id=REQUEST_ID,
        case_sha256=case_sha256,
        variant=variant,
        model_version="gpt-5-business-v1",
    )


def request(
    *,
    selected_binding: BusinessBenchmarkProviderBindingV1 | None = None,
    review_request_id: UUID = REVIEW_ID,
    requested_at: datetime = NOW,
) -> CaptainHumanReviewRequestV1:
    return CaptainHumanReviewRequestV1(
        schema="captain.business-benchmark-human-review-request.v1",
        review_request_id=review_request_id,
        binding=selected_binding or binding(),
        reason_code="mandatory-human-review",
        requested_at=requested_at,
    )


def evidence(label: str) -> ArtifactRef:
    digest = label.encode("utf-8").hex().ljust(64, "0")[:64]
    return ArtifactRef(
        uri=f"artifact://captain-human-review/{digest}",
        sha256=digest,
        media_type="application/json",
    )


@pytest.mark.asyncio
async def test_request_review_is_durable_accepted_and_identically_replayable(
    tmp_path: Path,
) -> None:
    expected_request = request()
    first = await CaptainHumanReviewStore(review_root(tmp_path)).request_review(
        expected_request
    )
    restarted = CaptainHumanReviewStore(review_root(tmp_path))
    replay = await restarted.request_review(expected_request)

    assert first == replay
    assert replay.status == "accepted"
    assert replay.authority == "captain_human_review"
    assert replay.binding == expected_request.binding
    assert replay.recorded_at == expected_request.requested_at
    assert replay.evidence_ref.sha256 == hashlib.sha256(
        next(tmp_path.rglob("request.json")).read_bytes()
    ).hexdigest()


@pytest.mark.asyncio
async def test_completion_requires_explicit_captain_action_and_survives_restart(
    tmp_path: Path,
) -> None:
    expected_request = request()
    store = CaptainHumanReviewStore(review_root(tmp_path))
    accepted = await store.request_review(expected_request)

    assert accepted.status == "accepted"
    completed = store.complete_review(
        expected_request,
        evidence_ref=evidence("review-decision"),
        completed_at=NOW + timedelta(minutes=2),
    )

    assert completed.status == "completed"
    assert completed.evidence_ref == evidence("review-decision")
    assert await CaptainHumanReviewStore(review_root(tmp_path)).request_review(
        expected_request
    ) == completed


def test_completion_cannot_create_or_auto_accept_a_missing_review(tmp_path: Path) -> None:
    store = CaptainHumanReviewStore(review_root(tmp_path))

    with pytest.raises(CaptainHumanReviewConflictError, match="accepted"):
        store.complete_review(
            request(),
            evidence_ref=evidence("review-decision"),
            completed_at=NOW + timedelta(minutes=2),
        )


@pytest.mark.asyncio
async def test_identical_completion_replays_but_changed_completion_conflicts(
    tmp_path: Path,
) -> None:
    expected_request = request()
    store = CaptainHumanReviewStore(review_root(tmp_path))
    await store.request_review(expected_request)
    first = store.complete_review(
        expected_request,
        evidence_ref=evidence("review-decision"),
        completed_at=NOW + timedelta(minutes=2),
    )

    assert store.complete_review(
        expected_request,
        evidence_ref=evidence("review-decision"),
        completed_at=NOW + timedelta(minutes=2),
    ) == first
    with pytest.raises(CaptainHumanReviewConflictError, match="different content"):
        store.complete_review(
            expected_request,
            evidence_ref=evidence("changed-decision"),
            completed_at=NOW + timedelta(minutes=3),
        )


@pytest.mark.asyncio
async def test_mixed_request_or_binding_for_same_effect_and_fence_is_rejected(
    tmp_path: Path,
) -> None:
    store = CaptainHumanReviewStore(review_root(tmp_path))
    await store.request_review(request())

    mixed = request(
        selected_binding=binding(case_sha256="c" * 64),
        review_request_id=UUID(int=REVIEW_ID.int + 1),
    )
    with pytest.raises(CaptainHumanReviewConflictError, match="different content"):
        await store.request_review(mixed)


@pytest.mark.asyncio
async def test_larger_fence_wins_and_stale_review_request_fails_closed(
    tmp_path: Path,
) -> None:
    store = CaptainHumanReviewStore(review_root(tmp_path))
    first = request()
    second = request(
        selected_binding=binding(fence=2),
        review_request_id=UUID(int=REVIEW_ID.int + 1),
        requested_at=NOW + timedelta(minutes=1),
    )
    await store.request_review(first)
    assert (await store.request_review(second)).binding.fence == 2

    with pytest.raises(CaptainHumanReviewStaleFenceError, match="stale"):
        await store.request_review(first)


@pytest.mark.asyncio
async def test_restart_rejects_tampered_request_and_receipt(tmp_path: Path) -> None:
    expected_request = request()
    await CaptainHumanReviewStore(review_root(tmp_path)).request_review(expected_request)
    request_path = next(tmp_path.rglob("request.json"))
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    payload["reason_code"] = "different-reason"
    request_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(CaptainHumanReviewConflictError, match="invalid|different"):
        await CaptainHumanReviewStore(review_root(tmp_path)).request_review(
            expected_request
        )


@pytest.mark.asyncio
async def test_restart_rejects_tampered_receipt_and_missing_predecessor(
    tmp_path: Path,
) -> None:
    expected_request = request()
    store = CaptainHumanReviewStore(review_root(tmp_path))
    await store.request_review(expected_request)
    store.complete_review(
        expected_request,
        evidence_ref=evidence("review-decision"),
        completed_at=NOW + timedelta(minutes=2),
    )
    completed_path = next(tmp_path.rglob("completed.json"))
    payload = json.loads(completed_path.read_text(encoding="utf-8"))
    payload["binding"]["case_sha256"] = "c" * 64
    completed_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(CaptainHumanReviewConflictError, match="invalid"):
        await CaptainHumanReviewStore(review_root(tmp_path)).request_review(
            expected_request
        )

    completed_path.unlink()
    next(tmp_path.rglob("accepted.json")).unlink()
    with pytest.raises(CaptainHumanReviewConflictError, match="invalid"):
        await CaptainHumanReviewStore(review_root(tmp_path)).request_review(
            expected_request
        )


def test_root_is_captain_private_and_gitignored(tmp_path: Path) -> None:
    assert default_captain_human_review_root(tmp_path) == review_root(tmp_path)
    with pytest.raises(ValueError, match=".captain-cook"):
        CaptainHumanReviewStore(tmp_path / "human-review")
