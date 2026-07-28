from __future__ import annotations

from pathlib import Path
import json
from datetime import datetime, timezone

import httpx
import pytest

from agenten.agent_factory.forge_contracts import CreationJobV1
from agenten.agent_factory.contracts import AgentFactoryJob
from agenten.agent_factory.minibook_forge import (
    CaptainCreationJobMapper,
    MinibookForgeHttpClient,
    MinibookForgeHttpSettings,
    MinibookForgeSettings,
    MinibookSwarmForge,
)
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_factory.skill_workflow_contracts import (
    CodebaseInventoryV1,
    CodexBuildBriefV1,
    FactorySkillStep,
)
from tests.agent_factory.test_skill_workflow_contracts import (
    brief_payload,
    inventory_payload,
)
from tests.agent_factory.test_state_machine import job_v3


def job() -> AgentFactoryJob:
    return AgentFactoryJob.model_validate({
        "schema": "captain.agent-factory-job.v1",
        "event_id": "00000000-0000-0000-0000-000000000001",
        "correlation_id": "00000000-0000-0000-0000-000000000002",
        "occurred_at": datetime(2026, 7, 21, tzinfo=timezone.utc),
        "producer": "captain",
        "job_id": "00000000-0000-0000-0000-000000000003",
        "subject_version": 1,
        "input_ref": {"uri": "artifact://factory/input", "sha256": "a" * 64, "media_type": "text/markdown"},
        "required_capability": "support_triage",
        "acceptance_assertion_ids": ["schema_valid", "real_case_green"],
        "max_behavioral_iterations": 5,
    })


class Materializer:
    def __init__(self, path: Path) -> None:
        self.path = path

    def materialize(self, _reference):
        return self.path


class Mapper:
    def map(self, _request: FactoryDispatch) -> CreationJobV1:
        fixture = Path("tests/fixtures/contracts/minibook_creation_job.v1.json")
        return CreationJobV1.model_validate_json(fixture.read_text(encoding="utf-8"))


def _workflow_evidence():
    factory_job = job_v3(mode="demo")

    def bind(payload: dict[str, object]) -> dict[str, object]:
        invocation = payload["invocation"]
        assert isinstance(invocation, dict)
        lease = invocation["lease"]
        assert isinstance(lease, dict)
        invocation.update(
            {
                "job_id": str(factory_job.job_id),
                "correlation_id": str(factory_job.correlation_id),
                "subject_version": factory_job.subject_version,
                "attempt": 1,
                "input_ref": factory_job.input_ref.model_dump(mode="json"),
                "input_sha256": factory_job.input_ref.sha256,
                "acceptance_assertion_ids": list(factory_job.acceptance_assertion_ids),
            }
        )
        lease.update(
            {
                "job_id": str(factory_job.job_id),
                "correlation_id": str(factory_job.correlation_id),
                "subject_version": factory_job.subject_version,
                "attempt": 1,
            }
        )
        payload.update(
            {
                "job_id": str(factory_job.job_id),
                "correlation_id": str(factory_job.correlation_id),
                "subject_version": factory_job.subject_version,
                "attempt": 1,
                "acceptance_assertion_ids": list(factory_job.acceptance_assertion_ids),
            }
        )
        return payload

    inventory = CodebaseInventoryV1.model_validate(bind(inventory_payload()))
    brief_data = bind(brief_payload())
    brief_data["context_refs"] = [inventory.artifact_ref.model_dump(mode="json")]
    invocation = brief_data["invocation"]
    assert isinstance(invocation, dict)
    assignment = brief_data["build_assignment"]
    assert isinstance(assignment, dict)
    assignment.update(
        {
            "correlation_id": str(factory_job.correlation_id),
            "subject_version": factory_job.subject_version,
            "attempt": 1,
            "compiled_spec_ref": factory_job.compiled_spec_ref.model_dump(mode="json"),
            "dependency_graph_ref": factory_job.dependency_graph_ref.model_dump(mode="json"),
            "released_skill": {
                "skill_id": invocation["released_skill"]["skill_id"],
                "version": invocation["released_skill"]["version"],
                "content_ref": invocation["released_skill"]["content_ref"],
                "content_sha256": invocation["released_skill"]["content_sha256"],
            },
            "public_assertion_ids": list(factory_job.acceptance_assertion_ids),
            "idempotency_key": invocation["idempotency_key"],
            "workspace_ref": invocation["lease"]["workspace_ref"],
            "deadline_at": factory_job.deadline_at,
        }
    )
    brief = CodexBuildBriefV1.model_validate(brief_data)
    return factory_job, inventory, brief


class ForgeEvidence:
    def __init__(self, inventory, brief) -> None:
        self.artifacts = (inventory, brief)

    def workflow_artifacts(self, job_id):
        assert job_id == self.artifacts[0].job_id
        return self.artifacts

    def released_for(self, job, step):
        assert job.job_id == self.artifacts[1].job_id
        assert step is FactorySkillStep.BRIEF_CODEX
        return self.artifacts[1].invocation.released_skill


def test_creation_job_mapper_uses_exact_captain_brief_and_is_deterministic() -> None:
    factory_job, inventory, brief = _workflow_evidence()
    request = FactoryDispatch(
        job=factory_job,
        action=FactoryAction(kind=FactoryActionKind.SUBMIT_FORGE_JOB, attempt=1),
        role=None,
        lease=None,
    )
    mapper = CaptainCreationJobMapper(evidence=ForgeEvidence(inventory, brief))

    first = mapper.map(request)
    second = mapper.map(request)

    assignment = brief.build_assignment
    assert first == second
    assert first.creation_job_id == assignment.creation_job_id
    assert first.factory_job_id == factory_job.job_id
    assert first.correlation_id == factory_job.correlation_id
    assert first.causation_id == brief.invocation_id
    assert first.subject_version == factory_job.subject_version
    assert first.attempt == request.action.attempt
    assert first.idempotency_key == assignment.idempotency_key
    assert first.input_ref.sha256 == factory_job.input_ref.sha256
    assert first.compiled_spec_ref.sha256 == factory_job.compiled_spec_ref.sha256
    assert first.dependency_graph_ref.sha256 == factory_job.dependency_graph_ref.sha256
    assert first.released_skill.content_sha256 == brief.invocation.released_skill.content_sha256
    assert first.public_assertion_ids == factory_job.acceptance_assertion_ids
    assert first.deadline_at == factory_job.deadline_at


def test_creation_job_mapper_rejects_missing_blueprint_inventory_binding() -> None:
    factory_job, inventory, brief = _workflow_evidence()
    changed = brief.model_copy(update={"context_refs": ()})
    mapper = CaptainCreationJobMapper(evidence=ForgeEvidence(inventory, changed))

    with pytest.raises(RuntimeError, match="inventory"):
        mapper.map(
            FactoryDispatch(
                job=factory_job,
                action=FactoryAction(kind=FactoryActionKind.SUBMIT_FORGE_JOB, attempt=1),
                role=None,
                lease=None,
            )
        )


def test_creation_job_mapper_rejects_brief_for_different_input() -> None:
    factory_job, inventory, brief = _workflow_evidence()
    changed_invocation = brief.invocation.model_copy(
        update={"input_ref": inventory.artifact_ref}
    )
    changed = brief.model_copy(update={"invocation": changed_invocation})
    mapper = CaptainCreationJobMapper(evidence=ForgeEvidence(inventory, changed))

    with pytest.raises(RuntimeError, match="assignment"):
        mapper.map(
            FactoryDispatch(
                job=factory_job,
                action=FactoryAction(
                    kind=FactoryActionKind.SUBMIT_FORGE_JOB,
                    attempt=1,
                ),
                role=None,
                lease=None,
            )
        )


@pytest.mark.asyncio
async def test_forge_rejects_a_non_file_input_before_spawning(tmp_path: Path) -> None:
    request = FactoryDispatch(
        job=job(),
        action=FactoryAction(kind=FactoryActionKind.SUBMIT_FORGE_JOB, attempt=1),
        role=None,
        lease=None,
    )
    forge = MinibookSwarmForge(
        materializer=Materializer(tmp_path / "missing.md"),
        mapper=Mapper(),
    )

    with pytest.raises(RuntimeError, match="materialize"):
        await forge.submit(request)


@pytest.mark.asyncio
async def test_forge_rejects_non_private_artifact_root_before_spawning(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("# Team", encoding="utf-8")
    forge = MinibookSwarmForge(
        materializer=Materializer(input_path),
        mapper=Mapper(),
        settings=MinibookForgeSettings(
            working_directory=tmp_path,
            artifact_root=tmp_path / "public-cas",
        ),
    )

    with pytest.raises(RuntimeError, match=r"\.captain-cook namespace"):
        await forge.submit(
            FactoryDispatch(
                job=job(),
                action=FactoryAction(
                    kind=FactoryActionKind.SUBMIT_FORGE_JOB,
                    attempt=1,
                ),
                role=None,
                lease=None,
            )
        )


@pytest.mark.asyncio
async def test_forge_starts_a_noninteractive_deadline_bounded_input_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("# Team", encoding="utf-8")
    received: list[object] = []
    observed_job_payloads: list[dict[str, object]] = []

    class Process:
        returncode = 0

        async def communicate(self):
            creation_job_path = Path(
                received[received.index("--creation-job-file") + 1]
            )
            result_path = Path(received[received.index("--result-file") + 1])
            observed_job_payloads.append(
                json.loads(creation_job_path.read_text(encoding="utf-8"))
            )
            assert result_path.parent == creation_job_path.parent
            assert not result_path.exists()
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
        mapper=Mapper(),
        settings=MinibookForgeSettings(working_directory=tmp_path, max_runtime_seconds=120),
    )

    result = await forge.submit(request)

    assert received[:6] == [
        "python",
        str(Path("minibook/autogen_swarm.py")),
        "--input-file",
        str(input_path),
        "--creation-job-file",
        received[5],
    ]
    assert received[6:10] == [
        "--non-interactive",
        "--max-runtime-seconds",
        "120",
        "--result-file",
    ]
    assert Path(received[5]).name == "creation-job.json"
    assert Path(received[10]).name == "creation-result.json"
    assert received[11:13] == [
        "--artifact-root",
        str((tmp_path / ".captain-cook" / "minibook-creation-cas").resolve()),
    ]
    assert observed_job_payloads == [
        Mapper().map(request).model_dump(mode="json", by_alias=True)
    ]
    assert result.status == "succeeded"


@pytest.mark.asyncio
async def test_forge_rejects_result_bound_to_a_different_creation_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("# Team", encoding="utf-8")

    class Process:
        returncode = 0

        async def communicate(self):
            payload = json.loads(
                Path("tests/fixtures/contracts/minibook_creation_result.v1.json").read_text(
                    encoding="utf-8"
                )
            )
            payload["creation_job_id"] = "22222222-2222-4222-8222-222222222222"
            Path(self.result_path).write_text(json.dumps(payload), encoding="utf-8")
            return b"", b""

    async def start(*command: str, **_kwargs: object) -> object:
        process = Process()
        process.result_path = command[command.index("--result-file") + 1]
        return process

    monkeypatch.setattr(
        "agenten.agent_factory.minibook_forge.asyncio.create_subprocess_exec",
        start,
    )
    forge = MinibookSwarmForge(
        materializer=Materializer(input_path),
        mapper=Mapper(),
        settings=MinibookForgeSettings(
            working_directory=tmp_path,
            max_runtime_seconds=120,
        ),
    )

    with pytest.raises(RuntimeError, match="does not match the submitted creation job"):
        await forge.submit(
            FactoryDispatch(
                job=job(),
                action=FactoryAction(
                    kind=FactoryActionKind.SUBMIT_FORGE_JOB,
                    attempt=1,
                ),
                role=None,
                lease=None,
            )
        )


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
