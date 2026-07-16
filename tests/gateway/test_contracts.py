from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gateway.contracts import (
    BatchDoneEvent,
    BatchProjection,
    ClaimEvent,
    EvidenceEvent,
    HeartbeatEvent,
    project_batch,
)


BATCH_ID = "batch-1"
PARENT_INDEX = 10
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
TERMINAL_OUTCOMES = (
    "succeeded",
    "failed",
    "rejected",
    "cancelled",
    "failed_after_max_iterations",
    "aborted_infra",
)


def work_batch(
    *,
    index: int = PARENT_INDEX,
    batch_id: str = BATCH_ID,
    status: str = "pending",
    parent_index: int | None = None,
) -> dict[str, Any]:
    return {
        "index": index,
        "parent_index": parent_index,
        "block_type": "work_batch",
        "data": {"batch_id": batch_id, "title": "Build a workflow"},
        "status": status,
    }


def child(
    index: int,
    block_type: str,
    *,
    parent_index: int = PARENT_INDEX,
    batch_id: str = BATCH_ID,
    **data: Any,
) -> dict[str, Any]:
    return {
        "index": index,
        "parent_index": parent_index,
        "block_type": block_type,
        "data": {"batch_id": batch_id, **data},
        "status": "recorded",
    }


def claim(
    index: int,
    *,
    expires_at: datetime | None = None,
    token_hash: str | None = None,
) -> dict[str, Any]:
    return child(
        index,
        "batch_claimed",
        claim_token_sha256=token_hash or f"token-{index}",
        claim_expires_at=expires_at or NOW + timedelta(minutes=30),
    )


def test_public_event_models_expose_the_planned_shapes() -> None:
    expiry = NOW + timedelta(minutes=30)

    projection = BatchProjection(batch_id=BATCH_ID, parent_index=PARENT_INDEX, status="pending")
    claimed = ClaimEvent(
        batch_id=BATCH_ID,
        claim_token_sha256="sha256",
        claim_expires_at=expiry,
    )
    heartbeat = HeartbeatEvent(batch_id=BATCH_ID, claim_expires_at=expiry)
    evidence = EvidenceEvent(batch_id=BATCH_ID, iteration=1, artifact_ref="run-1")
    done = BatchDoneEvent(batch_id=BATCH_ID, outcome="aborted_infra", reason="database unavailable")

    assert projection.claim_iteration == 0
    assert claimed.claim_token_sha256 == "sha256"
    assert heartbeat.claim_expires_at == expiry
    assert evidence.model_extra == {"artifact_ref": "run-1"}
    assert done.model_extra == {"reason": "database unavailable"}


def test_event_models_reject_invalid_iterations_and_terminal_outcomes() -> None:
    with pytest.raises(ValidationError):
        EvidenceEvent(batch_id=BATCH_ID, iteration=0)
    with pytest.raises(ValidationError):
        BatchDoneEvent(batch_id=BATCH_ID, outcome="timed_out")


@pytest.mark.parametrize("iteration", [True, "1"])
def test_evidence_iteration_requires_a_strict_integer(iteration: Any) -> None:
    with pytest.raises(ValidationError):
        EvidenceEvent(batch_id=BATCH_ID, iteration=iteration)


def test_pending_parent_projects_without_lifecycle_events() -> None:
    projection = project_batch([work_batch()], BATCH_ID, now=NOW)

    assert projection == BatchProjection(
        batch_id=BATCH_ID,
        parent_index=PARENT_INDEX,
        status="pending",
    )


def test_projection_ignores_blocks_owned_by_other_batches() -> None:
    blocks = [
        work_batch(index=2, batch_id="batch-other"),
        child(3, "batch_claimed", parent_index=2, batch_id="batch-other", claim_token_sha256="other", claim_expires_at=NOW),
        work_batch(),
    ]

    assert project_batch(blocks, BATCH_ID, now=NOW).status == "pending"


def test_projection_requires_exactly_one_root_work_batch() -> None:
    with pytest.raises(ValueError, match="missing work_batch"):
        project_batch([], BATCH_ID, now=NOW)

    with pytest.raises(ValueError, match="duplicate work_batch"):
        project_batch([work_batch(), work_batch(index=11)], BATCH_ID, now=NOW)

    with pytest.raises(ValueError, match="root work_batch"):
        project_batch([work_batch(parent_index=4)], BATCH_ID, now=NOW)


@pytest.mark.parametrize("status", ["claimed", "completed", ""])
def test_projection_rejects_mutated_parent_lifecycle_status(status: str) -> None:
    with pytest.raises(ValueError, match="pending or pending_review"):
        project_batch([work_batch(status=status)], BATCH_ID, now=NOW)


def test_projection_requires_strictly_increasing_block_indexes() -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        project_batch([work_batch(), child(9, "holdout")], BATCH_ID, now=NOW)

    with pytest.raises(ValueError, match="strictly increasing"):
        project_batch([work_batch(), child(PARENT_INDEX, "holdout")], BATCH_ID, now=NOW)


@pytest.mark.parametrize(
    "bad_child",
    [
        child(11, "holdout", parent_index=99),
        child(11, "holdout", batch_id="batch-other"),
        child(11, "batch_claimed", parent_index=99, claim_token_sha256="sha256", claim_expires_at=NOW),
        child(11, "batch_claimed", batch_id="batch-other", claim_token_sha256="sha256", claim_expires_at=NOW),
    ],
)
def test_projection_rejects_mismatched_child_batch_or_parent(bad_child: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="child relationship"):
        project_batch([work_batch(), bad_child], BATCH_ID, now=NOW)


def test_foreign_work_batch_cannot_attach_to_the_target_parent() -> None:
    foreign_child = work_batch(index=11, batch_id="batch-other", parent_index=PARENT_INDEX)

    with pytest.raises(ValueError, match="child relationship"):
        project_batch([work_batch(), foreign_child], BATCH_ID, now=NOW)


def test_boolean_parent_reference_cannot_match_integer_parent_index() -> None:
    target = work_batch(index=1)
    boolean_parent = child(2, "holdout", parent_index=True)

    with pytest.raises(ValueError, match="child relationship"):
        project_batch([target, boolean_parent], BATCH_ID, now=NOW)


def test_pending_review_requires_one_ordered_approval() -> None:
    unapproved = project_batch([work_batch(status="pending_review")], BATCH_ID, now=NOW)
    approved = project_batch(
        [work_batch(status="pending_review"), child(11, "batch_approved")],
        BATCH_ID,
        now=NOW,
    )

    assert unapproved.status == "pending_review"
    assert approved.status == "pending"


@pytest.mark.parametrize(
    "blocks",
    [
        [work_batch(), child(11, "batch_approved")],
        [work_batch(status="pending_review"), child(11, "batch_approved"), child(12, "batch_approved")],
        [work_batch(status="pending_review"), claim(11)],
    ],
)
def test_projection_rejects_invalid_or_duplicate_approval_ordering(blocks: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError, match="approval"):
        project_batch(blocks, BATCH_ID, now=NOW)


def test_live_claim_projects_current_fence_and_lease() -> None:
    expiry = NOW + timedelta(minutes=10)

    projection = project_batch(
        [work_batch(), claim(11, expires_at=expiry, token_hash="current-token-hash")],
        BATCH_ID,
        now=NOW,
    )

    assert projection.status == "claimed"
    assert projection.claim_iteration == 1
    assert projection.claim_token_sha256 == "current-token-hash"
    assert projection.claim_expires_at == expiry


def test_latest_heartbeat_owns_the_current_lease() -> None:
    heartbeat_expiry = NOW + timedelta(hours=1)

    projection = project_batch(
        [
            work_batch(),
            claim(11, expires_at=NOW + timedelta(minutes=5)),
            child(12, "batch_heartbeat", claim_expires_at=heartbeat_expiry),
        ],
        BATCH_ID,
        now=NOW,
    )

    assert projection.status == "claimed"
    assert projection.claim_expires_at == heartbeat_expiry


def test_expired_non_terminal_claim_projects_as_pending() -> None:
    expiry = NOW - timedelta(seconds=1)

    projection = project_batch([work_batch(), claim(11, expires_at=expiry)], BATCH_ID, now=NOW)

    assert projection.status == "pending"
    assert projection.claim_iteration == 1
    assert projection.claim_expires_at == expiry


def test_default_clock_is_current_utc() -> None:
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    past = datetime(2000, 1, 1, tzinfo=timezone.utc)

    assert project_batch([work_batch(), claim(11, expires_at=future)], BATCH_ID).status == "claimed"
    assert project_batch([work_batch(), claim(11, expires_at=past)], BATCH_ID).status == "pending"


def test_heartbeat_before_a_claim_is_invalid() -> None:
    with pytest.raises(ValueError, match="heartbeat before claim"):
        project_batch(
            [work_batch(), child(11, "batch_heartbeat", claim_expires_at=NOW + timedelta(minutes=5))],
            BATCH_ID,
            now=NOW,
        )


def test_current_iteration_evidence_is_projected() -> None:
    projection = project_batch(
        [
            work_batch(),
            claim(11),
            child(12, "codex_session", iteration=1, session_id="session-1"),
            child(13, "validation_run", iteration=1, report_ref="report-1"),
        ],
        BATCH_ID,
        now=NOW,
    )

    assert projection.codex_session_recorded is True
    assert projection.validation_run_recorded is True


@pytest.mark.parametrize(
    "blocks",
    [
        [work_batch(), child(11, "codex_session", iteration=1)],
        [work_batch(), claim(11), child(12, "validation_run", iteration=2)],
        [work_batch(), claim(11), claim(12), child(13, "codex_session", iteration=1)],
    ],
)
def test_projection_rejects_evidence_for_the_wrong_claim_iteration(blocks: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError, match="current claim iteration"):
        project_batch(blocks, BATCH_ID, now=NOW)


def test_new_claim_resets_current_iteration_evidence() -> None:
    projection = project_batch(
        [
            work_batch(),
            claim(11),
            child(12, "codex_session", iteration=1),
            child(13, "validation_run", iteration=1),
            claim(14, expires_at=NOW + timedelta(hours=1)),
        ],
        BATCH_ID,
        now=NOW,
    )

    assert projection.claim_iteration == 2
    assert projection.codex_session_recorded is False
    assert projection.validation_run_recorded is False


@pytest.mark.parametrize("outcome", TERMINAL_OUTCOMES)
def test_first_valid_batch_done_projects_every_terminal_outcome(outcome: str) -> None:
    blocks = [work_batch(), claim(11)]
    if outcome == "succeeded":
        blocks.append(child(12, "validation_run", iteration=1))
    blocks.append(child(13, "batch_done", outcome=outcome))

    assert project_batch(blocks, BATCH_ID, now=NOW).status == outcome


def test_succeeded_requires_prior_current_iteration_validation_evidence() -> None:
    with pytest.raises(ValueError, match="validation_run"):
        project_batch(
            [work_batch(), claim(11), child(12, "batch_done", outcome="succeeded")],
            BATCH_ID,
            now=NOW,
        )


def test_boolean_iteration_cannot_authorize_success() -> None:
    with pytest.raises(ValueError, match="invalid validation_run data"):
        project_batch(
            [
                work_batch(),
                claim(11),
                child(12, "validation_run", iteration=True),
                child(13, "batch_done", outcome="succeeded"),
            ],
            BATCH_ID,
            now=NOW,
        )


def test_terminal_before_a_claim_is_invalid() -> None:
    with pytest.raises(ValueError, match="terminal before claim"):
        project_batch(
            [work_batch(), child(11, "batch_done", outcome="failed")],
            BATCH_ID,
            now=NOW,
        )


@pytest.mark.parametrize(
    "later_event",
    [
        claim(13),
        child(13, "batch_heartbeat", claim_expires_at=NOW + timedelta(hours=1)),
        child(13, "codex_session", iteration=1),
        child(13, "validation_run", iteration=1),
        child(13, "batch_done", outcome="failed"),
        child(13, "batch_approved"),
    ],
)
def test_lifecycle_events_after_terminal_are_invalid(later_event: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="after terminal"):
        project_batch(
            [work_batch(), claim(11), child(12, "batch_done", outcome="failed"), later_event],
            BATCH_ID,
            now=NOW,
        )


def test_contract_boundary_has_no_infrastructure_or_fastapi_dependency() -> None:
    source = Path("gateway/contracts.py").read_text(encoding="utf-8").lower()

    assert "fastapi" not in source
    assert "mariadb" not in source
    assert "gateway.app" not in source
