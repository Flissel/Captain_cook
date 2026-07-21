from __future__ import annotations

import asyncio

import pytest

from minibook.swarm.cost_budget import (
    LlmBudgetExceeded,
    llm_cost_budget,
    reserve_openai_chat_completion,
)


def test_budget_reserves_conservative_gpt_4o_mini_cost_before_call() -> None:
    with llm_cost_budget(max_usd=0.001, model="gpt-4o-mini") as budget:
        reservation = reserve_openai_chat_completion(
            payload={"messages": [{"role": "user", "content": "build it"}]},
            max_output_tokens=1_000,
        )

        assert reservation > 0
        assert budget.reserved_usd == reservation
        with pytest.raises(LlmBudgetExceeded, match="before provider call"):
            reserve_openai_chat_completion(
                payload={"messages": [{"role": "user", "content": "x" * 5_000}]},
                max_output_tokens=1_000,
            )


def test_budget_rejects_model_without_attested_pricing() -> None:
    with pytest.raises(LlmBudgetExceeded, match="pricing is not attested"):
        with llm_cost_budget(max_usd=1.0, model="unknown-provider-model"):
            pass


def test_budget_is_isolated_per_async_creation_job() -> None:
    async def reserve(amount: int) -> float:
        with llm_cost_budget(max_usd=0.01, model="gpt-4o-mini") as budget:
            reserve_openai_chat_completion(
                payload={"messages": [{"role": "user", "content": "x" * amount}]},
                max_output_tokens=500,
            )
            await asyncio.sleep(0)
            return budget.reserved_usd

    async def run_both() -> tuple[float, float]:
        first, second = await asyncio.gather(reserve(100), reserve(10_000))
        return first, second

    first, second = asyncio.run(run_both())

    assert first != second
