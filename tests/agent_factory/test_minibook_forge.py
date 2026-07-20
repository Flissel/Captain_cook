from __future__ import annotations

from pathlib import Path

import pytest

from agenten.agent_factory.minibook_forge import MinibookForgeSettings, MinibookSwarmForge
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from tests.agent_factory.test_state_machine import job


class Materializer:
    def __init__(self, path: Path) -> None:
        self.path = path

    def materialize(self, _reference):
        return self.path


@pytest.mark.asyncio
async def test_forge_rejects_a_non_file_input_before_spawning(tmp_path: Path) -> None:
    request = FactoryDispatch(
        job=job(),
        action=FactoryAction(kind=FactoryActionKind.SUBMIT_FORGE_JOB, attempt=1),
        role=None,
        lease=None,
    )
    forge = MinibookSwarmForge(materializer=Materializer(tmp_path / "missing.md"))

    with pytest.raises(RuntimeError, match="materialize"):
        await forge.submit(request)


@pytest.mark.asyncio
async def test_forge_starts_a_noninteractive_deadline_bounded_input_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("# Team", encoding="utf-8")
    received: list[object] = []

    async def start(*command: str, **kwargs: object) -> object:
        received.extend(command)
        assert kwargs["cwd"] == str(tmp_path)
        return object()

    monkeypatch.setattr("agenten.agent_factory.minibook_forge.asyncio.create_subprocess_exec", start)
    request = FactoryDispatch(
        job=job(),
        action=FactoryAction(kind=FactoryActionKind.SUBMIT_FORGE_JOB, attempt=1),
        role=None,
        lease=None,
    )
    forge = MinibookSwarmForge(
        materializer=Materializer(input_path),
        settings=MinibookForgeSettings(working_directory=tmp_path, max_runtime_seconds=120),
    )

    await forge.submit(request)

    assert received == [
        "python",
        str(Path("minibook/autogen_swarm.py")),
        "--input-file",
        str(input_path),
        "--non-interactive",
        "--max-runtime-seconds",
        "120",
    ]
