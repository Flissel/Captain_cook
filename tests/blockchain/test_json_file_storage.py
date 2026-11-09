"""JSONFileStorage fails closed: an unreadable ledger is never replaced."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blockchain.Blockchain_modell import Blockchain
from blockchain.storage import JSONFileStorage, LedgerStorageCorruptError


def test_missing_file_is_an_empty_ledger(tmp_path: Path) -> None:
    assert JSONFileStorage(str(tmp_path / "absent.json")).load() == []


def test_valid_ledger_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "chain.json"
    chain = Blockchain(file_path=str(path))
    chain.add_block("problem", {"title": "ünïcode"})

    reloaded = Blockchain(file_path=str(path))

    assert [block.block_type for block in reloaded.chain] == ["genesis", "problem"]
    assert reloaded.chain[1].data == {"title": "ünïcode"}


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{not json",
        '[{"index": 0}',
        json.dumps({"index": 0}),
        json.dumps(["genesis"]),
        json.dumps(None),
    ],
    ids=["empty", "garbage", "truncated", "object-root", "non-object-entry", "null"],
)
def test_unreadable_ledger_raises_and_is_left_untouched(tmp_path: Path, content: str) -> None:
    path = tmp_path / "chain.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(LedgerStorageCorruptError):
        JSONFileStorage(str(path)).load()
    with pytest.raises(LedgerStorageCorruptError):
        Blockchain(file_path=str(path))

    # The evidence of what went wrong survives for an operator to inspect.
    assert path.read_text(encoding="utf-8") == content


def test_corrupt_error_names_the_file_but_not_its_content(tmp_path: Path) -> None:
    path = tmp_path / "chain.json"
    path.write_text('{"secret": "do-not-echo"', encoding="utf-8")

    with pytest.raises(LedgerStorageCorruptError) as excinfo:
        JSONFileStorage(str(path)).load()

    assert str(path) in str(excinfo.value)
    assert "do-not-echo" not in str(excinfo.value)


def test_explicit_reset_still_replaces_a_corrupt_ledger(tmp_path: Path) -> None:
    path = tmp_path / "chain.json"
    path.write_text("{not json", encoding="utf-8")

    chain = Blockchain(file_path=str(path), reset=True)

    assert [block.block_type for block in chain.chain] == ["genesis"]
