from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from agenten.agent_factory.capability_factory_production import (
    CapabilityProductionConfigurationError,
    MinibookSwarmCreationHttpPort,
)
from agenten.agent_factory.contracts import AgentFactoryJobV2
from agenten.agent_factory.forge_contracts import CreationJobV1, CreationResultV1
from minibook.tests.test_creation_evidence_api import (
    _completion_payload,
    _preparation_payload,
    _result,
)


FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "contracts"
    / "minibook_creation_job.v1.json"
)


class Response:
    def __init__(
        self,
        status_code: int,
        payload: Any,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class SequencedHttp:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], deque[Response]] = defaultdict(deque)
        self.calls: list[tuple[str, str]] = []

    def add(self, method: str, suffix: str, *responses: Response) -> None:
        self.responses[(method, suffix)].extend(responses)

    async def get(self, url: str, **kwargs: Any) -> Response:
        del kwargs
        return self._next("GET", url)

    async def post(self, url: str, **kwargs: Any) -> Response:
        del kwargs
        return self._next("POST", url)

    def _next(self, method: str, url: str) -> Response:
        suffix = url.removeprefix("http://127.0.0.1:8001")
        self.calls.append((method, suffix))
        return self.responses[(method, suffix)].popleft()


def _creation_job() -> CreationJobV1:
    return CreationJobV1.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def _factory_job(creation: CreationJobV1) -> AgentFactoryJobV2:
    payload = json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures"
            / "agent_factory"
            / "agent_factory_job.v2.json"
        ).read_text(encoding="utf-8")
    )
    payload.update(
        {
            "job_id": str(creation.factory_job_id),
            "correlation_id": str(creation.correlation_id),
            "subject_version": creation.subject_version,
            "deadline_at": creation.deadline_at.isoformat(),
        }
    )
    return AgentFactoryJobV2.model_validate(payload)


@pytest.mark.asyncio
async def test_minibook_creation_http_port_requires_submit_before_reads() -> None:
    creation = _creation_job()
    port = MinibookSwarmCreationHttpPort(
        "http://127.0.0.1:8001",
        SecretStr("test-key"),
        SequencedHttp(),
    )

    with pytest.raises(CapabilityProductionConfigurationError, match="submitted"):
        await port.preparation_blocks(_factory_job(creation), creation)


@pytest.mark.asyncio
async def test_minibook_creation_http_port_polls_pending_evidence_and_result() -> None:
    creation = _creation_job()
    job = _factory_job(creation)
    result = _result()
    job_id = str(creation.creation_job_id)
    http = SequencedHttp()
    http.add(
        "POST",
        "/api/v1/creation-jobs",
        Response(
            202,
            {
                "creation_job_id": job_id,
                "status": "queued",
                "subject_version": creation.subject_version,
                "replayed": False,
            },
        ),
    )
    http.add(
        "GET",
        f"/api/v1/creation-jobs/{job_id}/preparation-blocks",
        Response(409, {"detail": "pending"}, headers={"Retry-After": "0"}),
        Response(200, _preparation_payload()["blocks"]),
    )
    http.add(
        "GET",
        f"/api/v1/creation-jobs/{job_id}/result",
        Response(409, {"detail": "pending"}, headers={"Retry-After": "0"}),
        Response(200, result.model_dump(mode="json", by_alias=True)),
    )
    http.add(
        "GET",
        f"/api/v1/creation-jobs/{job_id}/completion-block",
        Response(409, {"detail": "pending"}, headers={"Retry-After": "0"}),
        Response(200, _completion_payload(result)["block"]),
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    port = MinibookSwarmCreationHttpPort(
        "http://127.0.0.1:8001",
        SecretStr("test-key"),
        http,
        sleep=sleep,
        clock=lambda: datetime(2029, 1, 1, tzinfo=timezone.utc),
    )

    await port.submit(creation)
    preparation = await port.preparation_blocks(job, creation)
    observed_result = await port.result(creation.creation_job_id)
    completion = await port.completion_block(job, observed_result)

    assert tuple(block.phase.value for block in preparation) == (
        "blueprint_created",
        "tool_candidate_tested",
    )
    assert observed_result.model_dump(mode="json", by_alias=True) == result.model_dump(
        mode="json", by_alias=True
    )
    assert completion.phase.value == "agent_code_created"
    assert sleeps == [0.0, 0.0, 0.0]
    assert http.calls[0] == ("POST", "/api/v1/creation-jobs")


@pytest.mark.asyncio
async def test_minibook_creation_http_port_stops_polling_at_creation_deadline() -> None:
    creation = _creation_job()
    http = SequencedHttp()
    job_id = str(creation.creation_job_id)
    http.add(
        "POST",
        "/api/v1/creation-jobs",
        Response(
            202,
            {
                "creation_job_id": job_id,
                "status": "queued",
                "subject_version": creation.subject_version,
            },
        ),
    )
    http.add(
        "GET",
        f"/api/v1/creation-jobs/{job_id}/result",
        Response(409, {"detail": "pending"}, headers={"Retry-After": "1"}),
    )
    port = MinibookSwarmCreationHttpPort(
        "http://127.0.0.1:8001",
        SecretStr("test-key"),
        http,
        sleep=lambda _delay: _no_wait(),
        clock=lambda: creation.deadline_at,
    )

    await port.submit(creation)
    with pytest.raises(TimeoutError, match="deadline"):
        await port.result(creation.creation_job_id)


async def _no_wait() -> None:
    return None
