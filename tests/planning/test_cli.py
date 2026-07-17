import json
from pathlib import Path

import httpx
import pytest

from agenten.llm.decompose import DecomposeResponse, SubproblemCandidate
from agenten.llm.model_client import build_replay_model_client
from agenten.planning.alignment import AlignmentPlan, BatchDraft
from agenten.planning.captain_pipeline import BatchEnrichment
from agenten.planning.cli import GatewayPlanningConfigurationError, async_main
from agenten.planning.run_models import CaptainRunState, CaptainRunStatus
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
    assert summary["canonical_plan_id"].startswith("plan-")
    assert len(summary["worker_pool"]) == 5
    assert (output / "source" / "input.md").exists()
    assert (output / "plans" / "index.md").exists()
    assert (output / "contracts" / "batches" / "delivery.json").exists()
    assert (output / "holdouts" / "delivery.json").exists()


@pytest.mark.asyncio
async def test_cli_can_publish_an_allowlisted_mixed_target_plan(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project.md"
    project.write_text("Build an n8n tool and dependent AutoGen team", encoding="utf-8")
    output = tmp_path / "release"
    responses = [
        DecomposeResponse(
            subproblems=[
                SubproblemCandidate(
                    description="Build the tool",
                    capability_tags=["delivery"],
                    atomic=True,
                ),
                SubproblemCandidate(
                    description="Build the team",
                    capability_tags=["delivery"],
                    atomic=True,
                ),
            ]
        ).model_dump_json(),
        AlignmentPlan(
            batches=[
                BatchDraft(
                    batch_id="lead-tool",
                    title="Lead Tool",
                    subtask_ids=["sub-01"],
                    target="n8n",
                ),
                BatchDraft(
                    batch_id="sales-team",
                    title="Sales Team",
                    subtask_ids=["sub-02"],
                    depends_on=["lead-tool"],
                    target="autogen",
                ),
            ]
        ).model_dump_json(),
        BatchEnrichment(
            goal="Build the tool",
            capability_tags=["delivery"],
            acceptance_criteria=[
                AcceptanceAssertion(
                    assertion_id="tool-done",
                    kind=AssertionKind.STATUS_EQUALS,
                    expected="succeeded",
                )
            ],
            holdout_cases=[ExampleCase(case_id="tool-hidden", input={"value": 1})],
        ).model_dump_json(),
        BatchEnrichment(
            goal="Build the team",
            capability_tags=["delivery"],
            acceptance_criteria=[
                AcceptanceAssertion(
                    assertion_id="team-done",
                    kind=AssertionKind.STATUS_EQUALS,
                    expected="succeeded",
                )
            ],
            holdout_cases=[ExampleCase(case_id="team-hidden", input={"value": 2})],
        ).model_dump_json(),
    ]

    exit_code = await async_main(
        [
            str(project),
            "--output",
            str(output),
            "--target",
            "n8n",
            "--allowed-target",
            "n8n",
            "--allowed-target",
            "autogen",
            "--capability",
            "delivery",
        ],
        model_client=build_replay_model_client(responses),
    )

    summary = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert summary["released_batches"] == ["lead-tool", "sales-team"]
    lead = json.loads(
        (output / "contracts" / "batches" / "lead-tool.json").read_text(encoding="utf-8")
    )
    team = json.loads(
        (output / "contracts" / "batches" / "sales-team.json").read_text(encoding="utf-8")
    )
    assert (lead["target"], team["target"]) == ("n8n", "autogen")


@pytest.mark.asyncio
async def test_gateway_mode_fails_before_model_creation_when_token_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project.md"
    project.write_text("Build the delivery", encoding="utf-8")
    monkeypatch.delenv("CAPTAIN_GATEWAY_TOKEN", raising=False)
    model_was_built = False

    def unexpected_model(*args, **kwargs):
        nonlocal model_was_built
        model_was_built = True
        raise AssertionError("model client must not be built")

    monkeypatch.setattr("agenten.planning.cli.build_model_client", unexpected_model)

    with pytest.raises(GatewayPlanningConfigurationError, match="CAPTAIN_GATEWAY_TOKEN"):
        await async_main(
            [
                str(project),
                "--release-mode",
                "gateway",
                "--gateway-url",
                "http://gateway",
                "--capability",
                "delivery",
            ]
        )

    assert model_was_built is False


@pytest.mark.asyncio
async def test_gateway_mode_releases_compiled_contracts_over_one_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project.md"
    project.write_text("Build the delivery", encoding="utf-8")
    output = tmp_path / "release"
    run_dir = tmp_path / "runs"
    monkeypatch.setenv("CAPTAIN_GATEWAY_TOKEN", "captain-secret")
    gateway_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gateway_requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[], request=request)
        return httpx.Response(
            201,
            json={"index": 40 + len(gateway_requests)},
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

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        exit_code = await async_main(
            [
                str(project),
                "--output",
                str(output),
                "--release-mode",
                "gateway",
                "--gateway-url",
                "http://gateway",
                "--run-id",
                "run-1",
                "--run-dir",
                str(run_dir),
                "--capability",
                "delivery",
            ],
            model_client=build_replay_model_client(responses),
            http_client=http,
        )

    assert exit_code == 0
    write_requests = [request for request in gateway_requests if request.method == "POST"]
    assert [json.loads(request.content)["block_type"] for request in write_requests] == [
        "work_batch",
        "holdout",
    ]
    assert all(
        request.headers["authorization"] == "Bearer captain-secret"
        for request in gateway_requests
    )
    assert (output / "contracts" / "batches" / "delivery.json").exists()
    checkpoint = CaptainRunState.model_validate_json(
        (run_dir / "run-1.json").read_text(encoding="utf-8")
    )
    assert checkpoint.status is CaptainRunStatus.RELEASED


@pytest.mark.asyncio
async def test_run_id_requires_gateway_mode_before_model_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project.md"
    project.write_text("Build the delivery", encoding="utf-8")
    model_was_built = False

    def unexpected_model(*args, **kwargs):
        nonlocal model_was_built
        model_was_built = True
        raise AssertionError("model client must not be built")

    monkeypatch.setattr("agenten.planning.cli.build_model_client", unexpected_model)

    with pytest.raises(GatewayPlanningConfigurationError, match="gateway release mode"):
        await async_main(
            [
                str(project),
                "--run-id",
                "run-1",
                "--capability",
                "delivery",
            ]
        )

    assert model_was_built is False
