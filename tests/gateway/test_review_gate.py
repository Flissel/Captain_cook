"""The Gateway review gate decides a released work batch's starting status."""

from gateway.app import BlockRequest, apply_review_gate


def _batch(**fields: object) -> BlockRequest:
    return BlockRequest(block_type="work_batch", data={"batch_id": "batch-1"}, **fields)


def test_enabled_review_gate_holds_a_default_release_for_approval() -> None:
    gated = apply_review_gate(_batch(), approval_enabled=True)

    assert gated.status == "pending_review"


def test_disabled_review_gate_keeps_the_claimable_default() -> None:
    assert apply_review_gate(_batch(), approval_enabled=False).status == "pending"


def test_explicit_status_is_a_deliberate_captain_decision() -> None:
    explicit = apply_review_gate(_batch(status="pending"), approval_enabled=True)

    assert explicit.status == "pending"


def test_review_gate_only_applies_to_work_batches() -> None:
    holdout = BlockRequest(block_type="holdout", data={"batch_id": "batch-1"}, parent_index=1)

    assert apply_review_gate(holdout, approval_enabled=True).status == "pending"
