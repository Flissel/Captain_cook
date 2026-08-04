from __future__ import annotations

import asyncio
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
    run_captain_human_review_completion_adapter,
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


def baseline_binding() -> BusinessBenchmarkProviderBindingV1:
    payload = binding().model_dump(mode="json", by_alias=True)
    payload["variant"] = "single_agent_baseline"
    return BusinessBenchmarkProviderBindingV1.model_validate(payload)


def request(
    *,
    selected_binding: BusinessBenchmarkProviderBindingV1 | None = None,
    review_request_id: UUID = REVIEW_ID,
    requested_at: datetime = NOW,
    reason_code: str = "mandatory_human_review",
) -> CaptainHumanReviewRequestV1:
    return CaptainHumanReviewRequestV1(
        schema="captain.business-benchmark-human-review-request.v1",
        review_request_id=review_request_id,
        binding=selected_binding or binding(),
        reason_code=reason_code,
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
async def test_baseline_can_never_request_captain_human_review(tmp_path: Path) -> None:
    store = CaptainHumanReviewStore(review_root(tmp_path))

    with pytest.raises(CaptainHumanReviewConflictError, match="candidate"):
        await store.request_review(request(selected_binding=baseline_binding()))

    assert not tuple(review_root(tmp_path).rglob("request.json"))


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


@pytest.mark.asyncio
async def test_configured_wait_observes_only_explicit_completion_after_acceptance(
    tmp_path: Path,
) -> None:
    expected_request = request()
    root = review_root(tmp_path)
    waiting_store = CaptainHumanReviewStore(
        root,
        completion_timeout_seconds=0.5,
        completion_poll_interval_seconds=0.01,
    )

    pending = asyncio.create_task(waiting_store.request_review(expected_request))
    for _ in range(100):
        if tuple(root.rglob("accepted.json")):
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail("review request was not durably accepted before polling")

    completed = CaptainHumanReviewStore(root).complete_review(
        expected_request,
        evidence_ref=evidence("operator-review"),
        completed_at=NOW + timedelta(minutes=3),
    )

    assert await pending == completed
    assert completed.status == "completed"


@pytest.mark.asyncio
async def test_configured_wait_times_out_as_accepted_without_auto_completion(
    tmp_path: Path,
) -> None:
    store = CaptainHumanReviewStore(
        review_root(tmp_path),
        completion_timeout_seconds=0.02,
        completion_poll_interval_seconds=0.005,
    )

    receipt = await store.request_review(request())

    assert receipt.status == "accepted"
    assert not tuple(review_root(tmp_path).rglob("completed.json"))


def test_wait_configuration_is_finite_and_bounded(tmp_path: Path) -> None:
    root = review_root(tmp_path)

    for invalid in (-1.0, float("inf"), 301.0):
        with pytest.raises(ValueError, match="completion timeout"):
            CaptainHumanReviewStore(root, completion_timeout_seconds=invalid)
    with pytest.raises(ValueError, match="poll interval"):
        CaptainHumanReviewStore(root, completion_poll_interval_seconds=0.0)


@pytest.mark.asyncio
async def test_pending_queue_lists_only_latest_redacted_restart_safe_metadata(
    tmp_path: Path,
) -> None:
    root = review_root(tmp_path)
    store = CaptainHumanReviewStore(root)
    await store.request_review(request())
    newer = request(
        selected_binding=binding(fence=2),
        review_request_id=UUID(int=REVIEW_ID.int + 1),
        requested_at=NOW + timedelta(minutes=1),
    )
    await store.request_review(newer)

    pending = CaptainHumanReviewStore(root).list_reviews(status="pending")

    assert len(pending) == 1
    assert pending[0].review_request_id == newer.review_request_id
    assert pending[0].effect_id == newer.binding.effect_id
    assert pending[0].fence == 2
    assert pending[0].status == "accepted"
    serialized = pending[0].model_dump_json(by_alias=True)
    assert "case_sha256" in serialized
    assert "case_body" not in serialized
    assert "task" not in serialized


@pytest.mark.asyncio
async def test_operator_cli_lists_and_explicitly_completes_with_redacted_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agenten.agent_factory.business_benchmark_human_review_cli import (
        main as operator_main,
    )

    root = review_root(tmp_path)
    expected_request = request()
    await CaptainHumanReviewStore(root).request_review(expected_request)

    assert operator_main(["--root", str(root), "list", "--status", "pending"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1
    assert listed["reviews"][0]["review_request_id"] == str(REVIEW_ID)

    completed_at = (NOW + timedelta(minutes=4)).isoformat()
    complete_args = [
        "--root",
        str(root),
        "complete",
        "--review-request-id",
        str(REVIEW_ID),
        "--operator-id",
        "captain-demo-operator",
        "--decision-code",
        "reviewed",
        "--completed-at",
        completed_at,
    ]
    assert operator_main(complete_args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "completed"

    # Exact operator replay after a process restart is byte-identical.
    assert operator_main(complete_args) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay == first
    assert await CaptainHumanReviewStore(root).request_review(expected_request) == (
        CaptainHumanReviewStore(root).find_receipt(REVIEW_ID)
    )

    artifacts = tuple(root.rglob("evidence.json"))
    assert len(artifacts) == 1
    artifact_bytes = artifacts[0].read_bytes()
    artifact_payload = json.loads(artifact_bytes)
    assert artifact_payload == {
        "authority": "captain_human_review",
        "completed_at": completed_at.replace("+00:00", "Z"),
        "decision_code": "reviewed",
        "effect_id": "a" * 64,
        "fence": 1,
        "operator_id": "captain-demo-operator",
        "review_request_id": str(REVIEW_ID),
        "schema": "captain.business-benchmark-human-review-evidence.v1",
    }
    assert first["evidence_ref"]["sha256"] == hashlib.sha256(
        artifact_bytes
    ).hexdigest()
    assert "case" not in artifact_bytes.decode("utf-8")
    assert "task" not in artifact_bytes.decode("utf-8")

    assert operator_main(["--root", str(root), "list", "--status", "pending"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 0


@pytest.mark.asyncio
async def test_completion_adapter_is_explicit_job_scoped_and_restart_safe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agenten.agent_factory.business_benchmark_human_review_cli import (
        main as operator_main,
    )

    root = review_root(tmp_path)
    store = CaptainHumanReviewStore(root)
    allowed = request()
    unrelated_job_id = UUID(int=JOB_ID.int + 100)
    unrelated = request(
        selected_binding=BusinessBenchmarkProviderBindingV1.model_validate(
            {
                **binding().model_dump(mode="json", by_alias=True),
                "effect_id": "c" * 64,
                "claim_id": str(UUID(int=CLAIM_ID.int + 100)),
                "job_id": str(unrelated_job_id),
                "correlation_id": str(UUID(int=CORRELATION_ID.int + 100)),
                "request_id": str(UUID(int=REQUEST_ID.int + 100)),
            }
        ),
        review_request_id=UUID(int=REVIEW_ID.int + 100),
    )
    await store.request_review(allowed)
    await store.request_review(unrelated)

    result = run_captain_human_review_completion_adapter(
        root,
        job_ids=(JOB_ID,),
        operator_id="codex-delegate",
        decision_code="benchmark-escalation-acknowledged",
        expected_completions=1,
        timeout_seconds=0,
        completed_at=lambda: NOW + timedelta(minutes=5),
    )

    assert result.status == "completed"
    assert result.completed_count == 1
    assert result.completed_review_request_ids == (REVIEW_ID,)
    assert CaptainHumanReviewStore(root).find_receipt(REVIEW_ID).status == "completed"
    assert CaptainHumanReviewStore(root).find_receipt(
        unrelated.review_request_id
    ).status == "accepted"

    # The process adapter exposes the same exact, bounded authority surface.
    restarted_root = tmp_path / ".captain-cook" / "private" / "adapter-cli"
    restarted_store = CaptainHumanReviewStore(restarted_root)
    await restarted_store.request_review(allowed)
    assert operator_main(
        [
            "--root",
            str(restarted_root),
            "watch",
            "--job-id",
            str(JOB_ID),
            "--operator-id",
            "codex-delegate",
            "--decision-code",
            "benchmark-escalation-acknowledged",
            "--expected-completions",
            "1",
            "--timeout-seconds",
            "0",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["completed_count"] == 1
    assert payload["job_ids"] == [str(JOB_ID)]


@pytest.mark.asyncio
async def test_invalid_operator_completion_leaves_no_evidence_artifact(
    tmp_path: Path,
) -> None:
    root = review_root(tmp_path)
    store = CaptainHumanReviewStore(root)
    await store.request_review(request())

    with pytest.raises(ValueError, match="before the request"):
        store.complete_review_as_operator(
            REVIEW_ID,
            operator_id="captain-demo-operator",
            decision_code="reviewed",
            completed_at=NOW - timedelta(seconds=1),
        )

    assert not tuple(root.rglob("evidence.json"))
    assert store.find_receipt(REVIEW_ID).status == "accepted"

    corrected = store.complete_review_as_operator(
        REVIEW_ID,
        operator_id="captain-demo-operator",
        decision_code="reviewed",
        completed_at=NOW + timedelta(seconds=1),
    )
    assert corrected.status == "completed"
