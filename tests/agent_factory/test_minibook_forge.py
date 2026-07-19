from __future__ import annotations

from pathlib import Path

import pytest

from agenten.agent_factory.minibook_forge import MinibookSwarmForge
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
