"""Planning-only command line boundary for AgentFarm input evaluation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections.abc import AsyncGenerator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from autogen_core import CancellationToken, FunctionCall
from autogen_core.models import (
    ChatCompletionClient,
    CreateResult,
    LLMMessage,
    ModelCapabilities,
    ModelFamily,
    ModelInfo,
    RequestUsage,
)
from autogen_core.tools import Tool, ToolSchema
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_ext.models.replay import ReplayChatCompletionClient
from pydantic import BaseModel, ValidationError

from .models import EvaluationStatus, EvaluationTelemetry, ProviderCallReservation
from .service import AgentFarmEvaluationService
from .source import EvaluationSourceError, load_evaluation_source
from .store import EvaluationConflictError, JsonEvaluationStore
from .tools import EvaluationToolService


DEFAULT_MODEL = "gpt-5.6"
PROMPT_VERSION = "agentfarm-evaluation-v1"

_TOOL_ARGUMENT_KEYS = {
    "read_source_block": frozenset({"run_id", "block_id"}),
    "stage_component_inventory": frozenset(
        {"run_id", "inventory_id", "source_citations", "components"}
    ),
    "stage_component_plan": frozenset({"run_id", "candidate"}),
    "record_qa_review": frozenset({"run_id", "review"}),
}


class EvaluationCallBudgetExceeded(RuntimeError):
    """A provider call would exceed Captain's immutable live-call budget."""


class EvaluationSourceDigestMismatch(ValueError):
    """A configured immutable source differs from its approved digest."""


class UsageTrackingChatCompletionClient(ChatCompletionClient):
    """Delegate model calls while enforcing and reporting one raw-call budget."""

    def __init__(
        self,
        inner: ChatCompletionClient,
        *,
        model_identifier: str,
        store: JsonEvaluationStore,
        run_id: str,
    ) -> None:
        self._inner = inner
        self._model_identifier = model_identifier
        self._store = store
        self._run_id = run_id

    async def create(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = (),
        tool_choice: Tool | Literal["auto", "required", "none"] = "auto",
        json_output: bool | type[BaseModel] | None = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: CancellationToken | None = None,
    ) -> CreateResult:
        reservation = await self._reserve_call()
        result = await self._inner.create(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            json_output=json_output,
            extra_create_args=extra_create_args,
            cancellation_token=cancellation_token,
        )
        await self._store.observe_provider_call(
            self._run_id,
            call_index=reservation.call_index,
            finish_reason=str(result.finish_reason),
            tool_calls=_structural_tool_calls(result),
        )
        await self._store.complete_provider_call(
            self._run_id,
            call_index=reservation.call_index,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
        )
        return result

    def create_stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = (),
        tool_choice: Tool | Literal["auto", "required", "none"] = "auto",
        json_output: bool | type[BaseModel] | None = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: CancellationToken | None = None,
    ) -> AsyncGenerator[str | CreateResult, None]:
        async def tracked_stream() -> AsyncGenerator[str | CreateResult, None]:
            reservation = await self._reserve_call()
            async for item in self._inner.create_stream(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                json_output=json_output,
                extra_create_args=extra_create_args,
                cancellation_token=cancellation_token,
            ):
                if isinstance(item, CreateResult):
                    await self._store.observe_provider_call(
                        self._run_id,
                        call_index=reservation.call_index,
                        finish_reason=str(item.finish_reason),
                        tool_calls=_structural_tool_calls(item),
                    )
                    await self._store.complete_provider_call(
                        self._run_id,
                        call_index=reservation.call_index,
                        prompt_tokens=item.usage.prompt_tokens,
                        completion_tokens=item.usage.completion_tokens,
                    )
                yield item

        return tracked_stream()

    async def close(self) -> None:
        await self._inner.close()

    def actual_usage(self) -> RequestUsage:
        return self._inner.actual_usage()

    def total_usage(self) -> RequestUsage:
        return self._inner.total_usage()

    def count_tokens(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = (),
    ) -> int:
        return self._inner.count_tokens(messages, tools=tools)

    def remaining_tokens(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = (),
    ) -> int:
        return self._inner.remaining_tokens(messages, tools=tools)

    @property
    def capabilities(self) -> ModelCapabilities:  # type: ignore[override]
        return self._inner.capabilities

    @property
    def model_info(self) -> ModelInfo:
        return self._inner.model_info

    def evaluation_telemetry(self) -> EvaluationTelemetry:
        return self._store.provider_telemetry(
            self._run_id,
            prompt_version=PROMPT_VERSION,
        )

    async def _reserve_call(self) -> ProviderCallReservation:
        try:
            return await self._store.reserve_provider_call(
                self._run_id,
                model_identifier=self._model_identifier,
            )
        except EvaluationConflictError as exc:
            raise EvaluationCallBudgetExceeded(
                "evaluation provider budget is exhausted"
            ) from exc


def build_evaluation_model_client(
    *,
    model: str,
    api_key: str | None,
) -> ChatCompletionClient:
    """Build the real tool-capable provider with concurrent tool calls disabled."""

    kwargs: dict[str, object] = {
        "model": model,
        "max_retries": 0,
        "parallel_tool_calls": False,
        "reasoning_effort": "none",
        "model_info": ModelInfo(
            vision=True,
            function_calling=True,
            json_output=True,
            family=ModelFamily.GPT_5,
            structured_output=True,
        ),
    }
    if api_key is not None:
        kwargs["api_key"] = api_key
    return OpenAIChatCompletionClient(**kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentfarm-evaluate",
        description="Create planning-only Captain evaluation evidence from AgentFarm Markdown.",
    )
    parser.add_argument("input", type=Path, help="immutable AgentFarm Markdown input")
    parser.add_argument("--source-reference", required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/evaluations"))
    parser.add_argument("--run-id")
    parser.add_argument("--model")
    parser.add_argument("--max-components", type=_positive_int, default=1)
    parser.add_argument("--max-rounds", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--max-calls", type=_positive_int, default=8)
    return parser


async def async_main(
    argv: Sequence[str] | None = None,
    *,
    model_client: ChatCompletionClient | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        source = load_evaluation_source(
            args.input,
            source_reference=args.source_reference,
            max_block_bytes=12_000,
        )
    except (EvaluationSourceError, OSError, ValidationError, UnicodeError, ValueError):
        _print_summary({"error": "invalid_source", "status": "failed"})
        return 1

    model_identifier = args.model or os.environ.get("CAPTAIN_MODEL") or DEFAULT_MODEL
    run_id = args.run_id or f"agentfarm-{source.sha256[:12]}"
    store = JsonEvaluationStore(args.output)
    owned_client = model_client is None
    try:
        inner = model_client or build_evaluation_model_client(
            model=model_identifier,
            api_key=os.environ.get("OPENAI_API_KEY"),
        )
        tracked_client = UsageTrackingChatCompletionClient(
            inner,
            model_identifier=model_identifier,
            store=store,
            run_id=run_id,
        )
        tools = EvaluationToolService(store)
        service = AgentFarmEvaluationService(
            model_client=tracked_client,
            tools=tools,
            store=store,
            source=source,
            idempotency_key=source.sha256,
            max_components=args.max_components,
            max_rounds=args.max_rounds,
            max_calls=args.max_calls,
            summary_model_client=_build_summary_model_client(args.max_calls),
            telemetry=tracked_client.evaluation_telemetry,
        )
        manifest = await service.run(run_id)
        _print_summary(
            {
                "artifact_reference": f"{manifest.run_id}/evaluation.md",
                "call_count": manifest.call_count,
                "model_identifier": manifest.model_identifier,
                "run_id": manifest.run_id,
                "status": manifest.status.value,
                "token_total": manifest.token_total,
            }
        )
        return 0 if manifest.status is EvaluationStatus.ACCEPTED else 2
    except Exception:
        _print_summary({"error": "evaluation_failed", "status": "failed"})
        return 1
    finally:
        if owned_client and "tracked_client" in locals():
            await tracked_client.close()


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


def verify_source_digest(path: Path, *, expected_sha256: str) -> Path:
    """Hard-fail a configured unreadable or changed immutable source."""

    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise EvaluationSourceDigestMismatch(
            "configured source digest cannot be verified"
        ) from exc
    if digest != expected_sha256:
        raise EvaluationSourceDigestMismatch(
            "configured source digest does not match the approved source"
        )
    return path


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _build_summary_model_client(max_calls: int) -> ChatCompletionClient:
    return ReplayChatCompletionClient(
        ["Non-authoritative planning slice summary."] * max_calls,
        model_info=ModelInfo(
            vision=False,
            function_calling=False,
            json_output=False,
            family=ModelFamily.UNKNOWN,
            structured_output=False,
        ),
    )


def _print_summary(summary: Mapping[str, object]) -> None:
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


def _structural_tool_calls(result: CreateResult) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Keep only approved tool names and public top-level argument keys."""

    if not isinstance(result.content, list):
        return ()
    observations: list[tuple[str, tuple[str, ...]]] = []
    for item in result.content:
        if not isinstance(item, FunctionCall):
            continue
        allowed_keys = _TOOL_ARGUMENT_KEYS.get(item.name)
        if allowed_keys is None:
            observations.append(("unknown", ()))
            continue
        try:
            arguments = json.loads(item.arguments)
        except (TypeError, json.JSONDecodeError):
            arguments = None
        keys = (
            tuple(sorted(key for key in arguments if isinstance(key, str) and key in allowed_keys))
            if isinstance(arguments, dict)
            else ()
        )
        observations.append((item.name, keys))
    return tuple(observations)


if __name__ == "__main__":
    raise SystemExit(main())
