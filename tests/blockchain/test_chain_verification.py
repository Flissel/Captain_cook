"""Chain verification for append-only, never-mutated ledgers."""
import pytest

from blockchain.Blockchain_modell import Block, ChainVerificationError, verify_chain


def _append(blocks, block_type, data, status="pending"):
    previous_hash = blocks[-1].hash if blocks else "0"
    block = Block(
        index=len(blocks),
        block_type=block_type,
        data=data,
        status=status,
        previous_hash=previous_hash,
    )
    blocks.append(block)
    return block


def test_verify_chain_accepts_an_untouched_append_only_chain():
    blocks = []
    _append(blocks, "genesis", {}, status="completed")
    _append(blocks, "batch_claimed", {"batch_id": "b-1"})
    _append(blocks, "recovery_decision", {"batch_id": "b-1", "decision": "requeue"})

    verify_chain(blocks)


def test_verify_chain_accepts_an_empty_chain():
    verify_chain([])


def test_verify_chain_accepts_a_genesis_only_chain():
    blocks = []
    _append(blocks, "genesis", {}, status="completed")

    verify_chain(blocks)


def test_verify_chain_detects_a_mutated_payload():
    blocks = []
    _append(blocks, "genesis", {}, status="completed")
    tampered = _append(blocks, "batch_claimed", {"batch_id": "b-1"})

    tampered.data["batch_id"] = "b-2"

    with pytest.raises(ChainVerificationError) as error:
        verify_chain(blocks)
    assert "index 1" in str(error.value)


def test_verify_chain_detects_a_broken_link():
    blocks = []
    _append(blocks, "genesis", {}, status="completed")
    _append(blocks, "batch_claimed", {"batch_id": "b-1"})

    blocks[1].previous_hash = "0" * 64

    with pytest.raises(ChainVerificationError) as error:
        verify_chain(blocks)
    assert "index 1" in str(error.value)


def test_status_is_outside_the_hash_by_design():
    """Documents a real limitation: a status-only edit is NOT detected.

    `status` is deliberately excluded from compute_hash because the in-process
    pipeline updates it after append. Anyone reading verify_chain as full
    tamper-evidence needs to see this boundary spelled out.

    The trailing `data` mutation proves this isn't just verify_chain being a
    no-op: the same chain, on the same blocks, IS caught the moment the
    mutation touches a hashed field. That keeps this test honest about what
    "not detected" means — a live check with a known blind spot, not a check
    that never fires.
    """
    blocks = []
    _append(blocks, "genesis", {}, status="completed")
    _append(blocks, "batch_claimed", {"batch_id": "b-1"}, status="pending")

    blocks[1].status = "aborted_infra"

    verify_chain(blocks)

    blocks[1].data["batch_id"] = "b-2"

    with pytest.raises(ChainVerificationError) as error:
        verify_chain(blocks)
    assert "index 1" in str(error.value)
