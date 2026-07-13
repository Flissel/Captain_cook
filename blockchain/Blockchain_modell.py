"""Generic hash-chained ledger for tracking arbitrary records.

Block carries an opaque ``block_type`` + ``data`` payload instead of
hardcoded task fields, so new record kinds (tasks, decisions, research
results, ...) never require changing Block or Blockchain — callers just
pick a new ``block_type`` and shape ``data`` however they need.
"""
import hashlib
from typing import Any, Dict, List, Optional

from .storage import LedgerStorage, JSONFileStorage


class Block:
    def __init__(
        self,
        index: int,
        block_type: str,
        data: Dict[str, Any],
        status: str,
        previous_hash: str,
        parent_index: Optional[int] = None,
        children: Optional[List[int]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        hash: Optional[str] = None,
    ):
        self.index = index
        self.block_type = block_type
        self.data = data
        self.status = status
        self.previous_hash = previous_hash
        self.parent_index = parent_index
        self.children = children or []  # List of child block indices
        self.metadata = metadata or {}
        self.hash = hash or self.compute_hash()

    def compute_hash(self) -> str:
        block_string = (
            f"{self.index}{self.block_type}{self.data}{self.status}"
            f"{self.previous_hash}{self.parent_index}{self.children}"
        )
        return hashlib.sha256(block_string.encode()).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    # --- Backward-compatible accessors for the original task-shaped payload ---
    @property
    def task(self) -> Optional[str]:
        return self.data.get("task") if self.block_type == "task" else None

    @property
    def assigned_agents(self) -> List[str]:
        return self.data.get("assigned_agents", []) if self.block_type == "task" else []


class Blockchain:
    """Hash-chained ledger.

    Both persistence (via ``storage``) and record shape (via ``block_type`` /
    ``data`` on ``add_block``) are pluggable — extending what the ledger
    tracks or where it lives never requires editing this class.
    """

    def __init__(
        self,
        storage: Optional[LedgerStorage] = None,
        file_path: str = "blockchain.json",
        reset: bool = False,
    ):
        self.storage = storage or JSONFileStorage(file_path)
        self.chain: List[Block] = []

        if reset:
            self.storage.clear()

        records = self.storage.load()
        if records:
            self.chain = [Block(**record) for record in records]
        else:
            self._create_genesis_block()

    def _create_genesis_block(self):
        genesis_block = Block(0, "genesis", {}, "completed", "0")
        self.chain = [genesis_block]
        self._save()

    def add_block(
        self,
        block_type: str,
        data: Dict[str, Any],
        status: str = "pending",
        parent_index: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Block:
        previous_hash = self.chain[-1].hash
        new_block = Block(
            index=len(self.chain),
            block_type=block_type,
            data=data,
            status=status,
            previous_hash=previous_hash,
            parent_index=parent_index,
            metadata=metadata,
        )

        # Update parent block with child reference
        if parent_index is not None:
            parent_block = self.chain[parent_index]
            parent_block.children.append(new_block.index)
            parent_block.hash = parent_block.compute_hash()  # Recompute parent hash

        self.chain.append(new_block)
        self._save()
        return new_block

    def add_task_block(
        self,
        task: str,
        assigned_agents: Optional[List[str]] = None,
        status: str = "pending",
        parent_index: Optional[int] = None,
    ) -> Block:
        """Convenience wrapper preserving the original task-block shape."""
        return self.add_block(
            block_type="task",
            data={"task": task, "assigned_agents": assigned_agents or []},
            status=status,
            parent_index=parent_index,
        )

    def update_task_status(self, index: int, status: str) -> Block:
        block = self.get_block(index)
        if block is None:
            raise IndexError(f"No block at index {index}")
        block.status = status
        block.hash = block.compute_hash()
        self._save()
        return block

    def get_block(self, index: int) -> Optional[Block]:
        return self.chain[index] if 0 <= index < len(self.chain) else None

    def get_blocks_by_type(self, block_type: str) -> List[Block]:
        return [block for block in self.chain if block.block_type == block_type]

    def _save(self) -> None:
        self.storage.save([block.to_dict() for block in self.chain])
