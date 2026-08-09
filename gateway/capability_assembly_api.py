"""Captain-only HTTP surface for the injected capability assembly controller."""

from __future__ import annotations

from collections.abc import Callable
from inspect import Parameter, iscoroutinefunction, signature
from typing import Literal, Protocol, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agenten.agent_factory.capability_live_runner import CapabilityLiveDemoEvidence
from gateway.auth import GatewayRole, require_captain
from gateway.capability_assembly_contracts import (
    CapabilityAssemblyAdvanceReceipt,
    CapabilityAssemblyAdvanceRequest,
    CapabilityAssemblyCheckpoint,
    CapabilityAssemblySubmitRequest,
)
from gateway.capability_assembly_controller import (
    CapabilityAssemblyConflict,
    CapabilityAssemblyIncomplete,
    CapabilityAssemblyNotFound,
)


class CapabilityAssemblyControllerPort(Protocol):
    def submit(
        self,
        request: CapabilityAssemblySubmitRequest,
    ) -> CapabilityAssemblyCheckpoint: ...

    def projection(self, assembly_id: UUID) -> CapabilityAssemblyCheckpoint: ...

    async def advance(
        self,
        request: CapabilityAssemblyAdvanceRequest,
    ) -> CapabilityAssemblyAdvanceReceipt: ...

    def final_evidence(self, assembly_id: UUID) -> CapabilityLiveDemoEvidence: ...


CapabilityAssemblyControllerFactory = Callable[[], CapabilityAssemblyControllerPort]
ContractT = TypeVar("ContractT", bound=BaseModel)


class CapabilityAssemblyApiHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["captain.capability-assembly-api-health.v1"] = Field(
        default="captain.capability-assembly-api-health.v1",
        alias="schema",
        serialization_alias="schema",
    )
    status: Literal["ready"] = "ready"
    composition: Literal["injected"] = "injected"


def unavailable_capability_assembly_controller() -> CapabilityAssemblyControllerPort:
    """Fail closed until a production composition explicitly injects D4."""

    raise RuntimeError("capability assembly controller is not configured")


def _translate_error(error: Exception) -> HTTPException:
    if isinstance(error, CapabilityAssemblyNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="capability assembly not found",
        )
    if isinstance(error, CapabilityAssemblyConflict):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="capability assembly conflict",
        )
    if isinstance(error, CapabilityAssemblyIncomplete):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="capability assembly incomplete",
        )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="capability assembly service unavailable",
    )


def _invalid_request() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="invalid capability assembly request",
    )


async def _validated_body(
    request: Request,
    model_type: type[ContractT],
) -> ContractT:
    try:
        return model_type.model_validate_json(await request.body())
    except (ValidationError, ValueError, TypeError):
        raise _invalid_request() from None


def _accepts_exactly_one_argument(method: object) -> bool:
    if not callable(method):
        return False
    try:
        method_signature = signature(method)
    except (TypeError, ValueError):
        return False
    parameters = tuple(method_signature.parameters.values())
    if len(parameters) != 1:
        return False
    parameter = parameters[0]
    if (
        parameter.kind
        not in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
        or parameter.default is not Parameter.empty
    ):
        return False
    try:
        method_signature.bind(object())
    except TypeError:
        return False
    return True


def _validated_controller(
    controller_factory: CapabilityAssemblyControllerFactory,
) -> CapabilityAssemblyControllerPort | None:
    try:
        controller = controller_factory()
        for method_name in ("submit", "projection", "final_evidence"):
            method = getattr(controller, method_name, None)
            if (
                not _accepts_exactly_one_argument(method)
                or iscoroutinefunction(method)
            ):
                return None
        advance = getattr(controller, "advance", None)
        if (
            not _accepts_exactly_one_argument(advance)
            or not iscoroutinefunction(advance)
        ):
            return None
        return controller
    except Exception:
        return None


def create_capability_assembly_router(
    controller_factory: CapabilityAssemblyControllerFactory,
) -> APIRouter:
    """Create a transport-only router around an explicitly bound controller."""

    router = APIRouter(prefix="/v1/capability-assemblies")
    controller = _validated_controller(controller_factory)
    ready_health = CapabilityAssemblyApiHealth()

    def get_controller() -> CapabilityAssemblyControllerPort:
        if controller is None:
            raise _translate_error(
                RuntimeError("capability assembly controller is unavailable")
            ) from None
        return controller

    @router.get("/health", response_model=CapabilityAssemblyApiHealth)
    async def health(
        _: GatewayRole = Depends(require_captain),
    ) -> CapabilityAssemblyApiHealth:
        if controller is None:
            raise _translate_error(
                RuntimeError("capability assembly controller is unavailable")
            ) from None
        return ready_health

    @router.post("", response_model=CapabilityAssemblyCheckpoint)
    async def submit(
        http_request: Request,
        _: GatewayRole = Depends(require_captain),
        controller: CapabilityAssemblyControllerPort = Depends(get_controller),
    ) -> CapabilityAssemblyCheckpoint:
        request = await _validated_body(
            http_request,
            CapabilityAssemblySubmitRequest,
        )
        try:
            return controller.submit(request)
        except Exception as exc:
            raise _translate_error(exc) from None

    @router.get("/{assembly_id}", response_model=CapabilityAssemblyCheckpoint)
    async def projection(
        assembly_id: UUID,
        _: GatewayRole = Depends(require_captain),
        controller: CapabilityAssemblyControllerPort = Depends(get_controller),
    ) -> CapabilityAssemblyCheckpoint:
        try:
            return controller.projection(assembly_id)
        except Exception as exc:
            raise _translate_error(exc) from None

    @router.post(
        "/{assembly_id}/advances",
        response_model=CapabilityAssemblyAdvanceReceipt,
    )
    async def advance(
        assembly_id: UUID,
        http_request: Request,
        _: GatewayRole = Depends(require_captain),
        controller: CapabilityAssemblyControllerPort = Depends(get_controller),
    ) -> CapabilityAssemblyAdvanceReceipt:
        request = await _validated_body(
            http_request,
            CapabilityAssemblyAdvanceRequest,
        )
        if request.assembly_id != assembly_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="capability assembly conflict",
            )
        if request.maximum_slices != 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="capability assembly advance requires one slice",
            )
        try:
            return await controller.advance(request)
        except Exception as exc:
            raise _translate_error(exc) from None

    @router.get(
        "/{assembly_id}/final-evidence",
        response_model=CapabilityLiveDemoEvidence,
    )
    async def final_evidence(
        assembly_id: UUID,
        _: GatewayRole = Depends(require_captain),
        controller: CapabilityAssemblyControllerPort = Depends(get_controller),
    ) -> CapabilityLiveDemoEvidence:
        try:
            return controller.final_evidence(assembly_id)
        except Exception as exc:
            raise _translate_error(exc) from None

    return router
