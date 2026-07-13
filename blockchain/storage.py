"""Pluggable persistence backends for the Blockchain ledger.

Blockchain talks to a LedgerStorage instead of a hardcoded JSON file, so a
new backend (SQLite, a database, S3, ...) can be added by implementing this
interface, with no changes to Blockchain/Block.
"""
from abc import ABC, abstractmethod
import json
import os
from typing import Any, Dict, List


class LedgerStorage(ABC):
    """Storage backend contract for a Blockchain's blocks."""

    @abstractmethod
    def load(self) -> List[Dict[str, Any]]:
        """Return the persisted blocks as plain dicts, oldest first."""

    @abstractmethod
    def save(self, blocks: List[Dict[str, Any]]) -> None:
        """Persist the full list of blocks (plain dicts), oldest first."""

    @abstractmethod
    def clear(self) -> None:
        """Wipe any persisted state."""


class JSONFileStorage(LedgerStorage):
    """Default backend: the whole chain as a single JSON array on disk."""

    def __init__(self, file_path: str = "blockchain.json"):
        self.file_path = file_path

    def load(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.file_path):
            return []
        try:
            with open(self.file_path, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return []

    def save(self, blocks: List[Dict[str, Any]]) -> None:
        temp_file_path = f"{self.file_path}.tmp"
        with open(temp_file_path, "w") as f:
            json.dump(blocks, f, indent=4)
        os.replace(temp_file_path, self.file_path)

    def clear(self) -> None:
        self.save([])


class InMemoryStorage(LedgerStorage):
    """Non-persistent backend, handy for tests or throwaway runs."""

    def __init__(self):
        self._blocks: List[Dict[str, Any]] = []

    def load(self) -> List[Dict[str, Any]]:
        return list(self._blocks)

    def save(self, blocks: List[Dict[str, Any]]) -> None:
        self._blocks = list(blocks)

    def clear(self) -> None:
        self._blocks = []
