"""Background work that must not run nested inside an event publish.

``InMemoryEventBus.publish`` awaits each handler inline, so a handler that
does slow work (a worker executing a subproblem, a retry waiting out its
backoff) blocks whoever published -- usually the Ledger Recorder's writer
loop. ``BackgroundTasks`` runs such work as tracked tasks instead: the
publishing handler returns at once, failures are logged rather than lost,
and owners can wait for or cancel what is still running at shutdown.
"""
import asyncio
import logging
from typing import Any, Awaitable, Callable, Coroutine, Set

logger = logging.getLogger(__name__)


class BackgroundTasks:
    def __init__(self, name: str):
        self._name = name
        # Held strongly so the event loop cannot garbage-collect a running
        # task; each removes itself when it finishes.
        self._tasks: Set["asyncio.Task[None]"] = set()

    @property
    def count(self) -> int:
        return len(self._tasks)

    def spawn(self, work: Coroutine[Any, Any, None], description: str) -> None:
        task = asyncio.ensure_future(self._run(work, description))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        # A task cancelled before it first ran never awaited `work`; close it
        # so it is not reported as "never awaited". No-op once it finished.
        task.add_done_callback(lambda _task: work.close())

    def detach(self, handler: Callable[[Any], Awaitable[None]]) -> Callable[[Any], Awaitable[None]]:
        """Wrap a bus handler so each delivery runs as a background task."""

        async def detached(event: Any) -> None:
            self.spawn(handler(event), f"{type(event).__name__} {getattr(event, 'subproblem_id', '')}".strip())

        return detached

    async def wait_idle(self) -> None:
        """Wait until nothing runs, including tasks spawned meanwhile."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def cancel_all(self) -> None:
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _run(self, work: Coroutine[Any, Any, None], description: str) -> None:
        try:
            await work
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a background task has no caller to report to
            logger.exception("%s background task failed: %s", self._name, description)
