"""Atomic JSON persistence for resumable Captain runs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import os
from pathlib import Path
import re
import time
from typing import AsyncContextManager, BinaryIO, Protocol

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
            lock_path = self._root / ".locks" / f"{run_id}.lock"
            handle = await asyncio.to_thread(self._acquire_file_lock, lock_path)
            try:
                yield
            finally:
                await asyncio.to_thread(self._release_file_lock, handle)

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

    @staticmethod
    def _acquire_file_lock(path: Path) -> BinaryIO:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            return handle
        except BaseException:
            handle.close()
            raise

    @staticmethod
    def _release_file_lock(handle: BinaryIO) -> None:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
