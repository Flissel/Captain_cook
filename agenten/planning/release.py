"""Captain-owned release adapters.

The JSON directory adapter is an offline, inspectable delivery boundary.  It
also defines the idempotency semantics expected from remote release adapters:
replaying identical content succeeds, while changing an existing batch id is
a conflict.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict

from agenten.validation.contracts import HoldoutSuite, WorkBatch


class ReleaseConflictError(RuntimeError):
    """A batch id was already released with different immutable content."""


class JsonDirectoryReleaseClient:
    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = asyncio.Lock()

    async def release(self, batch: WorkBatch, holdouts: HoldoutSuite) -> None:
        if batch.batch_id != holdouts.batch_id:
            raise ValueError("batch and holdout suite must have the same batch_id")
        async with self._lock:
            await asyncio.to_thread(self._release_sync, batch, holdouts)

    def _release_sync(self, batch: WorkBatch, holdouts: HoldoutSuite) -> None:
        batch_path = self._root / "batches" / f"{batch.batch_id}.json"
        holdout_path = self._root / "holdouts" / f"{batch.batch_id}.json"
        batch_payload = batch.model_dump(mode="json")
        holdout_payload = holdouts.model_dump(mode="json")

        self._assert_compatible(batch_path, batch_payload, batch.batch_id)
        self._assert_compatible(holdout_path, holdout_payload, batch.batch_id)

        # Publish hidden data first. If the process stops between replaces, an
        # identical retry completes the visible release without changing data.
        self._write_if_missing(holdout_path, holdout_payload)
        self._write_if_missing(batch_path, batch_payload)

    @staticmethod
    def _assert_compatible(path: Path, payload: Dict[str, Any], batch_id: str) -> None:
        if not path.exists():
            return
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != payload:
            raise ReleaseConflictError(f"batch {batch_id!r} was already released differently")

    @staticmethod
    def _write_if_missing(path: Path, payload: Dict[str, Any]) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
