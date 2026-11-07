"""The Gateway re-verifies the hash chain of its ledger before serving."""

from __future__ import annotations

from typing import Any

import pytest

from blockchain.Blockchain_modell import Block, ChainVerificationError
from gateway.store import verify_ledger_chain


class _Storage:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def load(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]


def _chain(length: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous_hash = "0"
    for index in range(length):
        block = Block(
            index=index,
            block_type="work_batch" if index == 0 else "codex_session",
            data={"batch_id": "batch-1", "iteration": index},
            status="pending" if index == 0 else "recorded",
            previous_hash=previous_hash,
            parent_index=None if index == 0 else 0,
        ).to_dict()
        rows.append(block)
        previous_hash = block["hash"]
    return rows


def test_intact_ledger_verifies_and_reports_its_length() -> None:
    assert verify_ledger_chain(_Storage(_chain(3))) == 3


def test_empty_ledger_verifies() -> None:
    assert verify_ledger_chain(_Storage([])) == 0


def test_edited_payload_is_detected() -> None:
    rows = _chain(3)
    rows[1]["data"] = {**rows[1]["data"], "iteration": 99}

    with pytest.raises(ChainVerificationError, match="payload hash mismatch at index 1"):
        verify_ledger_chain(_Storage(rows))


def test_deleted_block_is_detected() -> None:
    rows = _chain(3)
    del rows[1]

    with pytest.raises(ChainVerificationError, match="broken chain link at index 2"):
        verify_ledger_chain(_Storage(rows))


def test_gateway_refuses_to_serve_from_a_broken_ledger() -> None:
    from gateway.app import verify_ledger_before_serving

    rows = _chain(2)
    rows[0]["data"] = {"batch_id": "batch-1", "iteration": 7}

    with pytest.raises(ChainVerificationError):
        verify_ledger_before_serving(_Storage(rows))
    verify_ledger_before_serving(_Storage(_chain(2)))
