"""Atomic JSON persistence for resumable Captain runs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import os
from pathlib import Path
import re
from typing import AsyncContextManager, Protocol

from pydantic import ValidationError

from agenten.planning.run_models import CaptainRunState


_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class CaptainRunStoreError(RuntimeError):
    """A persisted run checkpoint is unreadable or inconsistent."""


class CaptainRunStore(Protocol):
    async def load(self, run_id: str) -> CaptainRunState | None: ...

    async def save(self, state: CaptainRunState) -> None: ...

    def lock(self, run_id: str) -> AsyncContextManager[None]: ...


class JsonCaptainRunStore:
    """Persist one validated state file per safe run id using ``os.replace``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).resolve()
        self._locks: dict[Path, asyncio.Lock] = {}
        self._write_lock = asyncio.Lock()

    async def load(self, run_id: str) -> CaptainRunState | None:
        path = self._path(run_id)
        return await asyncio.to_thread(self._load_sync, path, run_id)

    async def save(self, state: CaptainRunState) -> None:
        path = self._path(state.run_id)
        async with self._write_lock:
            await asyncio.to_thread(self._save_sync, path, state)

    @asynccontextmanager
    async def lock(self, run_id: str) -> AsyncIterator[None]:
        path = self._path(run_id)
        lock = self._locks.setdefault(path, asyncio.Lock())
        async with lock:
            yield

    def _path(self, run_id: str) -> Path:
        if not _SAFE_RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
            raise ValueError("run_id must be a safe filename component")
        return self._root / f"{run_id}.json"

    @staticmethod
    def _load_sync(path: Path, run_id: str) -> CaptainRunState | None:
        if not path.exists():
            return None
        try:
            state = CaptainRunState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            raise CaptainRunStoreError(f"run checkpoint {run_id!r} is invalid") from None
        if state.run_id != run_id:
            raise CaptainRunStoreError(f"run checkpoint {run_id!r} contains another run id")
        return state

    @staticmethod
    def _save_sync(path: Path, state: CaptainRunState) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(state.model_dump_json(indent=2))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
