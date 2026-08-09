"""Persistent in-process controller for one three-input capability assembly.

The controller is deliberately server-side: clients submit stable identities
and request bounded slices, while all lifecycle effects remain behind injected
Captain/Gateway ports.  Each advance operation is durably claimed before its
first effect, so a transport retry resumes only that operation's remaining
slice allowance.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TypeVar, cast
from uuid import UUID

from pydantic import BaseModel

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveCapabilityEvidence,
    CapabilityLiveDemoEvidence,
    CapabilityLiveRestartEvidence,
    CapabilityLiveStage,
    CapabilityLiveStageRequest,
    validate_live_demo_evidence,
)
from gateway.capability_assembly_contracts import (
    ASSEMBLY_INPUT_STAGES,
    ASSEMBLY_TOTAL_SLICES,
    CapabilityAssemblyActiveAdvance,
    CapabilityAssemblyAdvanceReceipt,
    CapabilityAssemblyAdvanceRequest,
    CapabilityAssemblyCheckpoint,
    CapabilityAssemblyInputAdmissionPort,
    CapabilityAssemblyItemState,
    CapabilityAssemblyRepositoryPort,
    CapabilityAssemblySlice,
    CapabilityAssemblySliceKind,
    CapabilityAssemblyStatus,
    CapabilityAssemblySubmitRequest,
    CapabilityAssemblyWriteConflict,
    assembly_request_sha256,
)
from gateway.capability_live_current_adapters import CapabilityResumeExecutionPort
from gateway.capability_live_pipeline import (
    CapabilityBusinessBenchmarkAuthority,
    CapabilityBusinessBenchmarkPort,
    CapabilityExecutionAuthority,
    CapabilityFactoryAuthority,
    CapabilityFactoryPort,
    CapabilityProjectionAuthority,
    CapabilityProjectionPort,
    CapabilityResumeAuthorization,
    CapabilityResumeExecutionRequest,
    PromotedCapabilityExecutionPort,
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
        factory: CapabilityFactoryPort,
        benchmark: CapabilityBusinessBenchmarkPort,
        resume: CapabilityResumeExecutionPort,
        execution: PromotedCapabilityExecutionPort,
        minibook_projection: CapabilityProjectionPort,
    ) -> None:
        if any(
            port is None
            for port in (
                repository,
                admission,
                factory,
                benchmark,
                resume,
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
        self._execution = execution
        self._projection = minibook_projection

    def submit(self, request: CapabilityAssemblySubmitRequest) -> CapabilityAssemblyCheckpoint:
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
                observed.repository_path != expected.repository_path
                or observed.input.input_id != expected.input_id
                or observed.input.conversation_pattern != expected.conversation_pattern
            ):
                raise CapabilityAssemblyConflict(
                    "input admission changed repository or conversation authority"
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
            raise CapabilityAssemblyIncomplete(
                "started slice has no terminal authority; effect replay is forbidden"
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

    async def _execute_input_stage(
        self,
        current: CapabilityAssemblyCheckpoint,
        *,
        ordinal: int,
        input_index: int,
        stage: CapabilityLiveStage,
    ) -> CapabilityAssemblyCheckpoint:
        item = current.items[input_index]
        request = CapabilityLiveStageRequest(
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
        authorization = await self._resume.authorize_resume(
            CapabilityResumeExecutionRequest(
                job_id=first.job_id,
                reservation_id=current.items[0].request.reservation.reservation_id,
                correlation_id=first.correlation_id,
                capability_id=first.capability_id,
                capability_version=first.capability_version,
                original_execution_command_id=first.execution_command_id,
                original_execution_result_id=first.execution_result_id,
            )
        )
        if authorization is None:
            raise CapabilityAssemblyIncomplete("resume authorization is unavailable")
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
            raise CapabilityAssemblyConflict("resume authorization changed release authority")
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
    ) -> CapabilityAssemblyCheckpoint:
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
        )
        self._append(completed, expected_revision=current.revision)
        return completed

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
        if (
            first.assembly_id != assembly_id
            or first.revision != 1
            or first.completed_slices != 0
            or first.status is not CapabilityAssemblyStatus.ADMITTED
        ):
            raise CapabilityAssemblyConflict("assembly history is not monotone")
        for expected_revision, record in enumerate(history, start=1):
            checkpoint = record.checkpoint
            CapabilityAssemblyController._require_checkpoint_shape(checkpoint)
            if (
                checkpoint.assembly_id != assembly_id
                or checkpoint.revision != expected_revision
                or checkpoint.request != first.request
                or checkpoint.request_sha256 != first.request_sha256
            ):
                raise CapabilityAssemblyConflict("assembly history is not monotone")
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
            )

    @staticmethod
    def _require_checkpoint_shape(checkpoint: CapabilityAssemblyCheckpoint) -> None:
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
    def _require_history_transition(
        prior: CapabilityAssemblyCheckpoint,
        current: CapabilityAssemblyCheckpoint,
        *,
        completed_delta: int,
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
            ):
                return
            if prior.active_advance is not None and current.active_advance is None:
                if (
                    prior.started_slice is not None
                    or current.started_slice is not None
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
        request: CapabilityLiveStageRequest,
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
        request: CapabilityLiveStageRequest,
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
    ) -> CapabilityLiveStageRequest:
        return CapabilityLiveStageRequest(
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
    payload = value.model_dump(mode="python", by_alias=True)
    payload.update(updates)
    return type(value).model_validate(payload)


__all__ = [
    "CapabilityAssemblyConflict",
    "CapabilityAssemblyController",
    "CapabilityAssemblyIncomplete",
    "CapabilityAssemblyNotFound",
]
