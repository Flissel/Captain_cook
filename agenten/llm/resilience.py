"""Bounded timeout and retry policy for Captain-owned LLM stages."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TypeVar

import openai


T = TypeVar("T")


class LlmStage(str, Enum):
    DECOMPOSE = "decompose"
    ALIGN = "align"
    ENRICH = "enrich"


class LlmStageError(RuntimeError):
    """Typed failure metadata for one bounded LLM stage."""

    def __init__(
        self,
        stage: LlmStage,
        attempts: int,
        reason: str,
        *,
        detail: str | None = None,
    ) -> None:
        self.stage = stage
        self.attempts = attempts
        self.reason = reason
        self.detail = detail
        message = f"{stage.value} LLM stage failed after {attempts} attempts: {reason}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class LlmTimeoutError(LlmStageError):
    """Local per-attempt timeouts exhausted the stage budget."""

    def __init__(self, stage: LlmStage, attempts: int) -> None:
        super().__init__(stage, attempts, "timeout")


class LlmSchemaError(LlmStageError):
    """A structured LLM adapter returned missing or invalid content."""

    def __init__(
        self,
        stage: LlmStage,
        detail: str,
        *,
        attempts: int = 1,
    ) -> None:
        super().__init__(stage, attempts, "schema", detail=detail)


_TRANSIENT_PROVIDER_ERRORS = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
)


async def run_llm_stage(
    stage: LlmStage,
    operation: Callable[[], Awaitable[T]],
    *,
    timeout_seconds: float = 30.0,
    max_attempts: int = 2,
) -> T:
    """Run one LLM operation with one explicit bounded retry owner."""

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and greater than zero")
    if max_attempts < 1:
        raise ValueError("max_attempts must be greater than zero")

    for attempt in range(1, max_attempts + 1):
        try:
            return await asyncio.wait_for(operation(), timeout=timeout_seconds)
        except LlmSchemaError as exc:
            if exc.stage is stage and exc.attempts == attempt:
                raise
            raise LlmSchemaError(
                stage,
                exc.detail or "invalid structured output",
                attempts=attempt,
            ) from exc
        except TimeoutError as exc:
            if attempt == max_attempts:
                raise LlmTimeoutError(stage, attempt) from exc
        except _TRANSIENT_PROVIDER_ERRORS as exc:
            if attempt == max_attempts:
                raise LlmStageError(stage, attempt, "provider") from exc

    raise AssertionError("unreachable")
