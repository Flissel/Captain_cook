from __future__ import annotations

from pathlib import Path
import json

import httpx
import pytest

from agenten.agent_factory.forge_contracts import CreationJobV1
from agenten.agent_factory.minibook_forge import (
    MinibookForgeHttpClient,
    MinibookForgeHttpSettings,
    MinibookForgeSettings,
    MinibookSwarmForge,
)
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from tests.agent_factory.test_state_machine import job


class Materializer:
    def __init__(self, path: Path) -> None:
        self.path = path

    def materialize(self, _reference):
        return self.path


class Mapper:
    def map(self, _request: FactoryDispatch) -> CreationJobV1:
        fixture = Path("tests/fixtures/contracts/minibook_creation_job.v1.json")
        return CreationJobV1.model_validate_json(fixture.read_text(encoding="utf-8"))


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

    class Process:
        returncode = 0

        async def communicate(self):
            result_path = tmp_path / "creation-result.json"
            result_path.write_text(
                (Path("tests/fixtures/contracts/minibook_creation_result.v1.json")).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            return b"", b""

    async def start(*command: str, **kwargs: object) -> object:
        received.extend(command)
        assert kwargs["cwd"] == str(tmp_path)
        return Process()

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

    result = await forge.submit(request)

    assert received == [
        "python",
        str(Path("minibook/autogen_swarm.py")),
        "--input-file",
        str(input_path),
        "--non-interactive",
        "--max-runtime-seconds",
        "120",
        "--result-file",
        str(tmp_path / "creation-result.json"),
    ]
    assert result.status == "succeeded"


@pytest.mark.asyncio
async def test_http_forge_returns_typed_submission_status_and_result() -> None:
    result_payload = json.loads(
        Path("tests/fixtures/contracts/minibook_creation_result.v1.json").read_text(encoding="utf-8")
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={
                "creation_job_id": "11111111-1111-4111-8111-111111111111",
                "status": "queued", "subject_version": 1, "replayed": False,
            })
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json=result_payload)
        return httpx.Response(200, json={
            "schema": "minibook.creation-progress.v1",
            "creation_job_id": "11111111-1111-4111-8111-111111111111",
            "subject_version": 1, "attempt": 1, "status": "running",
            "checkpoint": "catalog", "version": 2,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client = MinibookForgeHttpClient(
            mapper=Mapper(),
            settings=MinibookForgeHttpSettings(base_url="https://minibook.invalid", api_key="injected"),
            client=transport,
        )
        request = FactoryDispatch(
            job=job(),
            action=FactoryAction(kind=FactoryActionKind.SUBMIT_FORGE_JOB, attempt=1),
            role=None,
            lease=None,
        )
        receipt = await client.submit(request)
        progress = await client.status(receipt.creation_job_id)
        result = await client.result(receipt.creation_job_id)
    assert receipt.status == "queued"
    assert progress.checkpoint == "catalog"
    assert result.status == "succeeded"
