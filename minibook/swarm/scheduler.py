"""Single-worker scheduler for durable Minibook creation jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import UUID

from .contracts import CreationFailure, CreationResultV1
from .job_store import CreationJobStore
from .runner import CreationRunner


CreationRunnerFactory = Callable[[UUID], CreationRunner]


class CreationScheduler:
    """Run persisted jobs serially and expose a thread-safe FastAPI callback."""

    def __init__(
        self,
        *,
        store: CreationJobStore,
        runner_for: CreationRunnerFactory,
    ) -> None:
        self._store = store
        self._runner_for = runner_for
        self._queue: asyncio.Queue[UUID] = asyncio.Queue()
        self._pending: set[UUID] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker: asyncio.Task[None] | None = None

    async def start(self, *, resume: bool = True) -> None:
        if self._worker is not None:
            raise RuntimeError("creation scheduler is already started")
        self._loop = asyncio.get_running_loop()
        self._worker = asyncio.create_task(
            self._run_worker(), name="minibook-creation-worker"
        )
        if resume:
            for job_id in self._store.resumable_job_ids():
                self._enqueue(job_id)

    def schedule(self, job_id: UUID) -> None:
        """Schedule from FastAPI's sync worker thread without touching asyncio state."""

        loop = self._loop
        if loop is None or self._worker is None:
            raise RuntimeError("creation scheduler is not started")
        loop.call_soon_threadsafe(self._enqueue, job_id)

    async def join(self) -> None:
        """Wait until every callback delivered to the loop has completed."""

        await asyncio.sleep(0)
        await self._queue.join()

    async def stop(self) -> None:
        worker = self._worker
        self._worker = None
        self._loop = None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        while True:
            try:
                job_id = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._pending.discard(job_id)
            self._queue.task_done()

    def _enqueue(self, job_id: UUID) -> None:
        if job_id in self._pending:
            return
        self._pending.add(job_id)
        self._queue.put_nowait(job_id)

    async def _run_worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                runner = self._runner_for(job_id)
                await runner.run_slice(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._persist_failure(job_id, exc)
            finally:
                self._pending.discard(job_id)
                self._queue.task_done()

    def _persist_failure(self, job_id: UUID, exc: Exception) -> None:
        if self._store.result(job_id) is not None:
            return
        job = self._store.job(job_id)
        self._store.finish(
            CreationResultV1(
                creation_job_id=job.creation_job_id,
                correlation_id=job.correlation_id,
                subject_version=job.subject_version,
                attempt=job.attempt,
                status="failed",
                failure=CreationFailure(
                    code="internal_error",
                    summary="creation step failed",
                    exception_type=type(exc).__name__,
                ),
            )
        )


__all__ = ["CreationRunnerFactory", "CreationScheduler"]
