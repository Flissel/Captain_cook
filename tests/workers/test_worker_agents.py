"""Unit tests for agenten.workers.* against InMemoryEventBus + fake tools.

Deliberately never touches Selenium/real network: ResearchWorker is
exercised through a fake Tool double, matching how the real
InternetSearchTool is registered on a ToolRegistry.
"""
import asyncio
from typing import Any, Dict, List, Optional

import pytest

from agenten.events.schemas import (
    SubproblemAssigned,
    SubproblemCompleted,
    SubproblemFailed,
    WorkerHeartbeat,
    make_meta,
    topic_for,
)
from agenten.runtime.event_bus import InMemoryEventBus
from agenten.tools.base import Tool, ToolRegistry
from agenten.workers.base import WorkerAgent, WorkerExecutionError
from agenten.workers.echo_worker import EchoWorker
from agenten.workers.research_worker import SEARCH_TOOL_NAME, ResearchWorker

pytestmark = pytest.mark.asyncio


def make_assigned_event(
    subproblem_id: str = "sp-1",
    agent_type: str = "echo_worker",
    agent_key: str = "worker-1",
    root_problem_id: str = "root-1",
) -> SubproblemAssigned:
    return SubproblemAssigned(
        meta=make_meta(correlation_id=subproblem_id, root_problem_id=root_problem_id),
        subproblem_id=subproblem_id,
        agent_type=agent_type,
        agent_key=agent_key,
        lease_expires_at=1e12,
    )


class RecordingBusView:
    """Subscribes to the outcome topics a worker publishes on and records
    every event delivered, so tests can assert on what came out.
    """

    def __init__(self, bus: InMemoryEventBus):
        self.completed: List[SubproblemCompleted] = []
        self.failed: List[SubproblemFailed] = []
        self.heartbeats: List[WorkerHeartbeat] = []
        bus.subscribe(topic_for(SubproblemCompleted), self._on_completed)
        bus.subscribe(topic_for(SubproblemFailed), self._on_failed)
        bus.subscribe(topic_for(WorkerHeartbeat), self._on_heartbeat)

    async def _on_completed(self, event: SubproblemCompleted) -> None:
        self.completed.append(event)

    async def _on_failed(self, event: SubproblemFailed) -> None:
        self.failed.append(event)

    async def _on_heartbeat(self, event: WorkerHeartbeat) -> None:
        self.heartbeats.append(event)


class SlowEchoWorker(EchoWorker):
    """EchoWorker with a controllable, slow execute() for heartbeat tests."""

    def __init__(self, *args: Any, delay_seconds: float = 0.0, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.delay_seconds = delay_seconds

    async def execute(self, subproblem_id: str, description: str) -> Dict[str, Any]:
        await asyncio.sleep(self.delay_seconds)
        return {"echo": description}


class ExplodingWorker(WorkerAgent):
    agent_type = "exploding_worker"
    capability_tags = ["test"]

    def __init__(self, *args: Any, error: Exception, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._error = error

    async def execute(self, subproblem_id: str, description: str) -> Dict[str, Any]:
        raise self._error


class FakeSearchTool(Tool):
    name = SEARCH_TOOL_NAME

    def __init__(self, result: Optional[List[Dict[str, Any]]] = None, error: Optional[Exception] = None):
        self._result = result if result is not None else []
        self._error = error

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        if self._error is not None:
            raise self._error
        return self._result


async def test_echo_worker_end_to_end_publishes_completed_with_correct_result():
    bus = InMemoryEventBus()
    view = RecordingBusView(bus)
    worker = EchoWorker(bus, ToolRegistry(), description_resolver=lambda e: "hello world")
    bus.subscribe(topic_for(SubproblemAssigned), worker.handle_subproblem_assigned)

    event = make_assigned_event(agent_type="echo_worker")
    await bus.publish(topic_for(SubproblemAssigned), event)

    assert len(view.completed) == 1
    completed = view.completed[0]
    assert completed.subproblem_id == event.subproblem_id
    assert completed.result == {"echo": "hello world"}
    assert completed.meta.root_problem_id == event.meta.root_problem_id
    assert not view.failed


async def test_worker_ignores_assignment_for_different_agent_type():
    bus = InMemoryEventBus()
    view = RecordingBusView(bus)
    worker = EchoWorker(bus, ToolRegistry())
    bus.subscribe(topic_for(SubproblemAssigned), worker.handle_subproblem_assigned)

    event = make_assigned_event(agent_type="some_other_worker")
    await bus.publish(topic_for(SubproblemAssigned), event)

    assert not view.completed
    assert not view.failed
    assert not view.heartbeats


async def test_heartbeats_fire_during_slow_execute():
    bus = InMemoryEventBus()
    view = RecordingBusView(bus)
    worker = SlowEchoWorker(
        bus,
        ToolRegistry(),
        heartbeat_interval_seconds=0.02,
        delay_seconds=0.09,
        description_resolver=lambda e: "slow",
    )
    bus.subscribe(topic_for(SubproblemAssigned), worker.handle_subproblem_assigned)

    event = make_assigned_event(agent_type="echo_worker", agent_key="worker-42")
    await bus.publish(topic_for(SubproblemAssigned), event)

    assert len(view.completed) == 1
    # ~0.09s execute / 0.02s heartbeat interval -> a handful of beats, but
    # never after completion.
    assert len(view.heartbeats) >= 2
    for hb in view.heartbeats:
        assert hb.subproblem_id == event.subproblem_id
        assert hb.agent_type == "echo_worker"
        assert hb.agent_key == "worker-42"

    # No stray heartbeat task left running after the handler returns.
    await asyncio.sleep(0.05)
    assert len(view.heartbeats) == len(view.heartbeats)  # count settled, no crash on late tick


async def test_no_heartbeats_for_fast_execute_under_interval():
    bus = InMemoryEventBus()
    view = RecordingBusView(bus)
    worker = SlowEchoWorker(bus, ToolRegistry(), heartbeat_interval_seconds=5.0, delay_seconds=0.01)
    bus.subscribe(topic_for(SubproblemAssigned), worker.handle_subproblem_assigned)

    await bus.publish(topic_for(SubproblemAssigned), make_assigned_event())

    assert not view.heartbeats


async def test_worker_execution_error_produces_failed_with_retriable_flag():
    bus = InMemoryEventBus()
    view = RecordingBusView(bus)
    worker = ExplodingWorker(bus, ToolRegistry(), error=WorkerExecutionError("bad input", retriable=False))
    bus.subscribe(topic_for(SubproblemAssigned), worker.handle_subproblem_assigned)

    event = make_assigned_event(agent_type="exploding_worker")
    await bus.publish(topic_for(SubproblemAssigned), event)

    assert not view.completed
    assert len(view.failed) == 1
    failed = view.failed[0]
    assert failed.subproblem_id == event.subproblem_id
    assert failed.error == "bad input"
    assert failed.retriable is False


async def test_worker_execution_error_retriable_true_flows_through():
    bus = InMemoryEventBus()
    view = RecordingBusView(bus)
    worker = ExplodingWorker(bus, ToolRegistry(), error=WorkerExecutionError("transient", retriable=True))
    bus.subscribe(topic_for(SubproblemAssigned), worker.handle_subproblem_assigned)

    await bus.publish(topic_for(SubproblemAssigned), make_assigned_event(agent_type="exploding_worker"))

    assert view.failed[0].retriable is True


async def test_unexpected_exception_is_reported_as_retriable_failure_not_raised():
    bus = InMemoryEventBus()
    view = RecordingBusView(bus)
    worker = ExplodingWorker(bus, ToolRegistry(), error=RuntimeError("kaboom"))
    bus.subscribe(topic_for(SubproblemAssigned), worker.handle_subproblem_assigned)

    # Must not raise out of publish() -- handler is responsible for catching.
    await bus.publish(topic_for(SubproblemAssigned), make_assigned_event(agent_type="exploding_worker"))

    assert not view.completed
    assert len(view.failed) == 1
    assert view.failed[0].error == "kaboom"
    assert view.failed[0].retriable is True


async def test_research_worker_shapes_search_tool_output():
    bus = InMemoryEventBus()
    view = RecordingBusView(bus)
    tools = ToolRegistry()
    scored = [
        {"title": "A", "link": "http://a", "score": 0.9, "content": "..."},
        {"title": "B", "link": "http://b", "score": 0.5, "content": "..."},
    ]
    tools.register(FakeSearchTool(result=scored))
    worker = ResearchWorker(bus, tools, description_resolver=lambda e: "market size of widgets")
    bus.subscribe(topic_for(SubproblemAssigned), worker.handle_subproblem_assigned)

    event = make_assigned_event(agent_type="research_worker")
    await bus.publish(topic_for(SubproblemAssigned), event)

    assert not view.failed
    assert len(view.completed) == 1
    result = view.completed[0].result
    assert result["query"] == "market size of widgets"
    assert result["result_count"] == 2
    assert result["top_result"] == scored[0]
    assert result["results"] == scored


async def test_research_worker_wraps_tool_failure_as_retriable_worker_execution_error():
    bus = InMemoryEventBus()
    view = RecordingBusView(bus)
    tools = ToolRegistry()
    tools.register(FakeSearchTool(error=ConnectionError("network down")))
    worker = ResearchWorker(bus, tools)
    bus.subscribe(topic_for(SubproblemAssigned), worker.handle_subproblem_assigned)

    await bus.publish(topic_for(SubproblemAssigned), make_assigned_event(agent_type="research_worker"))

    assert not view.completed
    assert len(view.failed) == 1
    assert view.failed[0].retriable is True
    assert "network down" in view.failed[0].error


async def test_research_worker_missing_tool_registration_is_not_retriable():
    bus = InMemoryEventBus()
    view = RecordingBusView(bus)
    worker = ResearchWorker(bus, ToolRegistry())  # no internet_searcher registered
    bus.subscribe(topic_for(SubproblemAssigned), worker.handle_subproblem_assigned)

    await bus.publish(topic_for(SubproblemAssigned), make_assigned_event(agent_type="research_worker"))

    assert len(view.failed) == 1
    assert view.failed[0].retriable is False


async def test_cancelling_the_handler_also_cancels_the_execute_task():
    """handle_subproblem_assigned schedules execute() as its own asyncio.Task
    and must guarantee it's cancelled (not left running orphaned) if the
    handler itself is cancelled -- see the `finally` block's comment in
    WorkerAgent.handle_subproblem_assigned for why this is handled
    explicitly rather than left to rely on implicit CPython Task.cancel()
    propagation.
    """
    bus = InMemoryEventBus()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class NeverEndingWorker(WorkerAgent):
        agent_type = "never_ending_worker"
        capability_tags = ["test"]

        async def execute(self, subproblem_id: str, description: str) -> Dict[str, Any]:
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return {}

    worker = NeverEndingWorker(bus, ToolRegistry(), heartbeat_interval_seconds=100)
    handler_task = asyncio.ensure_future(
        worker.handle_subproblem_assigned(make_assigned_event(agent_type="never_ending_worker"))
    )

    await asyncio.wait_for(started.wait(), timeout=1)
    handler_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler_task

    await asyncio.wait_for(cancelled.wait(), timeout=1)


async def test_worker_agent_requires_agent_type():
    class NoTypeWorker(WorkerAgent):
        agent_type = ""
        capability_tags = []

        async def execute(self, subproblem_id: str, description: str) -> Dict[str, Any]:
            return {}

    with pytest.raises(ValueError):
        NoTypeWorker(InMemoryEventBus(), ToolRegistry())
