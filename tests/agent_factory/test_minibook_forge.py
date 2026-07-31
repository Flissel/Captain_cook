from __future__ import annotations

from pathlib import Path
import json
from datetime import datetime, timezone

import httpx
import pytest

from agenten.agent_factory.forge_contracts import (
    CreationJobV1,
    CreationJobV2,
    ForgeBuildSkillUsageReceiptV1,
)
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
    CodexBuildEvidenceV1,
    FactorySkillStep,
)
from tests.agent_factory.test_codex_build_provenance_contracts import (
    evidence_payload as codex_build_evidence_payload,
    receipt_ref as codex_build_receipt_ref,
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

    def build_skill_receipt(
        self,
        _request: FactoryDispatch,
        creation_job: CreationJobV1,
    ) -> ForgeBuildSkillUsageReceiptV1:
        return ForgeBuildSkillUsageReceiptV1.model_validate(
            {
                "producer": "hermes",
                "outcome": "fulfilled",
                "creation_job_id": creation_job.creation_job_id,
                "factory_job_id": creation_job.factory_job_id,
                "correlation_id": creation_job.correlation_id,
                "subject_version": creation_job.subject_version,
                "attempt": creation_job.attempt,
                "idempotency_key": creation_job.idempotency_key,
                "released_skill": creation_job.released_skill,
                "public_assertion_ids": creation_job.public_assertion_ids,
                "evidence_refs": (
                    {
                        "uri": "artifact://forge/hermes-brief/" + "9" * 64,
                        "sha256": "9" * 64,
                        "media_type": "application/json",
                    },
                ),
            }
        )


def _workflow_evidence(*, attempt: int = 1, inventory_attempt: int | None = None):
    factory_job = job_v3(mode="demo")
    inventory_attempt = attempt if inventory_attempt is None else inventory_attempt

    def bind(
        payload: dict[str, object],
        *,
        bound_attempt: int,
    ) -> dict[str, object]:
        invocation = payload["invocation"]
        assert isinstance(invocation, dict)
        lease = invocation["lease"]
        assert isinstance(lease, dict)
        invocation.update(
            {
                "job_id": str(factory_job.job_id),
                "correlation_id": str(factory_job.correlation_id),
                "subject_version": factory_job.subject_version,
                "attempt": bound_attempt,
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
                "attempt": bound_attempt,
            }
        )
        payload.update(
            {
                "job_id": str(factory_job.job_id),
                "correlation_id": str(factory_job.correlation_id),
                "subject_version": factory_job.subject_version,
                "attempt": bound_attempt,
                "acceptance_assertion_ids": list(factory_job.acceptance_assertion_ids),
            }
        )
        return payload

    inventory = CodebaseInventoryV1.model_validate(
        bind(inventory_payload(), bound_attempt=inventory_attempt)
    )
    brief_data = bind(brief_payload(), bound_attempt=attempt)
    brief_data["context_refs"] = [inventory.artifact_ref.model_dump(mode="json")]
    invocation = brief_data["invocation"]
    assert isinstance(invocation, dict)
    assignment = brief_data["build_assignment"]
    assert isinstance(assignment, dict)
    assignment.update(
        {
            "correlation_id": str(factory_job.correlation_id),
            "subject_version": factory_job.subject_version,
            "attempt": attempt,
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
    build_data = codex_build_evidence_payload()
    build_invocation = build_data["invocation"]
    assert isinstance(build_invocation, dict)
    build_lease = build_invocation["lease"]
    assert isinstance(build_lease, dict)
    build_invocation.update(
        {
            "job_id": str(factory_job.job_id),
            "correlation_id": str(factory_job.correlation_id),
            "subject_version": factory_job.subject_version,
            "attempt": attempt,
            "input_ref": brief.artifact_ref.model_dump(mode="json"),
            "input_sha256": brief.artifact_ref.sha256,
            "acceptance_assertion_ids": list(factory_job.acceptance_assertion_ids),
        }
    )
    build_lease.update(
        {
            "job_id": str(factory_job.job_id),
            "correlation_id": str(factory_job.correlation_id),
            "subject_version": factory_job.subject_version,
            "attempt": attempt,
            "workspace_ref": assignment["workspace_ref"],
        }
    )
    receipt = build_data["build_receipt"]
    assert isinstance(receipt, dict)
    receipt.update(
        {
            "factory_job_id": str(factory_job.job_id),
            "creation_job_id": assignment["creation_job_id"],
            "correlation_id": str(factory_job.correlation_id),
            "subject_version": factory_job.subject_version,
            "attempt": attempt,
            "assignment_id": assignment["assignment_id"],
            "idempotency_key": assignment["idempotency_key"],
            "seal_idempotency_key": build_invocation["idempotency_key"],
            "build_brief_ref": brief.artifact_ref.model_dump(mode="json"),
            "workspace_ref": assignment["workspace_ref"],
            "acceptance_assertion_ids": list(factory_job.acceptance_assertion_ids),
        }
    )
    sealed_ref = codex_build_receipt_ref(receipt)
    build_data.update(
        {
            "invocation": build_invocation,
            "job_id": str(factory_job.job_id),
            "correlation_id": str(factory_job.correlation_id),
            "subject_version": factory_job.subject_version,
            "attempt": attempt,
            "acceptance_assertion_ids": list(factory_job.acceptance_assertion_ids),
            "build_receipt_ref": sealed_ref,
            "evidence_refs": [sealed_ref],
            "build_receipt": receipt,
        }
    )
    build = CodexBuildEvidenceV1.model_validate(build_data)
    return factory_job, inventory, brief, build


class ForgeEvidence:
    def __init__(self, inventory, brief, build) -> None:
        self.artifacts = (inventory, brief, build)

    def workflow_artifacts(self, job_id):
        assert job_id == self.artifacts[0].job_id
        return self.artifacts

    def released_for(self, job, step):
        assert job.job_id == self.artifacts[1].job_id
        assert step is FactorySkillStep.BRIEF_CODEX
        return self.artifacts[1].invocation.released_skill


def test_creation_job_mapper_uses_exact_captain_brief_and_is_deterministic() -> None:
    factory_job, inventory, brief, build = _workflow_evidence()
    request = FactoryDispatch(
        job=factory_job,
        action=FactoryAction(kind=FactoryActionKind.SUBMIT_FORGE_JOB, attempt=1),
        role=None,
        lease=None,
    )
    mapper = CaptainCreationJobMapper(evidence=ForgeEvidence(inventory, brief, build))

    first = mapper.map(request)
    second = mapper.map(request)
    receipt = mapper.build_skill_receipt(request, first)

    assignment = brief.build_assignment
    assert first == second
    assert isinstance(first, CreationJobV2)
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
    assert first.codex_build_receipt == build.build_receipt
    assert first.codex_build_receipt_ref.model_dump(mode="json") == (
        build.build_receipt_ref.model_dump(mode="json")
    )
    assert first.source_archive_ref == build.build_receipt.source_archive_ref
    assert receipt.producer == "hermes"
    assert receipt.outcome == "fulfilled"
    assert receipt.creation_job_id == first.creation_job_id
    assert receipt.factory_job_id == first.factory_job_id
    assert receipt.released_skill == first.released_skill
    assert receipt.public_assertion_ids == first.public_assertion_ids
    assert any(ref.sha256 == inventory.artifact_ref.sha256 for ref in receipt.evidence_refs)
    assert any(ref.sha256 == brief.artifact_ref.sha256 for ref in receipt.evidence_refs)
    assert any(ref.sha256 == build.artifact_ref.sha256 for ref in receipt.evidence_refs)
    assert any(
        ref.sha256 == build.build_receipt_ref.sha256 for ref in receipt.evidence_refs
    )


def test_creation_job_mapper_retry_uses_brief_bound_baseline_inventory() -> None:
    factory_job, inventory, brief, build = _workflow_evidence(
        attempt=2,
        inventory_attempt=1,
    )
    mapper = CaptainCreationJobMapper(
        evidence=ForgeEvidence(inventory, brief, build)
    )

    creation_job = mapper.map(
        FactoryDispatch(
            job=factory_job,
            action=FactoryAction(
                kind=FactoryActionKind.SUBMIT_FORGE_JOB,
                attempt=2,
            ),
            role=None,
            lease=None,
        )
    )

    assert creation_job.attempt == 2
    assert inventory.attempt == 1
    assert inventory.artifact_ref in brief.context_refs


def test_creation_job_mapper_rejects_missing_blueprint_inventory_binding() -> None:
    factory_job, inventory, brief, build = _workflow_evidence()
    changed = brief.model_copy(update={"context_refs": ()})
    mapper = CaptainCreationJobMapper(evidence=ForgeEvidence(inventory, changed, build))

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
    factory_job, inventory, brief, build = _workflow_evidence()
    changed_invocation = brief.invocation.model_copy(
        update={"input_ref": inventory.artifact_ref}
    )
    changed = brief.model_copy(update={"invocation": changed_invocation})
    mapper = CaptainCreationJobMapper(evidence=ForgeEvidence(inventory, changed, build))

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
        skill_receipts=Mapper(),
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
        skill_receipts=Mapper(),
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
    observed_skill_receipts: list[ForgeBuildSkillUsageReceiptV1] = []

    class Process:
        returncode = 0

        async def communicate(self):
            creation_job_path = Path(
                received[received.index("--creation-job-file") + 1]
            )
            result_path = Path(received[received.index("--result-file") + 1])
            receipt_path = Path(
                received[received.index("--skill-usage-receipt-file") + 1]
            )
            observed_job_payloads.append(
                json.loads(creation_job_path.read_text(encoding="utf-8"))
            )
            observed_skill_receipts.append(
                ForgeBuildSkillUsageReceiptV1.model_validate_json(
                    receipt_path.read_text(encoding="utf-8")
                )
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
        skill_receipts=Mapper(),
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
    assert received[6:12] == [
        "--skill-usage-receipt-file",
        received[7],
        "--non-interactive",
        "--max-runtime-seconds",
        "120",
        "--result-file",
    ]
    assert Path(received[5]).name == "creation-job.json"
    assert Path(received[7]).name == "forge-skill-usage-receipt.json"
    assert Path(received[12]).name == "creation-result.json"
    assert received[13:15] == [
        "--artifact-root",
        str((tmp_path / ".captain-cook" / "minibook-creation-cas").resolve()),
    ]
    assert observed_job_payloads == [
        Mapper().map(request).model_dump(mode="json", by_alias=True)
    ]
    assert observed_skill_receipts == [
        Mapper().build_skill_receipt(request, Mapper().map(request))
    ]
    assert result.status == "succeeded"


@pytest.mark.asyncio
async def test_forge_v2_imports_exact_captain_source_without_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "captain-source.zip"
    source_path.write_bytes(b"captain-sealed-source")
    received: list[str] = []
    factory_job, inventory, brief, build = _workflow_evidence()
    mapper = CaptainCreationJobMapper(
        evidence=ForgeEvidence(inventory, brief, build)
    )
    request = FactoryDispatch(
        job=factory_job,
        action=FactoryAction(kind=FactoryActionKind.SUBMIT_FORGE_JOB, attempt=1),
        role=None,
        lease=None,
    )
    creation_job = mapper.map(request)

    class Process:
        returncode = 0

        async def communicate(self):
            result_path = Path(received[received.index("--result-file") + 1])
            result_path.write_text(
                json.dumps(
                    {
                        "schema": "minibook.creation-result.v1",
                        "creation_job_id": str(creation_job.creation_job_id),
                        "correlation_id": str(creation_job.correlation_id),
                        "subject_version": creation_job.subject_version,
                        "attempt": creation_job.attempt,
                        "status": "succeeded",
                        "package_manifest_ref": {
                            "uri": "artifact://sha256/package",
                            "sha256": "f" * 64,
                            "media_type": "application/json",
                        },
                        "artifact_refs": [],
                        "evidence_refs": [],
                        "tool_gaps": [],
                        "skill_usage_receipt_ref": {
                            "uri": "artifact://sha256/skill-receipt",
                            "sha256": "e" * 64,
                            "media_type": "application/json",
                        },
                        "private_skill_candidate_ref": None,
                        "failure": None,
                    }
                ),
                encoding="utf-8",
            )
            return b"", b""

    async def start(*command: str, **_kwargs: object) -> object:
        received.extend(command)
        return Process()

    monkeypatch.setattr(
        "agenten.agent_factory.minibook_forge.asyncio.create_subprocess_exec",
        start,
    )
    forge = MinibookSwarmForge(
        materializer=Materializer(source_path),
        mapper=mapper,
        skill_receipts=mapper,
        settings=MinibookForgeSettings(
            working_directory=tmp_path,
            max_runtime_seconds=120,
        ),
    )

    result = await forge.submit(request)

    assert "--source-archive-file" in received
    assert received[received.index("--source-archive-file") + 1] == str(source_path)
    assert "--input-file" not in received
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
        skill_receipts=Mapper(),
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
