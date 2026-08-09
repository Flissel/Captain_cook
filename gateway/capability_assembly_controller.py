"""Persistent in-process controller for one three-input capability assembly.

The controller is deliberately server-side: clients submit stable identities
and request bounded slices, while all lifecycle effects remain behind injected
Captain/Gateway ports.  Each advance operation is durably claimed before its
first effect, so a transport retry resumes only that operation's remaining
slice allowance.
"""

from __future__ import annotations

import hashlib
import inspect
from decimal import Decimal
from typing import TypeVar, cast
from uuid import UUID

from pydantic import BaseModel

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveCapabilityEvidence,
    CapabilityLiveDemoEvidence,
    CapabilityLiveRestartEvidence,
    CapabilityLiveStage,
    validate_live_demo_evidence,
)
from gateway.capability_assembly_contracts import (
    ASSEMBLY_INPUT_STAGES,
    ASSEMBLY_TOTAL_SLICES,
    CapabilityAssemblyActiveAdvance,
    CapabilityAssemblyAdvanceReceipt,
    CapabilityAssemblyAdvanceRequest,
    CapabilityAssemblyBusinessBenchmarkPort,
    CapabilityAssemblyCheckpoint,
    CapabilityAssemblyExecutionPort,
    CapabilityAssemblyFactoryPort,
    CapabilityAssemblyAdmittedInput,
    CapabilityAssemblyCanonicalInput,
    CapabilityAssemblyInputAdmissionPort,
    CapabilityAssemblyItemState,
    CapabilityAssemblyNoEffectRetryAuthorization,
    CapabilityAssemblyReconciliationOutcome,
    CapabilityAssemblyReconciliationPort,
    CapabilityAssemblyReconciliationRequest,
    CapabilityAssemblyRepositoryPort,
    CapabilityAssemblyProjectionPort,
    CapabilityAssemblySlice,
    CapabilityAssemblySliceKind,
    CapabilityAssemblyStatus,
    CapabilityAssemblyStageRequest,
    CapabilityAssemblySubmitRequest,
    CapabilityAssemblyWriteConflict,
    assembly_request_sha256,
    canonical_assembly_model_bytes,
)
from gateway.capability_live_current_adapters import CapabilityResumeExecutionPort
from gateway.capability_live_pipeline import (
    CapabilityBusinessBenchmarkAuthority,
    CapabilityExecutionAuthority,
    CapabilityFactoryAuthority,
    CapabilityProjectionAuthority,
    CapabilityResumeAuthorization,
    CapabilityResumeExecutionRequest,
)


_ModelT = TypeVar("_ModelT", bound=BaseModel)


class CapabilityAssemblyConflict(RuntimeError):
    """Persisted state or an authority receipt conflicts with the assembly."""


class CapabilityAssemblyIncomplete(RuntimeError):
    """A required authority is unavailable or the assembly is not terminal."""


class CapabilityAssemblyNotFound(LookupError):
    """No admitted assembly exists for the requested identity."""


class CapabilityAssemblyController:
    """Admit, advance, project, and seal one durable three-input assembly."""

    def __init__(
        self,
        *,
        repository: CapabilityAssemblyRepositoryPort,
        admission: CapabilityAssemblyInputAdmissionPort,
        factory: CapabilityAssemblyFactoryPort,
        benchmark: CapabilityAssemblyBusinessBenchmarkPort,
        resume: CapabilityResumeExecutionPort,
        reconciliation: CapabilityAssemblyReconciliationPort,
        execution: CapabilityAssemblyExecutionPort,
        minibook_projection: CapabilityAssemblyProjectionPort,
    ) -> None:
        if any(
            port is None
            for port in (
                repository,
                admission,
                factory,
                benchmark,
                resume,
                reconciliation,
                execution,
                minibook_projection,
            )
        ):
            raise ValueError("all capability assembly authority ports are required")
        self._repository = repository
        self._admission = admission
        self._factory = factory
        self._benchmark = benchmark
        self._resume = resume
        self._reconciliation = reconciliation
        self._execution = execution
        self._projection = minibook_projection
        self._validate_ports()

    def submit(self, request: CapabilityAssemblySubmitRequest) -> CapabilityAssemblyCheckpoint:
        self._validate_ports()
        digest = assembly_request_sha256(request)
        existing = self._history_optional(request.assembly_id)
        by_key = self._repository.find_by_idempotency_key(request.idempotency_key)
        if existing:
            latest = existing[-1].checkpoint
            if latest.request_sha256 != digest or latest.request != request:
                raise CapabilityAssemblyConflict(
                    "idempotent submission changed the admitted assembly"
                )
            return latest
        if by_key is not None:
            checkpoint = by_key.checkpoint
            if checkpoint.assembly_id != request.assembly_id or checkpoint.request != request:
                raise CapabilityAssemblyConflict(
                    "idempotent submission is already bound to another assembly"
                )
            return checkpoint

        admitted = tuple(self._admission.admit(item) for item in request.inputs)
        for expected, observed in zip(request.inputs, admitted, strict=True):
            if (
                not isinstance(observed, CapabilityAssemblyAdmittedInput)
                or not isinstance(observed.input, CapabilityAssemblyCanonicalInput)
            ):
                raise CapabilityAssemblyConflict(
                    "input admission did not return a canonical assembly input"
                )
            if (
                observed.repository_path != expected.repository_path
                or observed.input.input_id != expected.input_id
            ):
                raise CapabilityAssemblyConflict(
                    "input admission changed repository authority"
                )
        self._require_distinct_sources(admitted)
        if tuple(item.input.conversation_pattern for item in admitted) != (
            "sequential",
            "selector_group_chat",
            "swarm",
        ):
            raise CapabilityAssemblyConflict(
                "input compiler did not classify the three required conversation patterns"
            )
        checkpoint = CapabilityAssemblyCheckpoint(
            assembly_id=request.assembly_id,
            request=request,
            request_sha256=digest,
            revision=1,
            status=CapabilityAssemblyStatus.ADMITTED,
            completed_slices=0,
            items=tuple(
                CapabilityAssemblyItemState(request=source, admitted=admission)
                for source, admission in zip(request.inputs, admitted, strict=True)
            ),
        )
        self._append(checkpoint, expected_revision=0)
        return checkpoint

    def projection(self, assembly_id: UUID) -> CapabilityAssemblyCheckpoint:
        return self._history(assembly_id)[-1].checkpoint

    def claim_advance(
        self,
        request: CapabilityAssemblyAdvanceRequest,
    ) -> CapabilityAssemblyCheckpoint:
        self._validate_ports()
        history = self._history(request.assembly_id)
        current = history[-1].checkpoint
        self._require_request_binding(current, request)
        replay = self._receipt(current, request.operation_id)
        if replay is not None:
            self._require_receipt_request(replay, request)
            return current
        if current.active_advance is not None:
            self._require_active_request(current.active_advance, request)
            return current
        target = min(
            current.completed_slices + request.maximum_slices,
            ASSEMBLY_TOTAL_SLICES,
        )
        claimed = _validated_copy(
            current,
            revision=current.revision + 1,
            status=(
                CapabilityAssemblyStatus.READY_TO_USE
                if current.completed_slices == ASSEMBLY_TOTAL_SLICES
                else CapabilityAssemblyStatus.ADVANCING
            ),
            active_advance=CapabilityAssemblyActiveAdvance(
                operation_id=request.operation_id,
                request_sha256=request.request_sha256,
                maximum_slices=request.maximum_slices,
                start_completed_slices=current.completed_slices,
                target_completed_slices=target,
            ),
        )
        self._append(claimed, expected_revision=current.revision)
        return claimed

    async def advance(
        self,
        request: CapabilityAssemblyAdvanceRequest,
    ) -> CapabilityAssemblyAdvanceReceipt:
        self._validate_ports()
        history = self._history(request.assembly_id)
        current = history[-1].checkpoint
        self._require_request_binding(current, request)
        replay = self._receipt(current, request.operation_id)
        if replay is not None:
            self._require_receipt_request(replay, request)
            return replay

        current = self.claim_advance(request)
        active = current.active_advance
        if active is None:
            raise CapabilityAssemblyConflict("advance operation claim is unavailable")
        if current.started_slice is not None:
            started_record = self._history(request.assembly_id)[-1]
            current = await self._reconcile_started_slice(
                current,
                started_checkpoint_ref=started_record.artifact_ref,
            )
            active = current.active_advance
            if active is None:
                raise CapabilityAssemblyConflict(
                    "reconciliation lost the active advance"
                )
        while current.completed_slices < active.target_completed_slices:
            current = self._mark_next_slice_started(current)
            current = await self._execute_next_slice(current)
            active = current.active_advance
            if active is None:
                raise CapabilityAssemblyConflict("advance operation lost its durable claim")

        status = (
            CapabilityAssemblyStatus.READY_TO_USE
            if current.completed_slices == ASSEMBLY_TOTAL_SLICES
            else CapabilityAssemblyStatus.ADVANCING
        )
        receipt = CapabilityAssemblyAdvanceReceipt(
            assembly_id=current.assembly_id,
            operation_id=active.operation_id,
            request_sha256=active.request_sha256,
            maximum_slices=active.maximum_slices,
            start_completed_slices=active.start_completed_slices,
            end_completed_slices=current.completed_slices,
            executed_slices=current.completed_slices - active.start_completed_slices,
            status=status,
        )
        completed = _validated_copy(
            current,
            revision=current.revision + 1,
            status=status,
            active_advance=None,
            advance_receipts=(*current.advance_receipts, receipt),
        )
        self._append(completed, expected_revision=current.revision)
        return receipt

    def final_evidence(self, assembly_id: UUID) -> CapabilityLiveDemoEvidence:
        history = self._history(assembly_id)
        current = history[-1].checkpoint
        if (
            current.status is not CapabilityAssemblyStatus.READY_TO_USE
            or current.resume_dispatch is None
            or current.resume_readback is None
        ):
            raise CapabilityAssemblyIncomplete("capability assembly is not complete")
        if current.resume_dispatch != current.resume_readback:
            raise CapabilityAssemblyConflict("resume read-back changed the dispatched result")
        capabilities: list[CapabilityLiveCapabilityEvidence] = []
        for item in current.items:
            capability = item.capability_evidence
            if capability is None:
                raise CapabilityAssemblyIncomplete("capability projection is incomplete")
            checkpoint_refs = tuple(
                record.artifact_ref
                for record in history
                if record.checkpoint.last_slice is not None
                and record.checkpoint.last_slice.input_id == item.request.input_id
            )
            if not checkpoint_refs:
                raise CapabilityAssemblyIncomplete("capability checkpoints are unavailable")
            capabilities.append(_validated_copy(capability, checkpoint_refs=checkpoint_refs))
        first = capabilities[0]
        resumed = current.resume_readback
        restart = CapabilityLiveRestartEvidence(
            run_id=current.assembly_id,
            job_id=first.job_id,
            reservation_id=current.items[0].request.reservation.reservation_id,
            correlation_id=first.correlation_id,
            capability_id=first.capability_id,
            capability_version=first.capability_version,
            original_execution_command_id=cast(UUID, first.execution_command_id),
            original_execution_result_id=cast(UUID, first.execution_result_id),
            resumed_execution_authority_id=resumed.execution_authority_id,
            resumed_execution_command_id=resumed.execution_command_id,
            resumed_execution_result_id=resumed.execution_result_id,
            projection_event_id=first.projection_event_id,
            projection_readback_id=first.projection_readback_id,
            resume_actual_spend_usd=(
                resumed.resume_actual_spend_usd or Decimal("0")
            ),
            total_booked_cost_usd=resumed.booked_cost_usd,
            resume_budget_violation=resumed.resume_budget_violation,
        )
        booked = sum(
            (capability.booked_cost_usd for capability in capabilities),
            Decimal("0"),
        )
        return validate_live_demo_evidence(
            CapabilityLiveDemoEvidence(
                run_id=current.assembly_id,
                maximum_cost_usd=current.request.maximum_cost_usd,
                booked_cost_usd=booked,
                reservations=tuple(item.request.reservation for item in current.items),
                capabilities=tuple(capabilities),
                restart=restart,
            )
        )

    async def _execute_next_slice(
        self,
        current: CapabilityAssemblyCheckpoint,
    ) -> CapabilityAssemblyCheckpoint:
        ordinal = current.completed_slices
        if ordinal < len(ASSEMBLY_INPUT_STAGES) * 3:
            input_index, stage_index = divmod(ordinal, len(ASSEMBLY_INPUT_STAGES))
            stage = ASSEMBLY_INPUT_STAGES[stage_index]
            return await self._execute_input_stage(
                current,
                ordinal=ordinal,
                input_index=input_index,
                stage=stage,
            )
        if ordinal == len(ASSEMBLY_INPUT_STAGES) * 3:
            return await self._authorize_resume(current, ordinal=ordinal)
        if ordinal == len(ASSEMBLY_INPUT_STAGES) * 3 + 1:
            return await self._dispatch_resume(current, ordinal=ordinal)
        if ordinal == len(ASSEMBLY_INPUT_STAGES) * 3 + 2:
            return await self._readback_resume(current, ordinal=ordinal)
        raise CapabilityAssemblyConflict("assembly has no remaining bounded slice")

    def _mark_next_slice_started(
        self,
        current: CapabilityAssemblyCheckpoint,
    ) -> CapabilityAssemblyCheckpoint:
        if current.started_slice is not None:
            raise CapabilityAssemblyIncomplete(
                "started slice has no terminal authority; effect replay is forbidden"
            )
        if current.active_advance is None:
            raise CapabilityAssemblyConflict("slice start lacks an active advance")
        started = _validated_copy(
            current,
            revision=current.revision + 1,
            started_slice=_slice_for_ordinal(current.completed_slices, current),
        )
        self._append(started, expected_revision=current.revision)
        return started

    async def _reconcile_started_slice(
        self,
        current: CapabilityAssemblyCheckpoint,
        *,
        started_checkpoint_ref,
    ) -> CapabilityAssemblyCheckpoint:
        started = current.started_slice
        active = current.active_advance
        if started is None or active is None:
            raise CapabilityAssemblyConflict("reconciliation lacks a started slice")
        request = CapabilityAssemblyReconciliationRequest(
            assembly_id=current.assembly_id,
            operation_id=active.operation_id,
            request_sha256=current.request_sha256,
            started_slice=started,
            started_checkpoint_ref=started_checkpoint_ref,
        )
        outcome = await self._reconciliation.reconcile(request)
        if outcome is None:
            raise CapabilityAssemblyIncomplete(
                "started slice has no authoritative reconciliation"
            )
        if (
            outcome.assembly_id != request.assembly_id
            or outcome.operation_id != request.operation_id
            or outcome.request_sha256 != request.request_sha256
            or outcome.started_slice != request.started_slice
            or outcome.started_checkpoint_ref != request.started_checkpoint_ref
        ):
            raise CapabilityAssemblyConflict(
                "reconciliation changed the started slice authority"
            )
        if outcome.disposition == "retry_authorized_no_effect":
            self._require_fresh_retry_authorization(
                current.reconciliation_outcomes,
                outcome,
            )
            cleared = _validated_copy(
                current,
                revision=current.revision + 1,
                started_slice=None,
                reconciliation_outcomes=(
                    *current.reconciliation_outcomes,
                    outcome,
                ),
            )
            authorization = outcome.retry_authorization
            if authorization is None:
                raise CapabilityAssemblyConflict(
                    "no-effect retry lacks an authorization receipt"
                )
            self._consume_retry_authorization_and_append(
                cleared,
                authorization,
                expected_revision=current.revision,
            )
            return cleared
        return self._complete_from_reconciliation(current, outcome)

    @staticmethod
    def _require_fresh_retry_authorization(
        persisted_outcomes: tuple[CapabilityAssemblyReconciliationOutcome, ...],
        proposed_outcome: CapabilityAssemblyReconciliationOutcome,
    ) -> None:
        proposed = proposed_outcome.retry_authorization
        if proposed is None:
            raise CapabilityAssemblyConflict(
                "no-effect retry lacks an authorization receipt"
            )
        for persisted_outcome in persisted_outcomes:
            persisted = persisted_outcome.retry_authorization
            if (
                persisted is None
                or persisted.authorization_receipt_id
                != proposed.authorization_receipt_id
            ):
                continue
            if canonical_assembly_model_bytes(persisted) != canonical_assembly_model_bytes(
                proposed
            ):
                raise CapabilityAssemblyConflict(
                    "authorization receipt changed its binding or evidence"
                )
            raise CapabilityAssemblyConflict(
                "authorization receipt was already consumed"
            )

    def _complete_from_reconciliation(
        self,
        current: CapabilityAssemblyCheckpoint,
        outcome: CapabilityAssemblyReconciliationOutcome,
    ) -> CapabilityAssemblyCheckpoint:
        started = current.started_slice
        if started is None:
            raise CapabilityAssemblyConflict("reconciliation lost the started slice")
        if started.input_id is not None:
            input_index = next(
                (
                    index
                    for index, item in enumerate(current.items)
                    if item.request.input_id == started.input_id
                ),
                None,
            )
            if input_index is None:
                raise CapabilityAssemblyConflict(
                    "reconciliation input is unavailable"
                )
            item = current.items[input_index]
            stage = CapabilityLiveStage(started.kind.value)
            request = self._stage_request(current, item, stage)
            changes: dict[str, object] = {
                "completed_stages": (*item.completed_stages, stage),
            }
            if stage in (
                CapabilityLiveStage.SCAFFOLD_AND_TEST_HARNESS,
                CapabilityLiveStage.CODEX_IMPLEMENTATION,
                CapabilityLiveStage.SEAL,
            ):
                authority = outcome.factory_authority
                self._require_factory(
                    request,
                    authority,
                    expected=stage,
                    prior=item,
                )
                changes["stage_authority"] = authority
            elif stage is CapabilityLiveStage.EVALUATE:
                authority = outcome.benchmark_authority
                self._require_factory(
                    request,
                    authority,
                    expected=stage,
                    prior=item,
                )
                if authority is None:
                    raise CapabilityAssemblyIncomplete(
                        "benchmark reconciliation is unavailable"
                    )
                changes["benchmark_authority"] = authority
            elif stage is CapabilityLiveStage.PROMOTE:
                authority = outcome.factory_authority
                self._require_factory(
                    request,
                    authority,
                    expected=stage,
                    prior=item,
                )
                self._require_release(
                    authority,
                    item,
                    controlled_recovery=input_index == 0,
                )
                changes["release_authority"] = authority
            elif stage is CapabilityLiveStage.EXECUTE:
                release = self._release(item)
                execution = outcome.execution_authority
                self._require_execution(
                    request,
                    release,
                    execution,
                    fresh_resume=False,
                )
                changes["execution_authority"] = execution
            elif stage is CapabilityLiveStage.PROJECT:
                release = self._release(item)
                execution = self._execution_authority(item)
                projection = outcome.projection_authority
                self._require_projection(release, execution, projection)
                if projection is None:
                    raise CapabilityAssemblyIncomplete(
                        "projection reconciliation is unavailable"
                    )
                changes["projection_authority"] = projection
                changes["capability_evidence"] = self._capability_evidence(
                    item,
                    release=release,
                    execution=execution,
                    projection=projection,
                )
            else:
                raise CapabilityAssemblyConflict(
                    "unsupported reconciled input stage"
                )
            items = list(current.items)
            items[input_index] = _validated_copy(item, **changes)
            return self._append_slice(
                current,
                items=tuple(items),
                kind=started.kind,
                input_id=started.input_id,
                ordinal=started.ordinal,
                reconciliation_outcome=outcome,
            )

        if started.kind is CapabilityAssemblySliceKind.RESUME_AUTHORIZE:
            authorization = outcome.resume_authorization
            if authorization is None:
                raise CapabilityAssemblyIncomplete(
                    "resume authorization reconciliation is unavailable"
                )
            self._require_resume_authorization(current, authorization)
            return self._append_slice(
                current,
                items=current.items,
                kind=started.kind,
                input_id=None,
                ordinal=started.ordinal,
                resume_authorization=authorization,
                reconciliation_outcome=outcome,
            )
        if started.kind is CapabilityAssemblySliceKind.RESUME_DISPATCH:
            execution = outcome.execution_authority
            authorization = current.resume_authorization
            if execution is None or authorization is None:
                raise CapabilityAssemblyIncomplete(
                    "resume dispatch reconciliation is unavailable"
                )
            release = self._release(current.items[0])
            request = self._stage_request(
                current,
                current.items[0],
                CapabilityLiveStage.PROJECT,
            )
            self._require_execution(
                request,
                release,
                execution,
                fresh_resume=True,
            )
            if execution.execution_authority_id != authorization.authorization_receipt_id:
                raise CapabilityAssemblyConflict(
                    "reconciled resume dispatch lacks authorization binding"
                )
            self._require_fresh_identity(current.items[0], execution)
            return self._append_slice(
                current,
                items=current.items,
                kind=started.kind,
                input_id=None,
                ordinal=started.ordinal,
                resume_dispatch=execution,
                reconciliation_outcome=outcome,
            )
        execution = outcome.execution_authority
        if execution is None or execution != current.resume_dispatch:
            raise CapabilityAssemblyConflict(
                "reconciled resume read-back changed dispatched authority"
            )
        return self._append_slice(
            current,
            items=current.items,
            kind=started.kind,
            input_id=None,
            ordinal=started.ordinal,
            resume_readback=execution,
            reconciliation_outcome=outcome,
            status=CapabilityAssemblyStatus.READY_TO_USE,
        )

    async def _execute_input_stage(
        self,
        current: CapabilityAssemblyCheckpoint,
        *,
        ordinal: int,
        input_index: int,
        stage: CapabilityLiveStage,
    ) -> CapabilityAssemblyCheckpoint:
        item = current.items[input_index]
        request = CapabilityAssemblyStageRequest(
            run_id=current.assembly_id,
            input=item.admitted.input,
            correlation_id=item.request.correlation_id,
            reservation=item.request.reservation,
            stage=stage,
            controlled_recovery_required=input_index == 0,
            suite_ref=self._suite_ref(item),
            hermes_result_ref=self._hermes_ref(item),
            retry_number=0,
        )
        changes: dict[str, object] = {
            "completed_stages": (*item.completed_stages, stage),
        }
        if stage is CapabilityLiveStage.SCAFFOLD_AND_TEST_HARNESS:
            authority = await self._factory.scaffold(request)
            self._require_factory(request, authority, expected=stage, prior=item)
            changes["stage_authority"] = authority
        elif stage is CapabilityLiveStage.CODEX_IMPLEMENTATION:
            authority = await self._factory.implement(request)
            self._require_factory(request, authority, expected=stage, prior=item)
            changes["stage_authority"] = authority
        elif stage is CapabilityLiveStage.SEAL:
            authority = await self._factory.seal(request)
            self._require_factory(request, authority, expected=stage, prior=item)
            changes["stage_authority"] = authority
        elif stage is CapabilityLiveStage.EVALUATE:
            authority = await self._benchmark.evaluate(request)
            self._require_factory(request, authority, expected=stage, prior=item)
            if not isinstance(authority, CapabilityBusinessBenchmarkAuthority):
                raise CapabilityAssemblyConflict("benchmark authority is unavailable")
            changes["benchmark_authority"] = authority
        elif stage is CapabilityLiveStage.PROMOTE:
            authority = await self._factory.promote(request)
            self._require_factory(request, authority, expected=stage, prior=item)
            self._require_release(authority, item, controlled_recovery=input_index == 0)
            changes["release_authority"] = authority
        elif stage is CapabilityLiveStage.EXECUTE:
            release = self._release(item)
            execution = await self._execution.execute(request, release)
            self._require_execution(request, release, execution, fresh_resume=False)
            changes["execution_authority"] = execution
        elif stage is CapabilityLiveStage.PROJECT:
            release = self._release(item)
            execution = self._execution_authority(item)
            projection = await self._projection.project_and_readback(
                request,
                release,
                execution,
            )
            self._require_projection(release, execution, projection)
            changes["projection_authority"] = projection
            changes["capability_evidence"] = self._capability_evidence(
                item,
                release=release,
                execution=execution,
                projection=cast(CapabilityProjectionAuthority, projection),
            )
        else:
            raise CapabilityAssemblyConflict("unsupported assembly input stage")

        items = list(current.items)
        items[input_index] = _validated_copy(item, **changes)
        return self._append_slice(
            current,
            items=tuple(items),
            kind=CapabilityAssemblySliceKind(stage.value),
            input_id=item.request.input_id,
            ordinal=ordinal,
        )

    async def _authorize_resume(
        self,
        current: CapabilityAssemblyCheckpoint,
        *,
        ordinal: int,
    ) -> CapabilityAssemblyCheckpoint:
        first = self._completed_capability(current.items[0])
        if first.execution_command_id is None or first.execution_result_id is None:
            raise CapabilityAssemblyIncomplete("original execution evidence is unavailable")
        reservation = current.items[0].request.reservation
        authorization = await self._resume.authorize_resume(
            CapabilityResumeExecutionRequest(
                job_id=first.job_id,
                reservation_id=reservation.reservation_id,
                run_id=reservation.run_id,
                input_id=current.items[0].request.input_id,
                maximum_cost_usd=reservation.maximum_cost_usd,
                booked_cost_usd=first.booked_cost_usd,
                correlation_id=first.correlation_id,
                capability_id=first.capability_id,
                capability_version=first.capability_version,
                original_execution_command_id=first.execution_command_id,
                original_execution_result_id=first.execution_result_id,
            )
        )
        if authorization is None:
            raise CapabilityAssemblyIncomplete("resume authorization is unavailable")
        self._require_resume_authorization(current, authorization)
        return self._append_slice(
            current,
            items=current.items,
            kind=CapabilityAssemblySliceKind.RESUME_AUTHORIZE,
            input_id=None,
            ordinal=ordinal,
            resume_authorization=authorization,
        )

    async def _dispatch_resume(
        self,
        current: CapabilityAssemblyCheckpoint,
        *,
        ordinal: int,
    ) -> CapabilityAssemblyCheckpoint:
        authorization = current.resume_authorization
        if authorization is None:
            raise CapabilityAssemblyIncomplete("resume authorization is unavailable")
        execution = await self._resume.dispatch_resume(authorization)
        release = self._release(current.items[0])
        request = self._stage_request(current, current.items[0], CapabilityLiveStage.PROJECT)
        self._require_execution(request, release, execution, fresh_resume=True)
        if (
            cast(CapabilityExecutionAuthority, execution).execution_authority_id
            != authorization.authorization_receipt_id
        ):
            raise CapabilityAssemblyConflict("resume dispatch lacks authorization binding")
        self._require_fresh_identity(
            current.items[0],
            cast(CapabilityExecutionAuthority, execution),
        )
        return self._append_slice(
            current,
            items=current.items,
            kind=CapabilityAssemblySliceKind.RESUME_DISPATCH,
            input_id=None,
            ordinal=ordinal,
            resume_dispatch=cast(CapabilityExecutionAuthority, execution),
        )

    async def _readback_resume(
        self,
        current: CapabilityAssemblyCheckpoint,
        *,
        ordinal: int,
    ) -> CapabilityAssemblyCheckpoint:
        authorization = current.resume_authorization
        dispatched = current.resume_dispatch
        if authorization is None or dispatched is None:
            raise CapabilityAssemblyIncomplete("resume dispatch is unavailable")
        readback = await self._resume.readback_resume(authorization)
        if readback is None:
            raise CapabilityAssemblyIncomplete("resume read-back is unavailable")
        if readback != dispatched:
            raise CapabilityAssemblyConflict("resume read-back changed dispatched authority")
        return self._append_slice(
            current,
            items=current.items,
            kind=CapabilityAssemblySliceKind.RESUME_READBACK,
            input_id=None,
            ordinal=ordinal,
            resume_readback=readback,
            status=CapabilityAssemblyStatus.READY_TO_USE,
        )

    def _append_slice(
        self,
        current: CapabilityAssemblyCheckpoint,
        *,
        items: tuple[CapabilityAssemblyItemState, ...],
        kind: CapabilityAssemblySliceKind,
        input_id: str | None,
        ordinal: int,
        resume_authorization: CapabilityResumeAuthorization | None = None,
        resume_dispatch: CapabilityExecutionAuthority | None = None,
        resume_readback: CapabilityExecutionAuthority | None = None,
        status: CapabilityAssemblyStatus = CapabilityAssemblyStatus.ADVANCING,
        reconciliation_outcome: CapabilityAssemblyReconciliationOutcome | None = None,
    ) -> CapabilityAssemblyCheckpoint:
        if reconciliation_outcome is None:
            self._record_terminal_readback(
                current,
                items=items,
                kind=kind,
                input_id=input_id,
                resume_authorization=resume_authorization,
                resume_dispatch=resume_dispatch,
                resume_readback=resume_readback,
            )
        completed = _validated_copy(
            current,
            revision=current.revision + 1,
            status=status,
            completed_slices=current.completed_slices + 1,
            items=items,
            resume_authorization=(
                resume_authorization
                if resume_authorization is not None
                else current.resume_authorization
            ),
            resume_dispatch=(
                resume_dispatch
                if resume_dispatch is not None
                else current.resume_dispatch
            ),
            resume_readback=(
                resume_readback
                if resume_readback is not None
                else current.resume_readback
            ),
            last_slice=CapabilityAssemblySlice(
                ordinal=ordinal,
                kind=kind,
                input_id=input_id,
            ),
            started_slice=None,
            reconciliation_outcomes=(
                (*current.reconciliation_outcomes, reconciliation_outcome)
                if reconciliation_outcome is not None
                else current.reconciliation_outcomes
            ),
        )
        self._append(completed, expected_revision=current.revision)
        return completed

    def _record_terminal_readback(
        self,
        current: CapabilityAssemblyCheckpoint,
        *,
        items: tuple[CapabilityAssemblyItemState, ...],
        kind: CapabilityAssemblySliceKind,
        input_id: str | None,
        resume_authorization: CapabilityResumeAuthorization | None,
        resume_dispatch: CapabilityExecutionAuthority | None,
        resume_readback: CapabilityExecutionAuthority | None,
    ) -> None:
        recorder = getattr(self._reconciliation, "record_terminal", None)
        if recorder is None:
            return
        started = current.started_slice
        active = current.active_advance
        if started is None or active is None or started.kind is not kind:
            raise CapabilityAssemblyConflict(
                "terminal reconciliation lacks its started slice"
            )
        record = self._history(current.assembly_id)[-1]
        if record.checkpoint != current:
            raise CapabilityAssemblyConflict(
                "terminal reconciliation checkpoint changed before readback"
            )
        request = CapabilityAssemblyReconciliationRequest(
            assembly_id=current.assembly_id,
            operation_id=active.operation_id,
            request_sha256=current.request_sha256,
            started_slice=started,
            started_checkpoint_ref=record.artifact_ref,
        )
        authorities: dict[str, object] = {}
        if input_id is not None:
            item = next(
                (candidate for candidate in items if candidate.request.input_id == input_id),
                None,
            )
            if item is None:
                raise CapabilityAssemblyConflict(
                    "terminal reconciliation input is unavailable"
                )
            if kind in {
                CapabilityAssemblySliceKind.SCAFFOLD_AND_TEST_HARNESS,
                CapabilityAssemblySliceKind.CODEX_IMPLEMENTATION,
                CapabilityAssemblySliceKind.SEAL,
                CapabilityAssemblySliceKind.PROMOTE,
            }:
                authorities["factory_authority"] = (
                    item.release_authority
                    if kind is CapabilityAssemblySliceKind.PROMOTE
                    else item.stage_authority
                )
            elif kind is CapabilityAssemblySliceKind.EVALUATE:
                authorities["benchmark_authority"] = item.benchmark_authority
            elif kind is CapabilityAssemblySliceKind.EXECUTE:
                authorities["execution_authority"] = item.execution_authority
            elif kind is CapabilityAssemblySliceKind.PROJECT:
                authorities["projection_authority"] = item.projection_authority
        elif kind is CapabilityAssemblySliceKind.RESUME_AUTHORIZE:
            authorities["resume_authorization"] = resume_authorization
        else:
            authorities["execution_authority"] = (
                resume_readback
                if kind is CapabilityAssemblySliceKind.RESUME_READBACK
                else resume_dispatch
            )
        outcome = CapabilityAssemblyReconciliationOutcome(
            assembly_id=request.assembly_id,
            operation_id=request.operation_id,
            request_sha256=request.request_sha256,
            started_slice=request.started_slice,
            started_checkpoint_ref=request.started_checkpoint_ref,
            disposition="terminal_readback",
            **authorities,
        )
        recorder(request, outcome)

    def _append(self, checkpoint: CapabilityAssemblyCheckpoint, *, expected_revision: int) -> None:
        try:
            record = self._repository.append(
                checkpoint,
                expected_revision=expected_revision,
            )
        except CapabilityAssemblyWriteConflict as exc:
            raise CapabilityAssemblyConflict("assembly append lost sole-writer fencing") from exc
        if record.checkpoint != checkpoint:
            raise CapabilityAssemblyConflict("assembly repository changed checkpoint bytes")
        self._require_record_digest(record)

    def _consume_retry_authorization_and_append(
        self,
        checkpoint: CapabilityAssemblyCheckpoint,
        authorization: CapabilityAssemblyNoEffectRetryAuthorization,
        *,
        expected_revision: int,
    ) -> None:
        try:
            record = self._repository.consume_retry_authorization_and_append(
                checkpoint,
                authorization,
                expected_revision=expected_revision,
            )
        except CapabilityAssemblyWriteConflict as exc:
            raise CapabilityAssemblyConflict(
                "assembly append lost sole-writer fencing"
            ) from exc
        if record.checkpoint != checkpoint:
            raise CapabilityAssemblyConflict("assembly repository changed checkpoint bytes")
        self._require_record_digest(record)

    def _validate_ports(self) -> None:
        positional = object()
        method_specs = (
            (self._repository, "history", False, (positional,), {}),
            (self._repository, "find_by_idempotency_key", False, (positional,), {}),
            (
                self._repository,
                "append",
                False,
                (positional,),
                {"expected_revision": 0},
            ),
            (
                self._repository,
                "consume_retry_authorization_and_append",
                False,
                (positional, positional),
                {"expected_revision": 0},
            ),
            (self._admission, "admit", False, (positional,), {}),
            (self._factory, "scaffold", True, (positional,), {}),
            (self._factory, "implement", True, (positional,), {}),
            (self._factory, "seal", True, (positional,), {}),
            (self._factory, "promote", True, (positional,), {}),
            (self._benchmark, "evaluate", True, (positional,), {}),
            (self._resume, "authorize_resume", True, (positional,), {}),
            (self._resume, "dispatch_resume", True, (positional,), {}),
            (self._resume, "readback_resume", True, (positional,), {}),
            (self._reconciliation, "reconcile", True, (positional,), {}),
            (self._execution, "execute", True, (positional, positional), {}),
            (
                self._projection,
                "project_and_readback",
                True,
                (positional, positional, positional),
                {},
            ),
        )
        for port, method_name, async_required, args, kwargs in method_specs:
            method = getattr(port, method_name, None)
            if not callable(method) or inspect.iscoroutinefunction(method) is not async_required:
                raise ValueError(
                    f"capability assembly port binding {method_name} has wrong call mode"
                )
            try:
                inspect.signature(method).bind(*args, **kwargs)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"capability assembly port binding {method_name} has incompatible signature"
                ) from exc

    def _history_optional(self, assembly_id: UUID):
        history = self._repository.history(assembly_id)
        if history:
            self._validate_history(assembly_id, history)
        return history

    def _history(self, assembly_id: UUID):
        history = self._history_optional(assembly_id)
        if not history:
            raise CapabilityAssemblyNotFound(f"capability assembly {assembly_id} is unavailable")
        return history

    @staticmethod
    def _validate_history(assembly_id: UUID, history) -> None:
        first = history[0].checkpoint
        if first.request_sha256 != assembly_request_sha256(first.request):
            raise CapabilityAssemblyConflict(
                "assembly request digest does not match immutable submission"
            )
        if (
            first.assembly_id != assembly_id
            or first.revision != 1
            or first.completed_slices != 0
            or first.status is not CapabilityAssemblyStatus.ADMITTED
        ):
            raise CapabilityAssemblyConflict("assembly history is not monotone")
        consumed_authorizations: dict[UUID, bytes] = {}
        prior_outcome_count = 0
        for expected_revision, record in enumerate(history, start=1):
            CapabilityAssemblyController._require_record_digest(record)
            checkpoint = record.checkpoint
            CapabilityAssemblyController._require_checkpoint_shape(checkpoint)
            if (
                checkpoint.assembly_id != assembly_id
                or checkpoint.revision != expected_revision
                or checkpoint.request != first.request
                or checkpoint.request_sha256 != first.request_sha256
            ):
                raise CapabilityAssemblyConflict("assembly history is not monotone")
            if len(checkpoint.reconciliation_outcomes) < prior_outcome_count:
                raise CapabilityAssemblyConflict("assembly history is not monotone")
            for outcome in checkpoint.reconciliation_outcomes[prior_outcome_count:]:
                authorization = outcome.retry_authorization
                if authorization is None:
                    continue
                receipt_id = authorization.authorization_receipt_id
                binding = canonical_assembly_model_bytes(authorization)
                prior_binding = consumed_authorizations.get(receipt_id)
                if prior_binding is not None:
                    if prior_binding != binding:
                        raise CapabilityAssemblyConflict(
                            "authorization receipt changed its binding or evidence"
                        )
                    raise CapabilityAssemblyConflict(
                        "authorization receipt was consumed more than once"
                    )
                consumed_authorizations[receipt_id] = binding
            prior_outcome_count = len(checkpoint.reconciliation_outcomes)
            if expected_revision == 1:
                continue
            prior = history[expected_revision - 2].checkpoint
            delta = checkpoint.completed_slices - prior.completed_slices
            if delta not in (0, 1):
                raise CapabilityAssemblyConflict("assembly history is not monotone")
            CapabilityAssemblyController._require_history_transition(
                prior,
                checkpoint,
                completed_delta=delta,
                prior_record_ref=history[expected_revision - 2].artifact_ref,
            )

    @staticmethod
    def _require_record_digest(record) -> None:
        expected = hashlib.sha256(
            canonical_assembly_model_bytes(record.checkpoint)
        ).hexdigest()
        if (
            record.artifact_ref.sha256 != expected
            or record.artifact_ref.media_type != "application/json"
        ):
            raise CapabilityAssemblyConflict(
                "assembly repository artifact digest does not bind checkpoint bytes"
            )

    @staticmethod
    def _require_checkpoint_shape(checkpoint: CapabilityAssemblyCheckpoint) -> None:
        CapabilityAssemblyController._require_distinct_sources(checkpoint.inputs)
        stage_slices = sum(len(item.completed_stages) for item in checkpoint.items)
        resume_count = 0
        if checkpoint.resume_authorization is not None:
            resume_count = 1
        if checkpoint.resume_dispatch is not None:
            if checkpoint.resume_authorization is None:
                raise CapabilityAssemblyConflict("assembly history is not monotone")
            resume_count = 2
        if checkpoint.resume_readback is not None:
            if checkpoint.resume_dispatch is None:
                raise CapabilityAssemblyConflict("assembly history is not monotone")
            resume_count = 3
        if checkpoint.completed_slices != stage_slices + resume_count:
            raise CapabilityAssemblyConflict("assembly history is not monotone")
        if checkpoint.completed_slices == ASSEMBLY_TOTAL_SLICES:
            if checkpoint.status is not CapabilityAssemblyStatus.READY_TO_USE:
                raise CapabilityAssemblyConflict("assembly history is not monotone")
        elif checkpoint.status is CapabilityAssemblyStatus.READY_TO_USE:
            raise CapabilityAssemblyConflict("assembly history is not monotone")
        if checkpoint.active_advance is not None and not (
            checkpoint.active_advance.start_completed_slices
            <= checkpoint.completed_slices
            <= checkpoint.active_advance.target_completed_slices
        ):
            raise CapabilityAssemblyConflict("assembly history is not monotone")
        if checkpoint.started_slice is not None:
            if (
                not isinstance(checkpoint.started_slice, CapabilityAssemblySlice)
                or checkpoint.active_advance is None
                or checkpoint.started_slice
                != _slice_for_ordinal(checkpoint.completed_slices, checkpoint)
            ):
                raise CapabilityAssemblyConflict("assembly history is not monotone")
        if checkpoint.completed_slices == 0:
            if checkpoint.last_slice is not None:
                raise CapabilityAssemblyConflict("assembly history is not monotone")
            return
        last = checkpoint.last_slice
        if not isinstance(last, CapabilityAssemblySlice):
            raise CapabilityAssemblyConflict("assembly history is not monotone")
        expected = _slice_for_ordinal(checkpoint.completed_slices - 1, checkpoint)
        if last != expected:
            raise CapabilityAssemblyConflict("assembly history is not monotone")

    @staticmethod
    def _require_distinct_sources(admitted) -> None:
        source_refs = tuple(item.input.source_ref for item in admitted)
        if (
            len(source_refs) != 3
            or len(set(source_refs)) != 3
            or len({reference.sha256 for reference in source_refs}) != 3
            or len({reference.uri for reference in source_refs}) != 3
        ):
            raise CapabilityAssemblyConflict(
                "assembly requires three distinct source refs and content digests"
            )

    @staticmethod
    def _require_history_transition(
        prior: CapabilityAssemblyCheckpoint,
        current: CapabilityAssemblyCheckpoint,
        *,
        completed_delta: int,
        prior_record_ref,
    ) -> None:
        authority_before = (
            prior.items,
            prior.resume_authorization,
            prior.resume_dispatch,
            prior.resume_readback,
        )
        authority_after = (
            current.items,
            current.resume_authorization,
            current.resume_dispatch,
            current.resume_readback,
        )
        if completed_delta == 0:
            if authority_after != authority_before or current.last_slice != prior.last_slice:
                raise CapabilityAssemblyConflict("assembly history is not monotone")
            if prior.active_advance is None and current.active_advance is not None:
                if (
                    current.advance_receipts != prior.advance_receipts
                    or current.reconciliation_outcomes
                    != prior.reconciliation_outcomes
                    or prior.started_slice is not None
                    or current.started_slice is not None
                ):
                    raise CapabilityAssemblyConflict("assembly history is not monotone")
                active = current.active_advance
                if (
                    active.start_completed_slices != prior.completed_slices
                    or active.target_completed_slices
                    != min(
                        prior.completed_slices + active.maximum_slices,
                        ASSEMBLY_TOTAL_SLICES,
                    )
                ):
                    raise CapabilityAssemblyConflict("assembly history is not monotone")
                return
            if (
                prior.active_advance is not None
                and current.active_advance == prior.active_advance
                and prior.started_slice is None
                and current.started_slice
                == _slice_for_ordinal(prior.completed_slices, prior)
                and current.advance_receipts == prior.advance_receipts
                and current.reconciliation_outcomes
                == prior.reconciliation_outcomes
            ):
                return
            if (
                prior.active_advance is not None
                and current.active_advance == prior.active_advance
                and prior.started_slice is not None
                and current.started_slice is None
                and current.advance_receipts == prior.advance_receipts
                and len(current.reconciliation_outcomes)
                == len(prior.reconciliation_outcomes) + 1
                and current.reconciliation_outcomes[:-1]
                == prior.reconciliation_outcomes
            ):
                outcome = current.reconciliation_outcomes[-1]
                if (
                    outcome.disposition == "retry_authorized_no_effect"
                    and outcome.started_slice == prior.started_slice
                    and outcome.started_checkpoint_ref == prior_record_ref
                ):
                    return
            if prior.active_advance is not None and current.active_advance is None:
                if (
                    prior.started_slice is not None
                    or current.started_slice is not None
                    or current.reconciliation_outcomes
                    != prior.reconciliation_outcomes
                    or len(current.advance_receipts) != len(prior.advance_receipts) + 1
                ):
                    raise CapabilityAssemblyConflict("assembly history is not monotone")
                if current.advance_receipts[:-1] != prior.advance_receipts:
                    raise CapabilityAssemblyConflict("assembly history is not monotone")
                receipt = current.advance_receipts[-1]
                active = prior.active_advance
                if (
                    receipt.operation_id != active.operation_id
                    or receipt.request_sha256 != active.request_sha256
                    or receipt.maximum_slices != active.maximum_slices
                    or receipt.start_completed_slices != active.start_completed_slices
                    or receipt.end_completed_slices != prior.completed_slices
                ):
                    raise CapabilityAssemblyConflict("assembly history is not monotone")
                return
            raise CapabilityAssemblyConflict("assembly history is not monotone")

        if (
            prior.active_advance is None
            or current.active_advance != prior.active_advance
            or current.advance_receipts != prior.advance_receipts
            or current.completed_slices > prior.active_advance.target_completed_slices
            or prior.started_slice != _slice_for_ordinal(prior.completed_slices, prior)
            or current.started_slice is not None
        ):
            raise CapabilityAssemblyConflict("assembly history is not monotone")
        expected_slice = _slice_for_ordinal(prior.completed_slices, prior)
        if current.last_slice != expected_slice:
            raise CapabilityAssemblyConflict("assembly history is not monotone")
        if current.reconciliation_outcomes == prior.reconciliation_outcomes:
            pass
        elif (
            len(current.reconciliation_outcomes)
            == len(prior.reconciliation_outcomes) + 1
            and current.reconciliation_outcomes[:-1]
            == prior.reconciliation_outcomes
        ):
            outcome = current.reconciliation_outcomes[-1]
            if (
                outcome.disposition != "terminal_readback"
                or outcome.started_slice != prior.started_slice
                or outcome.started_checkpoint_ref != prior_record_ref
            ):
                raise CapabilityAssemblyConflict("assembly history is not monotone")
        else:
            raise CapabilityAssemblyConflict("assembly history is not monotone")
        CapabilityAssemblyController._require_authority_transition(
            prior,
            current,
            expected_slice,
        )

    @staticmethod
    def _require_authority_transition(
        prior: CapabilityAssemblyCheckpoint,
        current: CapabilityAssemblyCheckpoint,
        completed_slice: CapabilityAssemblySlice,
    ) -> None:
        if completed_slice.input_id is not None:
            input_index = next(
                (
                    index
                    for index, item in enumerate(prior.items)
                    if item.request.input_id == completed_slice.input_id
                ),
                None,
            )
            if input_index is None:
                raise CapabilityAssemblyConflict("assembly history is not monotone")
            for index, (before, after) in enumerate(
                zip(prior.items, current.items, strict=True)
            ):
                if index != input_index and before != after:
                    raise CapabilityAssemblyConflict("assembly history is not monotone")
            before = prior.items[input_index]
            after = current.items[input_index]
            expected_stage = CapabilityLiveStage(completed_slice.kind.value)
            if after.completed_stages != (*before.completed_stages, expected_stage):
                raise CapabilityAssemblyConflict("assembly history is not monotone")
            allowed_by_stage = {
                CapabilityLiveStage.SCAFFOLD_AND_TEST_HARNESS: {"stage_authority"},
                CapabilityLiveStage.CODEX_IMPLEMENTATION: {"stage_authority"},
                CapabilityLiveStage.SEAL: {"stage_authority"},
                CapabilityLiveStage.EVALUATE: {"benchmark_authority"},
                CapabilityLiveStage.PROMOTE: {"release_authority"},
                CapabilityLiveStage.EXECUTE: {"execution_authority"},
                CapabilityLiveStage.PROJECT: {
                    "projection_authority",
                    "capability_evidence",
                },
            }[expected_stage]
            before_payload = before.model_dump(mode="python")
            after_payload = after.model_dump(mode="python")
            for field in allowed_by_stage | {"completed_stages"}:
                before_payload.pop(field, None)
                after_payload.pop(field, None)
            if before_payload != after_payload:
                raise CapabilityAssemblyConflict("assembly history is not monotone")
            if (
                prior.resume_authorization,
                prior.resume_dispatch,
                prior.resume_readback,
            ) != (
                current.resume_authorization,
                current.resume_dispatch,
                current.resume_readback,
            ):
                raise CapabilityAssemblyConflict("assembly history is not monotone")
            return

        if current.items != prior.items:
            raise CapabilityAssemblyConflict("assembly history is not monotone")
        kind = completed_slice.kind
        if kind is CapabilityAssemblySliceKind.RESUME_AUTHORIZE:
            valid = (
                prior.resume_authorization is None
                and current.resume_authorization is not None
                and current.resume_dispatch is None
                and current.resume_readback is None
            )
        elif kind is CapabilityAssemblySliceKind.RESUME_DISPATCH:
            valid = (
                current.resume_authorization == prior.resume_authorization
                and prior.resume_dispatch is None
                and current.resume_dispatch is not None
                and current.resume_readback is None
            )
        else:
            valid = (
                current.resume_authorization == prior.resume_authorization
                and current.resume_dispatch == prior.resume_dispatch
                and prior.resume_readback is None
                and current.resume_readback is not None
            )
        if not valid:
            raise CapabilityAssemblyConflict("assembly history is not monotone")

    @staticmethod
    def _require_request_binding(
        current: CapabilityAssemblyCheckpoint,
        request: CapabilityAssemblyAdvanceRequest,
    ) -> None:
        if current.request_sha256 != request.request_sha256:
            raise CapabilityAssemblyConflict("advance changed the admitted request digest")
        active = current.active_advance
        if active is not None and active.operation_id != request.operation_id:
            raise CapabilityAssemblyConflict("another bounded advance is still active")

    @staticmethod
    def _receipt(
        current: CapabilityAssemblyCheckpoint,
        operation_id: UUID,
    ) -> CapabilityAssemblyAdvanceReceipt | None:
        matches = tuple(
            receipt
            for receipt in current.advance_receipts
            if receipt.operation_id == operation_id
        )
        if len(matches) > 1:
            raise CapabilityAssemblyConflict("advance receipt identity is duplicated")
        return None if not matches else matches[0]

    @staticmethod
    def _require_receipt_request(
        receipt: CapabilityAssemblyAdvanceReceipt,
        request: CapabilityAssemblyAdvanceRequest,
    ) -> None:
        if (
            receipt.assembly_id != request.assembly_id
            or receipt.request_sha256 != request.request_sha256
            or receipt.maximum_slices != request.maximum_slices
        ):
            raise CapabilityAssemblyConflict("advance operation replay changed its request")

    @staticmethod
    def _require_active_request(
        active: CapabilityAssemblyActiveAdvance,
        request: CapabilityAssemblyAdvanceRequest,
    ) -> None:
        if (
            active.operation_id != request.operation_id
            or active.request_sha256 != request.request_sha256
            or active.maximum_slices != request.maximum_slices
        ):
            raise CapabilityAssemblyConflict("active advance replay changed its request")

    @staticmethod
    def _suite_ref(item: CapabilityAssemblyItemState):
        if item.release_authority is not None:
            return item.release_authority.suite_ref
        if item.benchmark_authority is not None:
            return item.benchmark_authority.suite_ref
        return None if item.stage_authority is None else item.stage_authority.suite_ref

    @staticmethod
    def _hermes_ref(item: CapabilityAssemblyItemState):
        if item.release_authority is not None:
            return item.release_authority.hermes_result_ref
        if item.benchmark_authority is not None:
            return item.benchmark_authority.hermes_result_ref
        return None if item.stage_authority is None else item.stage_authority.hermes_result_ref

    @staticmethod
    def _require_factory(
        request: CapabilityAssemblyStageRequest,
        authority: CapabilityFactoryAuthority | None,
        *,
        expected: CapabilityLiveStage,
        prior: CapabilityAssemblyItemState,
    ) -> None:
        if authority is None:
            raise CapabilityAssemblyIncomplete("Factory authority is unavailable")
        if (
            authority.stage is not expected
            or authority.input_id != request.input.input_id
            or authority.source_ref != request.input.source_ref
            or authority.correlation_id != request.correlation_id
            or authority.booked_cost_usd > request.reservation.maximum_cost_usd
        ):
            raise CapabilityAssemblyConflict("Factory authority changed the stage binding")
        suite_ref = CapabilityAssemblyController._suite_ref(prior)
        hermes_ref = CapabilityAssemblyController._hermes_ref(prior)
        if suite_ref is not None and authority.suite_ref != suite_ref:
            raise CapabilityAssemblyConflict("Factory authority changed the benchmark suite")
        if hermes_ref is not None and authority.hermes_result_ref != hermes_ref:
            raise CapabilityAssemblyConflict("Factory authority changed the Hermes result")
        prior_authority = prior.stage_authority
        if prior_authority is not None and (
            authority.job_id != prior_authority.job_id
            or authority.capability_id != prior_authority.capability_id
            or authority.capability_version != prior_authority.capability_version
        ):
            raise CapabilityAssemblyConflict("Factory authority changed capability identity")
        prior_cost = CapabilityAssemblyController._latest_cost(prior)
        if authority.booked_cost_usd < prior_cost:
            raise CapabilityAssemblyConflict("Factory authority rewound booked cost")

    @staticmethod
    def _require_release(
        authority: CapabilityFactoryAuthority | None,
        item: CapabilityAssemblyItemState,
        *,
        controlled_recovery: bool,
    ) -> None:
        if authority is None:
            raise CapabilityAssemblyIncomplete("release authority is unavailable")
        benchmark = item.benchmark_authority
        if benchmark is None:
            raise CapabilityAssemblyIncomplete("benchmark authority is unavailable")
        if (
            authority.benchmark_authority_id != benchmark.benchmark_authority_id
            or authority.benchmark_summary_ref != benchmark.benchmark_summary_ref
            or authority.normal_trace_ids != benchmark.normal_trace_ids
            or authority.release_authority_id is None
            or (controlled_recovery and authority.controlled_recovery_id is None)
        ):
            raise CapabilityAssemblyConflict("release authority changed benchmark evidence")

    @staticmethod
    def _require_execution(
        request: CapabilityAssemblyStageRequest,
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority | None,
        *,
        fresh_resume: bool,
    ) -> None:
        if execution is None:
            raise CapabilityAssemblyIncomplete("execution authority is unavailable")
        if (
            execution.job_id != release.job_id
            or execution.correlation_id != release.correlation_id
            or execution.capability_id != release.capability_id
            or execution.capability_version != release.capability_version
            or execution.booked_cost_usd != release.booked_cost_usd
            or execution.booked_cost_usd > request.reservation.maximum_cost_usd
            or execution.terminal_status != "succeeded"
            or (fresh_resume and execution.replayed)
        ):
            raise CapabilityAssemblyConflict("execution authority changed release binding")

    @staticmethod
    def _require_projection(
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
        projection: CapabilityProjectionAuthority | None,
    ) -> None:
        if projection is None:
            raise CapabilityAssemblyIncomplete("Minibook projection authority is unavailable")
        if (
            projection.job_id != release.job_id
            or projection.correlation_id != release.correlation_id
            or projection.capability_id != release.capability_id
            or projection.capability_version != release.capability_version
            or projection.execution_command_id != execution.execution_command_id
            or projection.execution_result_id != execution.execution_result_id
        ):
            raise CapabilityAssemblyConflict("Minibook projection changed execution authority")

    @staticmethod
    def _release(item: CapabilityAssemblyItemState) -> CapabilityFactoryAuthority:
        if item.release_authority is None:
            raise CapabilityAssemblyIncomplete("release authority is unavailable")
        return item.release_authority

    @staticmethod
    def _execution_authority(item: CapabilityAssemblyItemState) -> CapabilityExecutionAuthority:
        if item.execution_authority is None:
            raise CapabilityAssemblyIncomplete("execution authority is unavailable")
        return item.execution_authority

    @staticmethod
    def _latest_cost(item: CapabilityAssemblyItemState) -> Decimal:
        if item.execution_authority is not None:
            return item.execution_authority.booked_cost_usd
        if item.release_authority is not None:
            return item.release_authority.booked_cost_usd
        if item.benchmark_authority is not None:
            return item.benchmark_authority.booked_cost_usd
        if item.stage_authority is not None:
            return item.stage_authority.booked_cost_usd
        return Decimal("0")

    @staticmethod
    def _completed_capability(
        item: CapabilityAssemblyItemState,
    ) -> CapabilityLiveCapabilityEvidence:
        if item.capability_evidence is None:
            raise CapabilityAssemblyIncomplete("capability projection is incomplete")
        return item.capability_evidence

    @staticmethod
    def _require_fresh_identity(
        item: CapabilityAssemblyItemState,
        execution: CapabilityExecutionAuthority,
    ) -> None:
        original = CapabilityAssemblyController._completed_capability(item)
        if execution.execution_command_id == original.execution_command_id:
            raise CapabilityAssemblyConflict("resume execution reused original command")
        if execution.execution_result_id == original.execution_result_id:
            raise CapabilityAssemblyConflict("resume execution reused original result")

    @staticmethod
    def _require_resume_authorization(
        current: CapabilityAssemblyCheckpoint,
        authorization: CapabilityResumeAuthorization,
    ) -> None:
        first = CapabilityAssemblyController._completed_capability(current.items[0])
        expected = (
            first.job_id,
            current.items[0].request.reservation.reservation_id,
            first.correlation_id,
            first.capability_id,
            first.capability_version,
        )
        observed = (
            authorization.job_id,
            authorization.reservation_id,
            authorization.correlation_id,
            authorization.capability_id,
            authorization.capability_version,
        )
        if observed != expected:
            raise CapabilityAssemblyConflict(
                "resume authorization changed release authority"
            )

    @staticmethod
    def _capability_evidence(
        item: CapabilityAssemblyItemState,
        *,
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
        projection: CapabilityProjectionAuthority,
    ) -> CapabilityLiveCapabilityEvidence:
        return CapabilityLiveCapabilityEvidence(
            input_id=item.request.input_id,
            conversation_pattern=item.admitted.input.conversation_pattern,
            job_id=release.job_id,
            correlation_id=release.correlation_id,
            capability_id=release.capability_id,
            capability_version=release.capability_version,
            controlled_recovery_id=release.controlled_recovery_id,
            normal_trace_ids=release.normal_trace_ids,
            release_authority_id=release.release_authority_id,
            execution_authority_id=execution.execution_authority_id,
            execution_command_id=execution.execution_command_id,
            execution_result_id=execution.execution_result_id,
            projection_event_id=projection.projection_event_id,
            projection_readback_id=projection.projection_readback_id,
            suite_ref=release.suite_ref,
            hermes_result_ref=release.hermes_result_ref,
            booked_cost_usd=execution.booked_cost_usd,
            checkpoint_refs=(),
        )

    @staticmethod
    def _stage_request(
        current: CapabilityAssemblyCheckpoint,
        item: CapabilityAssemblyItemState,
        stage: CapabilityLiveStage,
    ) -> CapabilityAssemblyStageRequest:
        return CapabilityAssemblyStageRequest(
            run_id=current.assembly_id,
            input=item.admitted.input,
            correlation_id=item.request.correlation_id,
            reservation=item.request.reservation,
            stage=stage,
            controlled_recovery_required=True,
            suite_ref=CapabilityAssemblyController._suite_ref(item),
            hermes_result_ref=CapabilityAssemblyController._hermes_ref(item),
            retry_number=0,
        )


def _slice_for_ordinal(
    ordinal: int,
    checkpoint: CapabilityAssemblyCheckpoint,
) -> CapabilityAssemblySlice:
    input_slice_count = len(ASSEMBLY_INPUT_STAGES) * 3
    if ordinal < input_slice_count:
        input_index, stage_index = divmod(ordinal, len(ASSEMBLY_INPUT_STAGES))
        return CapabilityAssemblySlice(
            ordinal=ordinal,
            kind=CapabilityAssemblySliceKind(ASSEMBLY_INPUT_STAGES[stage_index].value),
            input_id=checkpoint.items[input_index].request.input_id,
        )
    resume_kinds = (
        CapabilityAssemblySliceKind.RESUME_AUTHORIZE,
        CapabilityAssemblySliceKind.RESUME_DISPATCH,
        CapabilityAssemblySliceKind.RESUME_READBACK,
    )
    return CapabilityAssemblySlice(
        ordinal=ordinal,
        kind=resume_kinds[ordinal - input_slice_count],
        input_id=None,
    )


def _validated_copy(value: _ModelT, **updates: object) -> _ModelT:
    payload = value.model_dump(
        mode="python",
        by_alias=True,
        exclude_computed_fields=True,
    )
    payload.update(updates)
    return type(value).model_validate(payload)


__all__ = [
    "CapabilityAssemblyConflict",
    "CapabilityAssemblyController",
    "CapabilityAssemblyIncomplete",
    "CapabilityAssemblyNotFound",
]
