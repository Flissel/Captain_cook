"""Per-creation conservative provider budget for the Minibook swarm."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterator


class LlmBudgetExceeded(RuntimeError):
    """Raised before a provider call could exceed Captain's cost ceiling."""


@dataclass
class LlmCostBudget:
    max_usd: Decimal
    model: str
    reserved_usd: float = 0.0


_ACTIVE_BUDGET: ContextVar[LlmCostBudget | None] = ContextVar(
    "minibook_llm_cost_budget",
    default=None,
)


def _prices(model: str) -> tuple[Decimal, Decimal]:
    if model == "gpt-4o-mini" or model.startswith("gpt-4o-mini-"):
        return Decimal("0.15"), Decimal("0.60")
    raise LlmBudgetExceeded(
        "provider pricing is not attested for the configured Minibook model"
    )


@contextmanager
def llm_cost_budget(*, max_usd: float | Decimal, model: str) -> Iterator[LlmCostBudget]:
    maximum = Decimal(str(max_usd))
    if maximum <= 0:
        raise ValueError("LLM cost budget must be positive")
    _prices(model)
    budget = LlmCostBudget(max_usd=maximum, model=model)
    token = _ACTIVE_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _ACTIVE_BUDGET.reset(token)


def reserve_openai_chat_completion(
    *,
    payload: dict[str, Any],
    max_output_tokens: int,
) -> float:
    """Reserve a conservative upper bound before issuing one OpenAI request.

    UTF-8 bytes upper-bound ordinary text-token counts, while the declared
    output limit is charged in full. Reservations are intentionally not
    refunded, so retries and tool turns remain inside the same hard ceiling.
    """

    budget = _ACTIVE_BUDGET.get()
    if budget is None:
        return 0.0
    if max_output_tokens < 1:
        raise ValueError("max_output_tokens must be positive")
    input_price, output_price = _prices(budget.model)
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    reservation = (
        Decimal(len(serialized)) * input_price
        + Decimal(max_output_tokens) * output_price
    ) / Decimal(1_000_000)
    next_total = Decimal(str(budget.reserved_usd)) + reservation
    if next_total > budget.max_usd:
        raise LlmBudgetExceeded(
            "Minibook LLM budget would be exceeded before provider call"
        )
    budget.reserved_usd = float(next_total)
    return float(reservation)
