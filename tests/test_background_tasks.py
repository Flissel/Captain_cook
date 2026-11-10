import asyncio

import pytest

from agenten.runtime.background import BackgroundTasks

pytestmark = pytest.mark.asyncio


async def test_detached_handler_returns_before_the_work_finishes():
    release = asyncio.Event()
    seen: list[str] = []
    tasks = BackgroundTasks("test")

    async def handler(event: str) -> None:
        await release.wait()
        seen.append(event)

    detached = tasks.detach(handler)
    await asyncio.wait_for(detached("a"), timeout=1.0)

    assert tasks.count == 1
    assert seen == []
    release.set()
    await tasks.wait_idle()
    assert seen == ["a"]
    assert tasks.count == 0


async def test_wait_idle_includes_tasks_spawned_by_running_tasks():
    seen: list[str] = []
    tasks = BackgroundTasks("test")

    async def second() -> None:
        await asyncio.sleep(0)
        seen.append("second")

    async def first() -> None:
        await asyncio.sleep(0)
        seen.append("first")
        tasks.spawn(second(), "second")

    tasks.spawn(first(), "first")
    await asyncio.wait_for(tasks.wait_idle(), timeout=1.0)

    assert seen == ["first", "second"]


async def test_a_failing_task_is_logged_and_forgotten(caplog):
    tasks = BackgroundTasks("test")

    async def broken() -> None:
        raise RuntimeError("boom")

    with caplog.at_level("ERROR"):
        tasks.spawn(broken(), "subproblem sp-9")
        await tasks.wait_idle()

    assert tasks.count == 0
    assert any("subproblem sp-9" in record.getMessage() for record in caplog.records)


async def test_cancel_all_stops_waiting_tasks():
    tasks = BackgroundTasks("test")
    tasks.spawn(asyncio.sleep(3600), "long wait")

    await asyncio.wait_for(tasks.cancel_all(), timeout=1.0)

    assert tasks.count == 0
