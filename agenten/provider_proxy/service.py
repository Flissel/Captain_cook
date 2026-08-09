"""Cost-bounded orchestration for an injected OpenAI Responses transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from collections.abc import AsyncIterator
from typing import Callable, Mapping, Protocol
from uuid import UUID, uuid4, uuid5

from agenten.agent_runtime.contracts import (
    RuntimeProviderCostSettlementV1,
    RuntimeProviderUsageReceiptV1,
    RuntimeUsagePricingSnapshotV1,
    canonical_json_bytes,
)

from .contracts import OpenAIPriceCardV1, ProviderBudgetContextV1, RuntimeProviderPolicyV1
from .repository import ReservationRejected, SQLiteProviderBudgetRepository


class BudgetRejected(ValueError):
    pass


class ProviderUsageUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderReply:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class ProviderStreamReply:
    status_code: int
    headers: Mapping[str, str]
    chunks: AsyncIterator[bytes]


class ProviderClient(Protocol):
    async def count_input_tokens(self, payload: dict[str, object]) -> int: ...
    async def create_response(self, payload: dict[str, object]) -> ProviderReply: ...
    async def read_response(self, response_id: str, query: str) -> ProviderReply: ...
    async def cancel_response(self, response_id: str) -> ProviderReply: ...
    async def open_response_stream(self, payload: dict[str, object]) -> ProviderStreamReply: ...


class GatewaySettlementClient(Protocol):
    async def require_active_provider_authority(
        self, context: ProviderBudgetContextV1
    ) -> ProviderBudgetContextV1: ...

    async def record_provider_settlement(self, payload: dict[str, object]) -> None: ...


_ALLOWED_NONPAID_TOOLS = {"function", "custom", "local_shell", "shell", "apply_patch"}


class ProviderBudgetProxy:
    def __init__(
        self,
        *,
        context: ProviderBudgetContextV1,
        policy: RuntimeProviderPolicyV1,
        price_card: OpenAIPriceCardV1,
        repository: SQLiteProviderBudgetRepository,
        cas_root: Path,
        provider: ProviderClient,
        gateway: GatewaySettlementClient,
        clock: Callable[[], datetime],
    ) -> None:
        if (
            context.policy_sha256 != policy.sha256
            or context.price_card_sha256 != price_card.sha256
        ):
            raise ValueError("provider context policy binding is invalid")
        self.context = context
        self.policy = policy
        self.price_card = price_card
        self.repository = repository
        self.cas_root = cas_root.resolve()
        self.provider = provider
        self.gateway = gateway
        self.clock = clock
        self._finalize_lock = asyncio.Lock()

    async def _require_active_authority(self, now: datetime) -> None:
        try:
            self.context.require_active(now)
            authority = await self.gateway.require_active_provider_authority(
                self.context
            )
        except Exception as exc:
            raise BudgetRejected("provider authority is unavailable") from exc
        if authority != self.context:
            raise BudgetRejected("provider authority is unavailable")

    @property
    def pricing_snapshot(self) -> RuntimeUsagePricingSnapshotV1:
        return RuntimeUsagePricingSnapshotV1(
            schema="captain.runtime-usage-pricing-snapshot.v1",
            snapshot_id=self.price_card.snapshot_id,
            provider="openai",
            model=self.price_card.model,
            input_cost_per_million_usd=self.price_card.input_cost_per_million_usd,
            cached_input_cost_per_million_usd=self.price_card.cached_input_cost_per_million_usd,
            output_cost_per_million_usd=self.price_card.output_cost_per_million_usd,
            effective_at=self.price_card.effective_at,
            expires_at=self.price_card.expires_at,
        )

    async def create(self, payload: dict[str, object]) -> ProviderReply:
        now, call_id, reservation, outbound = await self._prepare(payload)
        try:
            reply = await self.provider.create_response(outbound)
        except Exception as exc:
            self.repository.settle(
                call_id=call_id,
                provider_response_id=f"ambiguous-{call_id}",
                consumed_usd=reservation,
                ambiguous=True,
            )
            raise ProviderUsageUnavailable(
                "provider effect outcome is ambiguous"
            ) from exc
        nonterminal_id = self._nonterminal_response_id(reply)
        if nonterminal_id is not None:
            self.repository.attach_response(
                call_id=call_id,
                response_id=nonterminal_id,
                started_at=now.isoformat(),
            )
            return reply
        try:
            response = self._terminal_response(reply)
            await self._settle_terminal(call_id, reservation, response, now)
        except ProviderUsageUnavailable:
            response_id = self._response_id(reply) or f"ambiguous-{call_id}"
            self.repository.settle(
                call_id=call_id,
                provider_response_id=response_id,
                consumed_usd=reservation,
                ambiguous=True,
            )
            raise
        return reply

    async def create_stream(self, payload: dict[str, object]) -> ProviderStreamReply:
        now, call_id, reservation, outbound = await self._prepare(payload)
        try:
            stream = await self.provider.open_response_stream(outbound)
        except Exception as exc:
            self.repository.settle(
                call_id=call_id,
                provider_response_id=f"ambiguous-{call_id}",
                consumed_usd=reservation,
                ambiguous=True,
            )
            raise ProviderUsageUnavailable(
                "provider stream outcome is ambiguous"
            ) from exc

        async def governed_chunks() -> AsyncIterator[bytes]:
            buffer = b""
            settled = False
            provider_response_id = f"ambiguous-{call_id}"
            try:
                async for chunk in stream.chunks:
                    buffer += chunk
                    while True:
                        crlf = buffer.find(b"\r\n\r\n")
                        lf = buffer.find(b"\n\n")
                        indexes = [item for item in (crlf, lf) if item >= 0]
                        if not indexes:
                            break
                        split_at = min(indexes)
                        separator = b"\r\n\r\n" if crlf == split_at else b"\n\n"
                        block = buffer[:split_at]
                        buffer = buffer[split_at + len(separator):]
                        terminal = self._terminal_sse_block(block)
                        if terminal is not None:
                            response_id = terminal.get("id")
                            if isinstance(response_id, str):
                                provider_response_id = response_id
                            try:
                                await self._settle_terminal(
                                    call_id, reservation, terminal, now
                                )
                            except ProviderUsageUnavailable:
                                self.repository.settle(
                                    call_id=call_id,
                                    provider_response_id=provider_response_id,
                                    consumed_usd=reservation,
                                    ambiguous=True,
                                )
                                settled = True
                                raise
                            settled = True
                        yield block + separator
                if buffer:
                    yield buffer
                if not settled:
                    raise ProviderUsageUnavailable(
                        "provider stream ended without terminal usage"
                    )
            finally:
                if not settled:
                    try:
                        self.repository.settle(
                            call_id=call_id,
                            provider_response_id=provider_response_id,
                            consumed_usd=reservation,
                            ambiguous=True,
                        )
                    except ReservationRejected:
                        pass

        return ProviderStreamReply(stream.status_code, stream.headers, governed_chunks())

    async def _prepare(
        self, payload: dict[str, object]
    ) -> tuple[datetime, str, Decimal, dict[str, object]]:
        now = self.clock()
        await self._require_active_authority(now)
        self.price_card.require_active(now)
        self.policy.require_safe()
        self._validate_request(payload)
        count_payload = {key: value for key, value in payload.items() if key not in {"stream", "background", "max_output_tokens"}}
        input_tokens = await self.provider.count_input_tokens(count_payload)
        now = self.clock()
        await self._require_active_authority(now)
        if input_tokens >= self.price_card.large_input_threshold_tokens:
            raise BudgetRejected(
                f"provider input must be below {self.price_card.large_input_threshold_tokens} tokens"
            )
        projection = self.repository.projection()
        ceiling = min(self.context.ceiling_usd, self.policy.maximum_run_cost_usd)
        remaining = ceiling - projection.consumed_usd - projection.active_reserved_usd
        worst_input = Decimal(input_tokens) * self.price_card.input_cost_per_million_usd * self.price_card.cache_write_multiplier / Decimal(1_000_000)
        derived_output = int(((remaining - worst_input) * Decimal(1_000_000) / self.price_card.output_cost_per_million_usd).to_integral_value(rounding=ROUND_FLOOR))
        requested_output = payload.get("max_output_tokens")
        if isinstance(requested_output, bool) or (requested_output is not None and not isinstance(requested_output, int)):
            raise BudgetRejected("max_output_tokens must be an integer")
        maximum_output = min(derived_output, self.policy.maximum_output_tokens_per_call, requested_output if isinstance(requested_output, int) else self.policy.maximum_output_tokens_per_call)
        if maximum_output < 1:
            raise BudgetRejected("provider budget cannot fund one output token")
        reservation = (
            worst_input
            + Decimal(maximum_output)
            * self.price_card.output_cost_per_million_usd
            / Decimal(1_000_000)
        ).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
        request_sha = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        call_id = str(uuid4())
        try:
            self.repository.reserve(call_id=call_id, request_sha256=request_sha, requested_usd=reservation, ceiling_usd=ceiling, maximum_calls=self.policy.maximum_provider_calls)
        except ReservationRejected as exc:
            raise BudgetRejected(str(exc)) from exc
        outbound = dict(payload)
        outbound["model"] = self.policy.model
        outbound["reasoning"] = {"effort": self.policy.reasoning_effort}
        outbound["max_output_tokens"] = maximum_output
        return now, call_id, reservation, outbound

    async def read(self, response_id: str, query: str = "") -> ProviderReply:
        await self._require_active_authority(self.clock())
        reply = await self.provider.read_response(response_id, query)
        active = self.repository.active_response(response_id)
        if active is None:
            return reply
        call_id, reservation, started_at = active
        try:
            terminal = self._terminal_response(reply)
            await self._settle_terminal(
                call_id,
                reservation,
                terminal,
                datetime.fromisoformat(started_at),
            )
        except ProviderUsageUnavailable:
            self.repository.settle(
                call_id=call_id,
                provider_response_id=response_id,
                consumed_usd=reservation,
                ambiguous=True,
            )
            raise
        return reply

    async def cancel(self, response_id: str) -> ProviderReply:
        await self._require_active_authority(self.clock())
        reply = await self.provider.cancel_response(response_id)
        active = self.repository.active_response(response_id)
        if active is not None:
            call_id, reservation, _ = active
            self.repository.settle(
                call_id=call_id,
                provider_response_id=response_id,
                consumed_usd=reservation,
                ambiguous=True,
            )
        cancellation = json.dumps(
            {
                "schema": "captain.provider-call-cancellation.v1",
                "provider_response_id": response_id,
                "status": "cancelled",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        digest = hashlib.sha256(cancellation).hexdigest()
        destination = self.cas_root / "cancellations" / digest[:2] / f"{digest}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.read_bytes() != cancellation:
            raise ProviderUsageUnavailable("provider cancellation CAS collision")
        destination.write_bytes(cancellation)
        self.repository.record_cancellation(
            response_id,
            reply.status_code,
            "application/json",
            cancellation,
        )
        return reply

    def _validate_request(self, payload: dict[str, object]) -> None:
        if payload.get("model") != self.policy.model:
            raise BudgetRejected("provider model is not authorized")
        reasoning = payload.get("reasoning")
        if not isinstance(reasoning, dict) or reasoning.get("effort") != "high":
            raise BudgetRejected("provider reasoning effort must be high")
        tools = payload.get("tools", [])
        if not isinstance(tools, list):
            raise BudgetRejected("provider tools must be a list")
        for tool in tools:
            if (
                not isinstance(tool, dict)
                or tool.get("type") not in _ALLOWED_NONPAID_TOOLS
            ):
                raise BudgetRejected("paid hosted tool is not authorized")

    def _terminal_response(self, reply: ProviderReply) -> dict[str, object]:
        content_type = reply.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            candidates: list[dict[str, object]] = []
            for line in reply.body.decode("utf-8").splitlines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "response.completed" and isinstance(event.get("response"), dict):
                    candidates.append(event["response"])
            if len(candidates) != 1:
                raise ProviderUsageUnavailable("provider terminal usage is unavailable")
            return candidates[0]
        try:
            value = json.loads(reply.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ProviderUsageUnavailable("provider terminal usage is unavailable") from None
        if not isinstance(value, dict) or value.get("status") != "completed":
            raise ProviderUsageUnavailable("provider terminal usage is unavailable")
        return value

    @staticmethod
    def _terminal_sse_block(block: bytes) -> dict[str, object] | None:
        data = b"\n".join(
            line[5:].lstrip()
            for line in block.replace(b"\r\n", b"\n").splitlines()
            if line.startswith(b"data:")
        )
        if not data or data == b"[DONE]":
            return None
        try:
            event = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if (
            isinstance(event, dict)
            and event.get("type") == "response.completed"
            and isinstance(event.get("response"), dict)
        ):
            return event["response"]
        return None

    async def _settle_terminal(
        self, call_id: str, reservation: Decimal, response: dict[str, object], started_at: datetime
    ) -> None:
        usage = response.get("usage")
        response_id = response.get("id")
        if not isinstance(usage, dict) or not isinstance(response_id, str) or not response_id:
            raise ProviderUsageUnavailable("provider terminal usage is unavailable")
        details = usage.get("input_tokens_details", {})
        if not isinstance(details, dict):
            raise ProviderUsageUnavailable("provider terminal usage is unavailable")
        counts = (usage.get("input_tokens"), details.get("cached_tokens", 0), usage.get("output_tokens"))
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
            raise ProviderUsageUnavailable("provider terminal usage is unavailable")
        input_units, cached_units, output_units = counts
        if cached_units > input_units:
            raise ProviderUsageUnavailable("provider terminal usage is unavailable")
        snapshot = self.pricing_snapshot
        actual = snapshot.cost(
            input_units=input_units,
            cached_input_units=cached_units,
            output_units=output_units,
        )
        if actual > reservation:
            raise ProviderUsageUnavailable("provider usage exceeds its reservation")
        ended_at = self.clock()
        if ended_at >= self.context.expires_at:
            raise ProviderUsageUnavailable("provider terminal usage window is invalid")
        call_receipt = {
            "schema": "captain.provider-call-usage.v1",
            "call_id": call_id,
            "provider_response_id": response_id,
            "input_units": input_units,
            "cached_input_units": cached_units,
            "output_units": output_units,
            "pricing_snapshot_id": snapshot.snapshot_id,
            "pricing_snapshot_sha256": snapshot.snapshot_sha256,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
        }
        encoded = json.dumps(
            call_receipt, sort_keys=True, separators=(",", ":")
        ).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        destination = self.cas_root / "calls" / digest[:2] / f"{digest}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.read_bytes() != encoded:
            raise ProviderUsageUnavailable("provider call receipt CAS collision")
        destination.write_bytes(encoded)
        self.repository.record_usage(
            call_id=call_id,
            provider_response_id=response_id,
            consumed_usd=actual,
            input_units=input_units,
            cached_input_units=cached_units,
            output_units=output_units,
            started_at=started_at.isoformat(),
            ended_at=ended_at.isoformat(),
        )

    async def finalize(
        self, *, command_id: UUID, result_id: UUID
    ) -> dict[str, object]:
        if command_id != self.context.command_id or result_id != self.context.result_id:
            raise BudgetRejected("provider finalization binding is invalid")
        await self._require_active_authority(self.clock())
        async with self._finalize_lock:
            try:
                (
                    input_units,
                    cached_units,
                    output_units,
                    started_text,
                    ended_text,
                    provider_ids,
                ) = self.repository.aggregate_usage()
            except ReservationRejected as exc:
                raise BudgetRejected(str(exc)) from exc
            snapshot = self.pricing_snapshot
            actual = snapshot.cost(
                input_units=input_units,
                cached_input_units=cached_units,
                output_units=output_units,
            )
            if actual > min(
                self.context.ceiling_usd, self.policy.maximum_run_cost_usd
            ):
                raise BudgetRejected("aggregate provider usage exceeds the run ceiling")
            provider_digest = hashlib.sha256(
                canonical_json_bytes(snapshot) + b"\0" + "\0".join(provider_ids).encode()
            ).hexdigest()
            receipt = RuntimeProviderUsageReceiptV1(
                schema="captain.runtime-provider-usage-receipt.v1",
                receipt_id=uuid5(self.context.command_id, "runtime-provider-usage"),
            request_id=self.context.command_id,
            command_id=self.context.original_command_id,
            result_id=self.context.result_id,
            reservation_id=self.context.reservation_id,
            job_id=self.context.job_id,
            run_id=self.context.run_id,
            input_id=self.context.input_id,
            correlation_id=self.context.correlation_id,
            capability_id=self.context.capability_id,
            capability_version=self.context.capability_version,
                session_id=self.context.session_id,
                provider="openai",
                model=self.policy.model,
                input_units=input_units,
                cached_input_units=cached_units,
                output_units=output_units,
                actual_cost_usd=actual,
                pricing_snapshot_id=snapshot.snapshot_id,
                pricing_snapshot_sha256=snapshot.snapshot_sha256,
                started_at=datetime.fromisoformat(started_text),
                ended_at=datetime.fromisoformat(ended_text),
            )
            settlement = RuntimeProviderCostSettlementV1(
                schema="captain.runtime-provider-cost-settlement.v1",
                settlement_id=uuid5(self.context.command_id, "provider-cost-settlement"),
                provider_call_id=f"aggregate-{provider_digest[:32]}",
                receipt=receipt,
                receipt_sha256=hashlib.sha256(canonical_json_bytes(receipt)).hexdigest(),
                settled_at=max(self.clock(), receipt.ended_at),
            )
            encoded = canonical_json_bytes(settlement)
            digest = hashlib.sha256(encoded).hexdigest()
            destination = self.cas_root / digest[:2] / f"{digest}.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.read_bytes() != encoded:
                raise ProviderUsageUnavailable("provider settlement CAS collision")
            destination.write_bytes(encoded)
            payload = settlement.model_dump(mode="json", by_alias=True)
            binding_sha = hashlib.sha256(
                canonical_json_bytes(settlement)
                + canonical_json_bytes(self.policy)
                + canonical_json_bytes(self.price_card)
            ).hexdigest()
            state = self.repository.claim_finalization(
                binding_sha, encoded.decode("utf-8")
            )
            if state != "settled":
                try:
                    await self.gateway.record_provider_settlement(payload)
                except Exception as exc:
                    raise ProviderUsageUnavailable(
                        "provider settlement gateway is unavailable"
                    ) from exc
                self.repository.complete_finalization(binding_sha)
            return payload

    @staticmethod
    def _response_id(reply: ProviderReply) -> str | None:
        try:
            value = json.loads(reply.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        response_id = value.get("id") if isinstance(value, dict) else None
        return response_id if isinstance(response_id, str) else None

    @staticmethod
    def _nonterminal_response_id(reply: ProviderReply) -> str | None:
        if "text/event-stream" in reply.headers.get("content-type", ""):
            return None
        try:
            value = json.loads(reply.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if (
            isinstance(value, dict)
            and value.get("status") in {"queued", "in_progress"}
            and isinstance(value.get("id"), str)
        ):
            return value["id"]
        return None
