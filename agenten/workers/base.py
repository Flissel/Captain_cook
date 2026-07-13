"""Worker agent base class: the seam between the event-driven supply-chain
pipeline (SubproblemAssigned in, SubproblemCompleted/Failed out) and a
concrete unit of work (research, code generation, echo, ...).

Business logic here depends only on ``agenten.events.schemas`` and
``agenten.runtime.event_bus.EventBus`` -- never on ``autogen_core`` -- so
every worker stays importable and unit-testable with ``InMemoryEventBus``
and zero AutoGen installed. The optional ``make_routed_agent_class`` factory
at the bottom of this module is the thin adapter that wires a ``WorkerAgent``
into a real AutoGen Core runtime once unit U11 assembles the full pipeline.
"""
from abc import ABC, abstractmethod
import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from agenten.events.schemas import (
    EventMeta,
    SubproblemAssigned,
    SubproblemCompleted,
    SubproblemFailed,
    WorkerHeartbeat,
    make_meta,
    topic_for,
)
from agenten.runtime.event_bus import EventBus
from agenten.tools.base import ToolRegistry

logger = logging.getLogger(__name__)

# A resolver maps an assignment event to the free-text description of the
# subproblem it should execute. It may return a plain string or an
# awaitable of one.
#
# NOTE: agenten.events.schemas.SubproblemAssigned (frozen by unit U0) does
# not itself carry a `description` field -- only SubproblemProposed does.
# Rather than guess at how the eventual integration (unit U11) threads that
# text through to the worker (a local cache keyed off SubproblemProposed? a
# LedgerQuery lookup by subproblem_id? something else entirely), WorkerAgent
# accepts a pluggable resolver and falls back to using the bare
# `subproblem_id` as the description so the base class is fully functional
# standalone and in tests.
DescriptionResolver = Callable[[SubproblemAssigned], Union[str, Awaitable[str]]]


class WorkerExecutionError(Exception):
    """Raised by WorkerAgent.execute() to signal a handled failure.

    ``retriable`` flows straight through to the published
    ``SubproblemFailed.retriable`` flag, which downstream units (retry /
    circuit-breaker policy) use to decide whether to re-lease the
    subproblem or give up on it.
    """

    def __init__(self, message: str, retriable: bool = True):
        self.retriable = retriable
        super().__init__(message)


class WorkerAgent(ABC):
    """Base class for a worker that executes one subproblem at a time.

    Subclasses set the class attributes ``agent_type`` (must be unique
    across all worker types registered in the system -- it's the routing
    key SpawnCoordinatorAgent/CapabilityRegistry use, and what
    ``handle_subproblem_assigned`` filters incoming assignments on) and
    ``capability_tags`` (the tags this worker type can serve), and implement
    ``execute()``.
    """

    agent_type: str = ""
    capability_tags: List[str] = []

    def __init__(
        self,
        bus: EventBus,
        tools: ToolRegistry,
        heartbeat_interval_seconds: float = 20.0,
        description_resolver: Optional[DescriptionResolver] = None,
    ):
        if not self.agent_type:
            raise ValueError(f"{type(self).__name__}.agent_type must be a non-empty string")
        self.bus = bus
        self.tools = tools
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._description_resolver = description_resolver

    @abstractmethod
    async def execute(self, subproblem_id: str, description: str) -> Dict[str, Any]:
        """Do the actual work; return a JSON-ish result dict.

        Raise WorkerExecutionError(retriable=...) on a handled failure.
        Any other exception raised here is still caught by
        handle_subproblem_assigned and reported as a (retriable) failure,
        but doing so skips the chance to make a considered retriable
        decision, so prefer raising WorkerExecutionError explicitly.
        """

    async def handle_subproblem_assigned(self, event: SubproblemAssigned) -> None:
        """React to a SubproblemAssigned event addressed to this worker type.

        Ignores assignments for other agent types (the same event may be
        broadcast to every worker-type handler subscribed to the topic).
        Otherwise runs ``execute()`` as a background task while a sibling
        heartbeat loop periodically publishes WorkerHeartbeat -- this is
        what lets a Reaper-style supervisor unit detect a hung/dead worker
        well before the lease fully expires, instead of getting zero signal
        until then. Publishes exactly one of SubproblemCompleted /
        SubproblemFailed when execute() settles; never lets an exception
        (expected or not) propagate out of this handler.
        """
        if event.agent_type != self.agent_type:
            return

        description = await self._resolve_description(event)
        execute_task: "asyncio.Task[Dict[str, Any]]" = asyncio.ensure_future(
            self.execute(event.subproblem_id, description)
        )
        heartbeat_task = asyncio.ensure_future(self._heartbeat_loop(event, execute_task))

        try:
            result = await execute_task
        except WorkerExecutionError as exc:
            await self._publish_failed(event, str(exc), exc.retriable)
            return
        except Exception as exc:  # noqa: BLE001 - deliberately broad: never leak out of the handler
            logger.exception(
                "worker %s: unexpected error executing subproblem %s", self.agent_type, event.subproblem_id
            )
            await self._publish_failed(event, str(exc), True)
            return
        finally:
            # Cleanup that covers every exit path, including one not caught
            # above: asyncio.CancelledError is a BaseException (not an
            # Exception), so it skips both except clauses and lands here if
            # the caller cancels the task running this handler (e.g. on
            # shutdown) while we're awaiting execute_task. CPython's
            # Task.cancel() happens to propagate to a Task currently being
            # directly `await`-ed (execute_task is our _fut_waiter at that
            # point), so execute_task doesn't strictly go unmanaged today --
            # but that's an implementation detail of the direct-await shape
            # of this method, not something to depend on. Cancelling both
            # tasks explicitly here keeps cleanup correct even if this
            # method is later refactored to do other awaits in between, and
            # is a no-op (cancel() on an already-done Task) on every other
            # exit path.
            heartbeat_task.cancel()
            if not execute_task.done():
                execute_task.cancel()
            await asyncio.gather(heartbeat_task, execute_task, return_exceptions=True)

        await self._publish_completed(event, result)

    async def _resolve_description(self, event: SubproblemAssigned) -> str:
        if self._description_resolver is None:
            return event.subproblem_id
        outcome = self._description_resolver(event)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return outcome

    async def _heartbeat_loop(self, event: SubproblemAssigned, execute_task: "asyncio.Task[Any]") -> None:
        try:
            while not execute_task.done():
                await asyncio.sleep(self.heartbeat_interval_seconds)
                if execute_task.done():
                    break
                await self._publish_heartbeat(event)
        except asyncio.CancelledError:
            pass

    def _derive_meta(self, event: SubproblemAssigned) -> EventMeta:
        return make_meta(
            correlation_id=event.subproblem_id,
            root_problem_id=event.meta.root_problem_id,
            attempt=event.meta.attempt,
            constitution_version=event.meta.constitution_version,
        )

    async def _publish_heartbeat(self, event: SubproblemAssigned, progress_note: str = "") -> None:
        heartbeat = WorkerHeartbeat(
            meta=self._derive_meta(event),
            subproblem_id=event.subproblem_id,
            agent_type=self.agent_type,
            agent_key=event.agent_key,
            progress_note=progress_note,
        )
        await self.bus.publish(topic_for(WorkerHeartbeat), heartbeat)

    async def _publish_completed(self, event: SubproblemAssigned, result: Dict[str, Any]) -> None:
        completed = SubproblemCompleted(
            meta=self._derive_meta(event),
            subproblem_id=event.subproblem_id,
            result=result,
        )
        await self.bus.publish(topic_for(SubproblemCompleted), completed)

    async def _publish_failed(self, event: SubproblemAssigned, error: str, retriable: bool) -> None:
        failed = SubproblemFailed(
            meta=self._derive_meta(event),
            subproblem_id=event.subproblem_id,
            error=error,
            retriable=retriable,
        )
        await self.bus.publish(topic_for(SubproblemFailed), failed)


# --- Optional AutoGen Core adapter ------------------------------------------
#
# Kept behind a soft import so every module above this line -- the actual
# business logic -- stays importable with zero AutoGen installed, matching
# the rest of this migration (see agenten/runtime/event_bus.py).
try:
    import autogen_core
    from autogen_core import MessageContext, RoutedAgent, message_handler
except ImportError:  # pragma: no cover - exercised by CI without autogen_core installed
    autogen_core = None
    RoutedAgent = None  # type: ignore[assignment,misc]
    MessageContext = None  # type: ignore[assignment,misc]
    message_handler = None  # type: ignore[assignment]


def make_routed_agent_class(worker: WorkerAgent):
    """Build an AutoGen Core ``RoutedAgent`` subclass that delegates
    ``SubproblemAssigned`` messages to ``worker.handle_subproblem_assigned``.

    Returns ``None`` if ``autogen_core`` is not installed (there is no
    ``RoutedAgent`` base class to subclass in that case) -- callers should
    treat that as "AutoGen wiring not available in this environment", not
    as an error, since the pure ``WorkerAgent`` half is fully usable without
    it (e.g. behind ``InMemoryEventBus`` in tests, or a future non-AutoGen
    runtime).

    Intended AutoGen wiring (assembled by unit U11's orchestration/pipeline
    module, not exercised here):

    1. Registration binds this class to the runtime under the worker's own
       ``agent_type`` as the AutoGen agent type::

           RoutedCls = make_routed_agent_class(research_worker)
           await RoutedCls.register(runtime, research_worker.agent_type, lambda: RoutedCls())

    2. A ``TypeSubscription`` (or an explicit ``add_subscription`` call, since
       we're not using the ``@type_subscription`` class decorator here to
       keep this factory usable for arbitrary worker instances) binds the
       topic ``topic_for(SubproblemAssigned)`` to that agent type, e.g.::

           await runtime.add_subscription(
               TypeSubscription(topic_type=topic_for(SubproblemAssigned), agent_type=research_worker.agent_type)
           )

       This mirrors event_bus.py's note that AutoGen Core subscribes agent
       TYPES to topics, not arbitrary callables -- InMemoryEventBus.subscribe()
       is the *test* mechanism; this is the *real* one.
    3. AutoGen Core delivers every ``SubproblemAssigned`` published on that
       topic to a (runtime-managed, lazily-instantiated) instance of the
       returned class, whose ``@message_handler`` method below just forwards
       to the already-tested ``WorkerAgent.handle_subproblem_assigned`` --
       the *only* place with business logic stays the plain worker class, so
       AutoGen-specific code is a zero-logic shim that's easy to keep correct
       as autogen_core's own API evolves.
    4. Because ``handle_subproblem_assigned`` itself already filters on
       ``event.agent_type == self.agent_type``, subscribing every worker
       type's RoutedAgent to a single shared ``SubproblemAssigned`` topic is
       safe (each ignores assignments addressed to other agent types) --
       useful if a deployment prefers one broad topic over one topic per
       worker type.
    """
    if autogen_core is None:
        return None

    class _WorkerRoutedAgent(RoutedAgent):
        def __init__(self) -> None:
            super().__init__(description=f"{worker.agent_type} worker agent")
            self._worker = worker

        @message_handler
        async def on_subproblem_assigned(self, message: SubproblemAssigned, ctx: MessageContext) -> None:
            await self._worker.handle_subproblem_assigned(message)

    _WorkerRoutedAgent.__name__ = f"{worker.agent_type.title().replace('_', '')}RoutedAgent"
    _WorkerRoutedAgent.__qualname__ = _WorkerRoutedAgent.__name__
    return _WorkerRoutedAgent
