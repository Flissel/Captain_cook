import json
from pathlib import Path

import httpx
import pytest

from agenten.llm.decompose import DecomposeResponse, SubproblemCandidate
from agenten.llm.model_client import build_replay_model_client
from agenten.planning.alignment import AlignmentPlan, BatchDraft
from agenten.planning.captain_pipeline import BatchEnrichment
from agenten.planning.cli import async_main
from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    ExampleCase,
)


@pytest.mark.asyncio
async def test_cli_reads_project_and_writes_release_summary(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project.md"
    project.write_text("Build the delivery", encoding="utf-8")
    output = tmp_path / "release"
    responses = [
        DecomposeResponse(
            subproblems=[
                SubproblemCandidate(
                    description="Deliver",
                    capability_tags=["delivery"],
                    atomic=True,
                )
            ]
        ).model_dump_json(),
        AlignmentPlan(
            batches=[BatchDraft(batch_id="delivery", title="Delivery", subtask_ids=["sub-01"])]
        ).model_dump_json(),
        BatchEnrichment(
            goal="Deliver",
            capability_tags=["delivery"],
            acceptance_criteria=[
                AcceptanceAssertion(
                    assertion_id="done",
                    kind=AssertionKind.STATUS_EQUALS,
                    expected="succeeded",
                )
            ],
            holdout_cases=[ExampleCase(case_id="hidden", input={"value": 2})],
        ).model_dump_json(),
    ]

    exit_code = await async_main(
        [
            str(project),
            "--output",
            str(output),
            "--capability",
            "delivery",
        ],
        model_client=build_replay_model_client(responses),
    )

    summary = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert summary["released_batches"] == ["delivery"]
    assert (output / "batches" / "delivery.json").exists()


@pytest.mark.asyncio
async def test_gateway_mode_reuses_one_client_for_lookup_and_release(
    tmp_path: Path,
    capsys,
) -> None:
    project = tmp_path / "project.md"
    project.write_text("Build the delivery", encoding="utf-8")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[], request=request)
        return httpx.Response(
            201,
            json={"index": 40 + len(requests)},
            request=request,
        )

    responses = [
        DecomposeResponse(
            subproblems=[
                SubproblemCandidate(
                    description="Deliver",
                    capability_tags=["delivery"],
                    atomic=True,
                )
            ]
        ).model_dump_json(),
        AlignmentPlan(
            batches=[
                BatchDraft(
                    batch_id="delivery",
                    title="Delivery",
                    subtask_ids=["sub-01"],
                )
            ]
        ).model_dump_json(),
        BatchEnrichment(
            goal="Deliver",
            capability_tags=["delivery"],
            acceptance_criteria=[
                AcceptanceAssertion(
                    assertion_id="done",
                    kind=AssertionKind.STATUS_EQUALS,
                    expected="succeeded",
                )
            ],
            holdout_cases=[ExampleCase(case_id="hidden", input={"value": 2})],
        ).model_dump_json(),
    ]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        exit_code = await async_main(
            [
                str(project),
                "--release-mode",
                "gateway",
                "--gateway-url",
                "http://gateway",
                "--capability",
                "delivery",
            ],
            model_client=build_replay_model_client(responses),
            http_client=http,
        )

    assert exit_code == 0
    assert [request.method for request in requests] == ["GET", "POST", "POST"]
    assert not (tmp_path / "release").exists()
    assert json.loads(capsys.readouterr().out)["release_mode"] == "gateway"
