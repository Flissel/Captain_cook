from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid5

import pytest

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveInput,
    CapabilityLiveReservation,
    CapabilityLiveStage,
)
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.capability_assembly_contracts import (
    CapabilityAssemblyAdmittedInput,
    CapabilityAssemblyAdvanceRequest,
    CapabilityAssemblyCheckpoint,
    CapabilityAssemblyInputRequest,
    CapabilityAssemblyNoEffectRetryAuthorization,
    CapabilityAssemblyReconciliationOutcome,
    CapabilityAssemblyReconciliationRequest,
    CapabilityAssemblySlice,
    CapabilityAssemblySliceKind,
    CapabilityAssemblyStatus,
    CapabilityAssemblySubmitRequest,
    CapabilityAssemblyWriteConflict,
    canonical_assembly_model_bytes,
)
from gateway.capability_assembly_controller import (
    CapabilityAssemblyConflict,
    CapabilityAssemblyController,
    CapabilityAssemblyIncomplete,
)
from gateway.capability_live_pipeline import (
    CapabilityBusinessBenchmarkAuthority,
    CapabilityExecutionAuthority,
    CapabilityFactoryAuthority,
    CapabilityProjectionAuthority,
    CapabilityResumeAuthorization,
)


NS = UUID("891290cd-30c7-5a09-a610-266aff599653")
ASSEMBLY_ID = uuid5(NS, "assembly")


def _id(label: str) -> UUID:
    return uuid5(NS, label)


def _ref(label: str) -> ArtifactRef:
    digest = hashlib.sha256(label.encode()).hexdigest()
    return ArtifactRef(
        uri=f"artifact://assembly/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _input_request(
    index: int,
    *,
    pattern: str,
    path: str,
) -> CapabilityAssemblyInputRequest:
    input_id = f"input-{index}"
    return CapabilityAssemblyInputRequest(
        input_id=input_id,
        repository_path=path,
        conversation_pattern=pattern,
        correlation_id=_id(f"correlation-{index}"),
        reservation=CapabilityLiveReservation(
            reservation_id=_id(f"reservation-{index}"),
            run_id=ASSEMBLY_ID,
            input_id=input_id,
            maximum_cost_usd=Decimal("2.00"),
        ),
    )


def _submission(*, idempotency_key: str = "a" * 64) -> CapabilityAssemblySubmitRequest:
    return CapabilityAssemblySubmitRequest(
        assembly_id=ASSEMBLY_ID,
        idempotency_key=idempotency_key,
        maximum_cost_usd=Decimal("6.00"),
        inputs=(
            _input_request(
                1,
                pattern="sequential",
                path="examples/customer-a/TO_BE_BUILT.md",
            ),
            _input_request(
                2,
                pattern="selector_group_chat",
                path="examples/customer-b/TO_BE_BUILT.md",
            ),
            _input_request(
                3,
                pattern="swarm",
                path="products/customer-c/TO_BE_BUILT.md",
            ),
        ),
    )


@dataclass(frozen=True)
class _Record:
    checkpoint: CapabilityAssemblyCheckpoint
    artifact_ref: ArtifactRef


def _record_for_checkpoint(checkpoint: CapabilityAssemblyCheckpoint) -> _Record:
    digest = hashlib.sha256(canonical_assembly_model_bytes(checkpoint)).hexdigest()
    return _Record(
        checkpoint=checkpoint,
        artifact_ref=ArtifactRef(
            uri=f"artifact://assembly-checkpoint/{digest}",
            sha256=digest,
            media_type="application/json",
        ),
    )


class _Repository:
    def __init__(self) -> None:
        self._records: dict[UUID, list[_Record]] = {}

    def history(self, assembly_id: UUID) -> tuple[_Record, ...]:
        return tuple(self._records.get(assembly_id, ()))

    def find_by_idempotency_key(self, idempotency_key: str) -> _Record | None:
        for records in self._records.values():
            if records and records[-1].checkpoint.idempotency_key == idempotency_key:
                return records[-1]
        return None

    def append(
        self,
        checkpoint: CapabilityAssemblyCheckpoint,
        *,
        expected_revision: int,
    ) -> _Record:
        records = self._records.setdefault(checkpoint.assembly_id, [])
        current = 0 if not records else records[-1].checkpoint.revision
        if current != expected_revision:
            raise CapabilityAssemblyWriteConflict("stale assembly revision")
        if checkpoint.revision != expected_revision + 1:
            raise AssertionError("controller attempted a non-monotone append")
        record = _record_for_checkpoint(checkpoint)
        records.append(record)
        return record


class _Admission:
    def __init__(self) -> None:
        self.calls: list[CapabilityAssemblyInputRequest] = []

    def admit(self, request: CapabilityAssemblyInputRequest) -> CapabilityAssemblyAdmittedInput:
        self.calls.append(request)
        semantics = {
            "sequential": dict(
                integration_intent="n8n",
                conflicting_input=False,
                human_escalation_required=False,
                required_tool_marker=None,
            ),
            "selector_group_chat": dict(
                integration_intent="none",
                conflicting_input=True,
                human_escalation_required=True,
                required_tool_marker=None,
            ),
            "swarm": dict(
                integration_intent="none",
                conflicting_input=False,
                human_escalation_required=False,
                required_tool_marker="TODO_TOOL.v1",
            ),
        }[request.conversation_pattern]
        return CapabilityAssemblyAdmittedInput(
            repository_path=request.repository_path,
            input=CapabilityLiveInput(
                input_id=request.input_id,
                conversation_pattern=request.conversation_pattern,
                source_ref=_ref(f"source-{request.input_id}"),
                **semantics,
            ),
        )


class _Factory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def scaffold(self, request):
        return self._authority(request, "scaffold")

    async def implement(self, request):
        return self._authority(request, "implement")

    async def seal(self, request):
        return self._authority(request, "seal")

    async def promote(self, request):
        self.calls.append(("promote", request.input.input_id))
        return CapabilityFactoryAuthority(
            stage=request.stage,
            input_id=request.input.input_id,
            source_ref=request.input.source_ref,
            job_id=_id(f"job-{request.input.input_id}"),
            correlation_id=request.correlation_id,
            subject_version=1,
            capability_id=f"capability-{request.input.input_id}",
            capability_version=1,
            suite_ref=request.suite_ref,
            hermes_result_ref=request.hermes_result_ref,
            evidence_refs=(_ref(f"promote-{request.input.input_id}"),),
            booked_cost_usd=Decimal("1.00"),
            controlled_recovery_id=(
                _id(f"recovery-{request.input.input_id}")
                if request.controlled_recovery_required
                else None
            ),
            normal_trace_ids=tuple(
                _id(f"trace-{request.input.input_id}-{number}") for number in range(3)
            ),
            benchmark_authority_id=_id(f"benchmark-{request.input.input_id}"),
            benchmark_summary_ref=_ref(f"summary-{request.input.input_id}"),
            release_authority_id=_id(f"release-{request.input.input_id}"),
        )

    async def released(self, request):
        raise AssertionError("controller must use its persisted promotion authority")

    def _authority(self, request, label: str) -> CapabilityFactoryAuthority:
        self.calls.append((label, request.input.input_id))
        return CapabilityFactoryAuthority(
            stage=request.stage,
            input_id=request.input.input_id,
            source_ref=request.input.source_ref,
            job_id=_id(f"job-{request.input.input_id}"),
            correlation_id=request.correlation_id,
            subject_version=1,
            capability_id=f"capability-{request.input.input_id}",
            capability_version=1,
            suite_ref=request.suite_ref or _ref(f"suite-{request.input.input_id}"),
            hermes_result_ref=request.hermes_result_ref
            or _ref(f"hermes-{request.input.input_id}"),
            evidence_refs=(_ref(f"{label}-{request.input.input_id}"),),
            booked_cost_usd=Decimal("1.00"),
        )


class _Benchmark:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def evaluate(self, request):
        self.calls.append(request.input.input_id)
        return CapabilityBusinessBenchmarkAuthority(
            stage=CapabilityLiveStage.EVALUATE,
            input_id=request.input.input_id,
            source_ref=request.input.source_ref,
            job_id=_id(f"job-{request.input.input_id}"),
            correlation_id=request.correlation_id,
            subject_version=1,
            capability_id=f"capability-{request.input.input_id}",
            capability_version=1,
            suite_ref=request.suite_ref,
            hermes_result_ref=request.hermes_result_ref,
            evidence_refs=(_ref(f"evaluation-{request.input.input_id}"),),
            booked_cost_usd=Decimal("1.00"),
            benchmark_authority_id=_id(f"benchmark-{request.input.input_id}"),
            benchmark_summary_ref=_ref(f"summary-{request.input.input_id}"),
            normal_trace_ids=tuple(
                _id(f"trace-{request.input.input_id}-{number}") for number in range(3)
            ),
        )


class _Execution:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, request, release):
        self.calls.append(request.input.input_id)
        return CapabilityExecutionAuthority(
            job_id=release.job_id,
            correlation_id=release.correlation_id,
            capability_id=release.capability_id,
            capability_version=release.capability_version,
            execution_authority_id=_id(f"execution-{request.input.input_id}"),
            execution_command_id=_id(f"command-{request.input.input_id}"),
            execution_result_id=_id(f"result-{request.input.input_id}"),
            evidence_refs=(_ref(f"execution-{request.input.input_id}"),),
            booked_cost_usd=release.booked_cost_usd,
            terminal_status="succeeded",
            replayed=False,
        )


class _Projection:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def project_and_readback(self, request, release, execution):
        self.calls.append(request.input.input_id)
        return CapabilityProjectionAuthority(
            job_id=release.job_id,
            correlation_id=release.correlation_id,
            capability_id=release.capability_id,
            capability_version=release.capability_version,
            execution_command_id=execution.execution_command_id,
            execution_result_id=execution.execution_result_id,
            projection_event_id=_id(f"projection-{request.input.input_id}"),
            projection_readback_id=_id(f"readback-{request.input.input_id}"),
            evidence_refs=(_ref(f"projection-{request.input.input_id}"),),
        )


class _Resume:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.calls: list[str] = []
        self.authorization: CapabilityResumeAuthorization | None = None
        self.execution: CapabilityExecutionAuthority | None = None

    async def authorize_resume(self, request):
        self.calls.append("authorize")
        if not self.available:
            return None
        self.authorization = CapabilityResumeAuthorization(
            job_id=request.job_id,
            reservation_id=request.reservation_id,
            correlation_id=request.correlation_id,
            capability_id=request.capability_id,
            capability_version=request.capability_version,
            authorization_receipt_id=_id("resume-authorization"),
            evidence_refs=(_ref("resume-authorization"),),
        )
        return self.authorization

    async def dispatch_resume(self, authorization):
        self.calls.append("dispatch")
        self.execution = CapabilityExecutionAuthority(
            job_id=authorization.job_id,
            correlation_id=authorization.correlation_id,
            capability_id=authorization.capability_id,
            capability_version=authorization.capability_version,
            execution_authority_id=authorization.authorization_receipt_id,
            execution_command_id=_id("resumed-command"),
            execution_result_id=_id("resumed-result"),
            evidence_refs=(_ref("resumed-execution"),),
            booked_cost_usd=Decimal("1.00"),
            terminal_status="succeeded",
            replayed=False,
        )
        return self.execution

    async def readback_resume(self, authorization):
        self.calls.append("readback")
        return self.execution


class _Reconciliation:
    def __init__(self) -> None:
        self.calls: list[CapabilityAssemblyReconciliationRequest] = []
        self.outcome: CapabilityAssemblyReconciliationOutcome | None = None

    async def reconcile(self, request: CapabilityAssemblyReconciliationRequest):
        self.calls.append(request)
        return self.outcome


def _controller(
    repository: _Repository | None = None,
    *,
    admission: _Admission | None = None,
    reconciliation: _Reconciliation | None = None,
    resume: _Resume | None = None,
):
    repo = repository or _Repository()
    admission_port = admission or _Admission()
    factory = _Factory()
    benchmark = _Benchmark()
    execution = _Execution()
    projection = _Projection()
    resume_port = resume or _Resume()
    reconciliation_port = reconciliation or _Reconciliation()
    controller = CapabilityAssemblyController(
        repository=repo,
        admission=admission_port,
        factory=factory,
        benchmark=benchmark,
        resume=resume_port,
        reconciliation=reconciliation_port,
        execution=execution,
        minibook_projection=projection,
    )
    return (
        controller,
        repo,
        admission_port,
        factory,
        benchmark,
        execution,
        projection,
        resume_port,
        reconciliation_port,
    )


def _advance(
    controller: CapabilityAssemblyController,
    *,
    operation: str,
    maximum_slices: int,
):
    projection = controller.projection(ASSEMBLY_ID)
    return asyncio.run(
        controller.advance(
            CapabilityAssemblyAdvanceRequest(
                assembly_id=ASSEMBLY_ID,
                operation_id=_id(operation),
                request_sha256=projection.request_sha256,
                maximum_slices=maximum_slices,
            )
        )
    )


def test_submit_admits_exactly_three_repository_inputs_and_replays_without_readmission() -> None:
    controller, _, admission, *_ = _controller()

    first = controller.submit(_submission())
    replay = controller.submit(_submission())

    assert first == replay
    assert replay.status is CapabilityAssemblyStatus.ADMITTED
    assert replay.revision == 1
    assert [item.repository_path for item in replay.inputs] == [
        "examples/customer-a/TO_BE_BUILT.md",
        "examples/customer-b/TO_BE_BUILT.md",
        "products/customer-c/TO_BE_BUILT.md",
    ]
    assert len(admission.calls) == 3


def test_submit_rejects_changed_idempotent_replay_and_noncanonical_paths() -> None:
    controller, *_ = _controller()
    controller.submit(_submission())
    changed = _submission().model_copy(update={"maximum_cost_usd": Decimal("7.00")})

    with pytest.raises(CapabilityAssemblyConflict, match="idempotent submission"):
        controller.submit(changed)

    with pytest.raises(ValueError, match="repository-relative TO_BE_BUILT.md"):
        _input_request(1, pattern="sequential", path="C:/outside/TO_BE_BUILT.md")


def test_submit_rejects_duplicate_admitted_source_digest_before_persistence() -> None:
    class _DuplicateSourceAdmission(_Admission):
        def admit(self, request):
            admitted = super().admit(request)
            return admitted.model_copy(
                update={
                    "input": admitted.input.model_copy(
                        update={"source_ref": _ref("same-source-bytes")}
                    )
                }
            )

    admission = _DuplicateSourceAdmission()
    controller, repo, _, factory, *_ = _controller(admission=admission)

    with pytest.raises(CapabilityAssemblyConflict, match="distinct source"):
        controller.submit(_submission())

    assert repo.history(ASSEMBLY_ID) == ()
    assert len(admission.calls) == 3
    assert factory.calls == []


def test_persisted_projection_rejects_duplicate_admitted_source_identity() -> None:
    controller, repo, _, factory, *_ = _controller()
    controller.submit(_submission())
    first = repo._records[ASSEMBLY_ID][0].checkpoint
    items = list(first.items)
    duplicate_input = items[1].admitted.input.model_copy(
        update={"source_ref": items[0].admitted.input.source_ref}
    )
    duplicate_admission = items[1].admitted.model_copy(
        update={"input": duplicate_input}
    )
    items[1] = items[1].model_copy(update={"admitted": duplicate_admission})
    changed = first.model_copy(update={"items": tuple(items)})
    repo._records[ASSEMBLY_ID][0] = _record_for_checkpoint(changed)

    with pytest.raises(CapabilityAssemblyConflict, match="distinct source"):
        controller.projection(ASSEMBLY_ID)

    assert factory.calls == []


def test_persisted_projection_recomputes_the_immutable_submit_digest() -> None:
    controller, repo, _, factory, *_ = _controller()
    controller.submit(_submission())
    first = repo._records[ASSEMBLY_ID][0].checkpoint
    changed = first.model_copy(update={"request_sha256": "b" * 64})
    repo._records[ASSEMBLY_ID][0] = _record_for_checkpoint(changed)

    with pytest.raises(CapabilityAssemblyConflict, match="request digest"):
        controller.projection(ASSEMBLY_ID)

    assert factory.calls == []


@pytest.mark.parametrize(
    ("port_name", "method_name", "failure_kind"),
    (
        ("repository", "history", "not_callable"),
        ("repository", "find_by_idempotency_key", "wrong_signature"),
        ("repository", "append", "wrong_signature"),
        ("admission", "admit", "async_for_sync"),
        ("factory", "scaffold", "sync_for_async"),
        ("factory", "implement", "wrong_async_signature"),
        ("factory", "seal", "not_callable"),
        ("factory", "promote", "sync_for_async"),
        ("benchmark", "evaluate", "sync_for_async"),
        ("resume", "authorize_resume", "sync_for_async"),
        ("resume", "dispatch_resume", "wrong_async_signature"),
        ("resume", "readback_resume", "not_callable"),
        ("reconciliation", "reconcile", "wrong_async_signature"),
        ("execution", "execute", "wrong_async_signature"),
        ("projection", "project_and_readback", "sync_for_async"),
    ),
)
def test_controller_preflights_every_port_before_any_write_or_effect(
    port_name: str,
    method_name: str,
    failure_kind: str,
) -> None:
    repository = _Repository()
    admission = _Admission()
    factory = _Factory()
    benchmark = _Benchmark()
    resume = _Resume()
    execution = _Execution()
    projection = _Projection()
    reconciliation = _Reconciliation()
    ports = {
        "repository": repository,
        "admission": admission,
        "factory": factory,
        "benchmark": benchmark,
        "resume": resume,
        "execution": execution,
        "projection": projection,
        "reconciliation": reconciliation,
    }

    async def async_no_args():
        return None

    async def async_one_arg(_value):
        return None

    if failure_kind == "not_callable":
        replacement = None
    elif failure_kind == "wrong_signature":
        replacement = lambda: None
    elif failure_kind == "async_for_sync":
        replacement = async_one_arg
    elif failure_kind == "wrong_async_signature":
        replacement = async_no_args
    else:
        replacement = lambda *_args, **_kwargs: None
    setattr(ports[port_name], method_name, replacement)

    with pytest.raises(ValueError, match="port binding"):
        CapabilityAssemblyController(
            repository=repository,
            admission=admission,
            factory=factory,
            benchmark=benchmark,
            resume=resume,
            reconciliation=reconciliation,
            execution=execution,
            minibook_projection=projection,
        )

    assert repository._records == {}
    assert admission.calls == []
    assert factory.calls == []
    assert benchmark.calls == []
    assert resume.calls == []
    assert execution.calls == []
    assert projection.calls == []
    assert reconciliation.calls == []


def test_advance_rechecks_mutated_port_before_claim_or_effect() -> None:
    controller, repo, _, factory, *_ = _controller()
    controller.submit(_submission())
    before = len(repo.history(ASSEMBLY_ID))

    async def invalid_signature():
        return None

    factory.scaffold = invalid_signature
    request = CapabilityAssemblyAdvanceRequest(
        assembly_id=ASSEMBLY_ID,
        operation_id=_id("invalid-rebound-port"),
        request_sha256=controller.projection(ASSEMBLY_ID).request_sha256,
        maximum_slices=1,
    )

    with pytest.raises(ValueError, match="port binding"):
        asyncio.run(controller.advance(request))

    assert len(repo.history(ASSEMBLY_ID)) == before
    assert factory.calls == []


def test_repository_receipt_digest_must_bind_exact_checkpoint_bytes() -> None:
    class _CorruptReceiptRepository(_Repository):
        def append(self, checkpoint, *, expected_revision):
            record = super().append(
                checkpoint,
                expected_revision=expected_revision,
            )
            return _Record(
                checkpoint=record.checkpoint,
                artifact_ref=_ref("wrong-checkpoint-bytes"),
            )

    repository = _CorruptReceiptRepository()
    controller, _, admission, factory, *_ = _controller(repository)

    with pytest.raises(CapabilityAssemblyConflict, match="artifact digest"):
        controller.submit(_submission())

    assert len(admission.calls) == 3
    assert factory.calls == []


def test_advance_is_bounded_and_operation_replay_has_no_additional_effect() -> None:
    controller, _, _, factory, benchmark, execution, projection, resume, _ = _controller()
    controller.submit(_submission())

    first = _advance(controller, operation="advance-1", maximum_slices=2)
    replay = _advance(controller, operation="advance-1", maximum_slices=2)

    assert first == replay
    assert first.executed_slices == 2
    assert first.start_completed_slices == 0
    assert first.end_completed_slices == 2
    assert factory.calls == [("scaffold", "input-1"), ("implement", "input-1")]
    assert benchmark.calls == []
    assert execution.calls == []
    assert projection.calls == []
    assert resume.calls == []


def test_new_controller_resumes_claimed_operation_after_interruption() -> None:
    controller, repo, *_ = _controller()
    controller.submit(_submission())
    request = CapabilityAssemblyAdvanceRequest(
        assembly_id=ASSEMBLY_ID,
        operation_id=_id("interrupted"),
        request_sha256=controller.projection(ASSEMBLY_ID).request_sha256,
        maximum_slices=3,
    )
    controller.claim_advance(request)

    restarted, _, _, restarted_factory, *_ = _controller(repo)
    result = asyncio.run(restarted.advance(request))

    assert result.executed_slices == 3
    assert restarted_factory.calls == [
        ("scaffold", "input-1"),
        ("implement", "input-1"),
        ("seal", "input-1"),
    ]


def test_complete_assembly_returns_validated_final_evidence_and_uses_d1_chain() -> None:
    controller, _, _, factory, benchmark, execution, projection, resume, _ = _controller()
    controller.submit(_submission())

    for batch in range(4):
        _advance(controller, operation=f"complete-{batch}", maximum_slices=8)

    current = controller.projection(ASSEMBLY_ID)
    evidence = controller.final_evidence(ASSEMBLY_ID)

    assert current.status is CapabilityAssemblyStatus.READY_TO_USE
    assert evidence.status == "ready_to_use"
    assert len(evidence.capabilities) == 3
    assert all(item.checkpoint_refs for item in evidence.capabilities)
    assert len(factory.calls) == 12
    assert benchmark.calls == ["input-1", "input-2", "input-3"]
    assert execution.calls == ["input-1", "input-2", "input-3"]
    assert projection.calls == ["input-1", "input-2", "input-3"]
    assert resume.calls == ["authorize", "dispatch", "readback"]


def test_missing_d1_authority_fails_closed_without_final_evidence() -> None:
    resume = _Resume(available=False)
    controller, *_ = _controller(resume=resume)
    controller.submit(_submission())

    for batch in range(2):
        _advance(controller, operation=f"until-resume-{batch}", maximum_slices=8)
    _advance(controller, operation="until-resume-2", maximum_slices=5)
    with pytest.raises(CapabilityAssemblyIncomplete, match="resume authorization"):
        _advance(controller, operation="resume-blocked", maximum_slices=1)
    with pytest.raises(CapabilityAssemblyIncomplete, match="not complete"):
        controller.final_evidence(ASSEMBLY_ID)


def test_changed_authority_receipt_is_rejected_before_checkpoint_append() -> None:
    controller, repo, _, factory, *_ = _controller()
    controller.submit(_submission())
    original = factory.scaffold

    async def changed(request):
        value = await original(request)
        return value.model_copy(update={"correlation_id": _id("foreign")})

    factory.scaffold = changed
    before = len(repo.history(ASSEMBLY_ID))

    with pytest.raises(CapabilityAssemblyConflict, match="Factory authority"):
        _advance(controller, operation="bad-authority", maximum_slices=1)
    assert len(repo.history(ASSEMBLY_ID)) == before + 2  # claim plus started marker


def test_repository_history_revision_gap_fails_closed_before_any_effect() -> None:
    controller, repo, _, factory, *_ = _controller()
    controller.submit(_submission())
    first = repo._records[ASSEMBLY_ID][0]
    changed = first.checkpoint.model_copy(update={"revision": 3})
    repo._records[ASSEMBLY_ID].append(_record_for_checkpoint(changed))

    with pytest.raises(CapabilityAssemblyConflict, match="monotone"):
        controller.projection(ASSEMBLY_ID)
    assert factory.calls == []


def test_repository_history_rejects_slice_count_without_matching_item_progress() -> None:
    controller, repo, _, factory, *_ = _controller()
    controller.submit(_submission())
    first = repo._records[ASSEMBLY_ID][0]
    changed = first.checkpoint.model_copy(
        update={
            "revision": 2,
            "status": CapabilityAssemblyStatus.ADVANCING,
            "completed_slices": 1,
            "last_slice": CapabilityAssemblySlice(
                ordinal=0,
                kind=CapabilityAssemblySliceKind.SCAFFOLD_AND_TEST_HARNESS,
                input_id="input-1",
            ),
        }
    )
    repo._records[ASSEMBLY_ID].append(_record_for_checkpoint(changed))

    with pytest.raises(CapabilityAssemblyConflict, match="monotone"):
        controller.projection(ASSEMBLY_ID)
    assert factory.calls == []


def test_factory_cost_cannot_rewind_between_persisted_stages() -> None:
    controller, repo, _, factory, *_ = _controller()
    controller.submit(_submission())
    _advance(controller, operation="cost-scaffold", maximum_slices=1)
    original = factory.implement

    async def rewound(request):
        value = await original(request)
        return value.model_copy(update={"booked_cost_usd": Decimal("0.50")})

    factory.implement = rewound
    before = len(repo.history(ASSEMBLY_ID))

    with pytest.raises(CapabilityAssemblyConflict, match="rewound"):
        _advance(controller, operation="cost-rewind", maximum_slices=1)
    assert len(repo.history(ASSEMBLY_ID)) == before + 2


def test_repository_history_rejects_active_operation_identity_swap() -> None:
    controller, repo, _, factory, *_ = _controller()
    controller.submit(_submission())
    claimed = controller.claim_advance(
        CapabilityAssemblyAdvanceRequest(
            assembly_id=ASSEMBLY_ID,
            operation_id=_id("stable-operation"),
            request_sha256=controller.projection(ASSEMBLY_ID).request_sha256,
            maximum_slices=2,
        )
    )
    assert claimed.active_advance is not None
    swapped = claimed.active_advance.model_copy(
        update={"operation_id": _id("foreign-operation")}
    )
    changed = claimed.model_copy(
        update={"revision": 3, "active_advance": swapped}
    )
    repo._records[ASSEMBLY_ID].append(_record_for_checkpoint(changed))

    with pytest.raises(CapabilityAssemblyConflict, match="monotone"):
        controller.projection(ASSEMBLY_ID)
    assert factory.calls == []


def test_started_slice_without_terminal_never_replays_the_effect_after_restart() -> None:
    controller, repo, _, factory, *_ = _controller()
    controller.submit(_submission())
    request = CapabilityAssemblyAdvanceRequest(
        assembly_id=ASSEMBLY_ID,
        operation_id=_id("crashed-slice"),
        request_sha256=controller.projection(ASSEMBLY_ID).request_sha256,
        maximum_slices=1,
    )

    async def interrupted(request):
        factory.calls.append(("interrupted", request.input.input_id))
        raise RuntimeError("simulated process interruption")

    factory.scaffold = interrupted
    with pytest.raises(RuntimeError, match="simulated"):
        asyncio.run(controller.advance(request))
    assert factory.calls == [("interrupted", "input-1")]

    restarted, _, _, restarted_factory, *_ = _controller(repo)
    with pytest.raises(CapabilityAssemblyIncomplete, match="started slice"):
        asyncio.run(restarted.advance(request))
    assert restarted_factory.calls == []


def _scaffold_readback(checkpoint: CapabilityAssemblyCheckpoint) -> CapabilityFactoryAuthority:
    item = checkpoint.items[0]
    return CapabilityFactoryAuthority(
        stage=CapabilityLiveStage.SCAFFOLD_AND_TEST_HARNESS,
        input_id=item.request.input_id,
        source_ref=item.admitted.input.source_ref,
        job_id=_id(f"job-{item.request.input_id}"),
        correlation_id=item.request.correlation_id,
        subject_version=1,
        capability_id=f"capability-{item.request.input_id}",
        capability_version=1,
        suite_ref=_ref(f"suite-{item.request.input_id}"),
        hermes_result_ref=_ref(f"hermes-{item.request.input_id}"),
        evidence_refs=(_ref("authoritative-scaffold-readback"),),
        booked_cost_usd=Decimal("1.00"),
    )


def _crash_first_slice(
    controller: CapabilityAssemblyController,
    factory: _Factory,
    request: CapabilityAssemblyAdvanceRequest,
) -> None:
    async def interrupted(stage_request):
        factory.calls.append(("interrupted", stage_request.input.input_id))
        raise RuntimeError("simulated process interruption")

    factory.scaffold = interrupted
    with pytest.raises(RuntimeError, match="simulated"):
        asyncio.run(controller.advance(request))


def test_started_slice_completes_only_from_matching_authoritative_readback() -> None:
    reconciliation = _Reconciliation()
    controller, repo, _, factory, *_ = _controller(reconciliation=reconciliation)
    controller.submit(_submission())
    request = CapabilityAssemblyAdvanceRequest(
        assembly_id=ASSEMBLY_ID,
        operation_id=_id("terminal-reconciliation"),
        request_sha256=controller.projection(ASSEMBLY_ID).request_sha256,
        maximum_slices=1,
    )
    _crash_first_slice(controller, factory, request)
    started = controller.projection(ASSEMBLY_ID)
    started_record = repo.history(ASSEMBLY_ID)[-1]
    assert started.started_slice is not None
    reconciliation.outcome = CapabilityAssemblyReconciliationOutcome(
        assembly_id=ASSEMBLY_ID,
        operation_id=request.operation_id,
        request_sha256=request.request_sha256,
        started_slice=started.started_slice,
        started_checkpoint_ref=started_record.artifact_ref,
        disposition="terminal_readback",
        factory_authority=_scaffold_readback(started),
    )

    restarted, _, _, restarted_factory, *_ = _controller(
        repo,
        reconciliation=reconciliation,
    )
    receipt = asyncio.run(restarted.advance(request))

    assert receipt.executed_slices == 1
    assert restarted_factory.calls == []
    assert len(reconciliation.calls) == 1
    assert restarted.projection(ASSEMBLY_ID).started_slice is None
    assert restarted.projection(ASSEMBLY_ID).items[0].completed_stages == (
        CapabilityLiveStage.SCAFFOLD_AND_TEST_HARNESS,
    )


def test_no_effect_retry_requires_explicit_captain_authorization_before_reexecution() -> None:
    reconciliation = _Reconciliation()
    controller, repo, _, factory, *_ = _controller(reconciliation=reconciliation)
    controller.submit(_submission())
    request = CapabilityAssemblyAdvanceRequest(
        assembly_id=ASSEMBLY_ID,
        operation_id=_id("no-effect-retry"),
        request_sha256=controller.projection(ASSEMBLY_ID).request_sha256,
        maximum_slices=1,
    )
    _crash_first_slice(controller, factory, request)
    started = controller.projection(ASSEMBLY_ID)
    started_record = repo.history(ASSEMBLY_ID)[-1]
    assert started.started_slice is not None
    authorization = CapabilityAssemblyNoEffectRetryAuthorization(
        assembly_id=ASSEMBLY_ID,
        operation_id=request.operation_id,
        request_sha256=request.request_sha256,
        started_slice=started.started_slice,
        started_checkpoint_ref=started_record.artifact_ref,
        authorization_receipt_id=_id("captain-no-effect-authorization"),
        evidence_refs=(_ref("captain-no-effect-authorization"),),
        no_effect_observed=True,
    )
    reconciliation.outcome = CapabilityAssemblyReconciliationOutcome(
        assembly_id=ASSEMBLY_ID,
        operation_id=request.operation_id,
        request_sha256=request.request_sha256,
        started_slice=started.started_slice,
        started_checkpoint_ref=started_record.artifact_ref,
        disposition="retry_authorized_no_effect",
        retry_authorization=authorization,
    )

    restarted, _, _, restarted_factory, *_ = _controller(
        repo,
        reconciliation=reconciliation,
    )
    receipt = asyncio.run(restarted.advance(request))

    assert receipt.executed_slices == 1
    assert restarted_factory.calls == [("scaffold", "input-1")]
    assert len(reconciliation.calls) == 1


def test_mismatched_reconciliation_is_rejected_without_reexecuting_started_effect() -> None:
    reconciliation = _Reconciliation()
    controller, repo, _, factory, *_ = _controller(reconciliation=reconciliation)
    controller.submit(_submission())
    request = CapabilityAssemblyAdvanceRequest(
        assembly_id=ASSEMBLY_ID,
        operation_id=_id("mismatched-reconciliation"),
        request_sha256=controller.projection(ASSEMBLY_ID).request_sha256,
        maximum_slices=1,
    )
    _crash_first_slice(controller, factory, request)
    started = controller.projection(ASSEMBLY_ID)
    assert started.started_slice is not None
    reconciliation.outcome = CapabilityAssemblyReconciliationOutcome(
        assembly_id=ASSEMBLY_ID,
        operation_id=request.operation_id,
        request_sha256=request.request_sha256,
        started_slice=started.started_slice,
        started_checkpoint_ref=_ref("foreign-started-checkpoint"),
        disposition="terminal_readback",
        factory_authority=_scaffold_readback(started),
    )

    restarted, _, _, restarted_factory, *_ = _controller(
        repo,
        reconciliation=reconciliation,
    )
    before = len(repo.history(ASSEMBLY_ID))
    with pytest.raises(CapabilityAssemblyConflict, match="reconciliation"):
        asyncio.run(restarted.advance(request))

    assert len(repo.history(ASSEMBLY_ID)) == before
    assert restarted_factory.calls == []


def test_checkpoint_round_trips_as_strict_json_after_authority_slices() -> None:
    controller, *_ = _controller()
    controller.submit(_submission())
    _advance(controller, operation="json-roundtrip", maximum_slices=8)
    checkpoint = controller.projection(ASSEMBLY_ID)

    encoded = json.dumps(
        checkpoint.model_dump(mode="json", by_alias=True),
        separators=(",", ":"),
        sort_keys=True,
    )

    assert CapabilityAssemblyCheckpoint.model_validate_json(encoded) == checkpoint
