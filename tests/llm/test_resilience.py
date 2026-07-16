import asyncio
import inspect

import httpx
import openai
import pytest

from agenten.llm.resilience import (
    LlmSchemaError,
    LlmStage,
    LlmStageError,
    LlmTimeoutError,
    run_llm_stage,
)
from agenten.planning.alignment import AlignmentError
from agenten.planning.policy import PlanningPolicyError


def _provider_errors() -> tuple[Exception, ...]:
    request = httpx.Request("POST", "https://provider.invalid/v1/chat")
    return (
        openai.APIConnectionError(request=request),
        openai.APITimeoutError(request),
        openai.RateLimitError(
            "rate limited",
            response=httpx.Response(429, request=request),
            body=None,
        ),
        openai.InternalServerError(
            "provider unavailable",
            response=httpx.Response(500, request=request),
            body=None,
        ),
    )


def test_resilience_policy_defaults_are_bounded() -> None:
    parameters = inspect.signature(run_llm_stage).parameters

    assert parameters["timeout_seconds"].default == 30.0
    assert parameters["max_attempts"].default == 2


@pytest.mark.parametrize(
    ("timeout_seconds", "max_attempts"),
    ((0.0, 2), (-1.0, 2), (1.0, 0), (1.0, -1)),
)
@pytest.mark.asyncio
async def test_invalid_policy_is_rejected_before_invocation(
    timeout_seconds: float,
    max_attempts: int,
) -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        return "unexpected"

    with pytest.raises(ValueError):
        await run_llm_stage(
            LlmStage.DECOMPOSE,
            operation,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )

    assert calls == 0


@pytest.mark.parametrize(
    "timeout_seconds",
    (float("inf"), float("-inf"), float("nan")),
)
@pytest.mark.asyncio
async def test_non_finite_timeout_is_rejected_before_invocation(
    timeout_seconds: float,
) -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        return "unexpected"

    with pytest.raises(ValueError, match="timeout_seconds"):
        await run_llm_stage(
            LlmStage.DECOMPOSE,
            operation,
            timeout_seconds=timeout_seconds,
        )

    assert calls == 0


@pytest.mark.asyncio
async def test_stage_timeout_cleans_up_each_attempt_and_preserves_cause() -> None:
    calls = 0
    active = 0
    cleanups = 0

    async def blocked() -> str:
        nonlocal active, calls, cleanups
        calls += 1
        active += 1
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
            cleanups += 1

    with pytest.raises(LlmTimeoutError) as failure:
        await run_llm_stage(
            LlmStage.ENRICH,
            blocked,
            timeout_seconds=0.01,
            max_attempts=2,
        )

    assert calls == 2
    assert active == 0
    assert cleanups == 2
    assert failure.value.stage is LlmStage.ENRICH
    assert failure.value.attempts == 2
    assert failure.value.reason == "timeout"
    assert isinstance(failure.value.__cause__, TimeoutError)


@pytest.mark.parametrize("provider_error", _provider_errors())
@pytest.mark.asyncio
async def test_transient_provider_errors_retry_then_preserve_final_cause(
    provider_error: Exception,
) -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise provider_error

    with pytest.raises(LlmStageError) as failure:
        await run_llm_stage(
            LlmStage.ALIGN,
            operation,
            timeout_seconds=1.0,
            max_attempts=2,
        )

    assert type(failure.value) is LlmStageError
    assert calls == 2
    assert failure.value.stage is LlmStage.ALIGN
    assert failure.value.attempts == 2
    assert failure.value.reason == "provider"
    assert failure.value.__cause__ is provider_error


@pytest.mark.asyncio
async def test_schema_error_is_not_retried() -> None:
    calls = 0
    schema_error = LlmSchemaError(LlmStage.ALIGN, "invalid structured output")

    async def invalid() -> str:
        nonlocal calls
        calls += 1
        raise schema_error

    with pytest.raises(LlmSchemaError) as failure:
        await run_llm_stage(LlmStage.ALIGN, invalid)

    assert calls == 1
    assert failure.value is schema_error
    assert failure.value.stage is LlmStage.ALIGN
    assert failure.value.attempts == 1


@pytest.mark.asyncio
async def test_schema_error_after_transient_retry_reports_current_attempt() -> None:
    calls = 0
    request = httpx.Request("POST", "https://provider.invalid/v1/chat")
    transient_error = openai.APIConnectionError(request=request)
    schema_error = LlmSchemaError(LlmStage.ALIGN, "invalid structured output")

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise transient_error
        raise schema_error

    with pytest.raises(LlmSchemaError) as failure:
        await run_llm_stage(
            LlmStage.ALIGN,
            operation,
            timeout_seconds=1.0,
            max_attempts=2,
        )

    assert calls == 2
    assert failure.value.stage is LlmStage.ALIGN
    assert failure.value.attempts == 2
    assert failure.value.reason == "schema"
    assert failure.value.detail == "invalid structured output"
    assert failure.value.__cause__ is schema_error


@pytest.mark.asyncio
async def test_non_retryable_api_status_error_propagates_natively() -> None:
    calls = 0
    request = httpx.Request("POST", "https://provider.invalid/v1/chat")
    provider_error = openai.BadRequestError(
        "invalid request",
        response=httpx.Response(400, request=request),
        body=None,
    )

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise provider_error

    with pytest.raises(openai.BadRequestError) as failure:
        await run_llm_stage(LlmStage.DECOMPOSE, operation)

    assert calls == 1
    assert failure.value is provider_error


@pytest.mark.parametrize(
    "deterministic_error",
    (
        ValueError("deterministic adapter failure"),
        AlignmentError("invalid alignment"),
        PlanningPolicyError("invalid enrichment policy"),
    ),
)
@pytest.mark.asyncio
async def test_deterministic_errors_are_not_retried_or_retyped(
    deterministic_error: Exception,
) -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise deterministic_error

    with pytest.raises(type(deterministic_error)) as failure:
        await run_llm_stage(LlmStage.ENRICH, operation)

    assert calls == 1
    assert failure.value is deterministic_error


@pytest.mark.asyncio
async def test_external_cancellation_propagates_after_operation_cleanup() -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def operation() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    task = asyncio.create_task(
        run_llm_stage(
            LlmStage.DECOMPOSE,
            operation,
            timeout_seconds=30.0,
            max_attempts=2,
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert cleaned.is_set()
