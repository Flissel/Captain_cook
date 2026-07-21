from decimal import Decimal

import pytest
from pydantic import ValidationError

from agenten.agent_factory.execution_policy import FactoryExecutionPolicyV1


def release_payload() -> dict[str, object]:
    return {
        "schema": "captain.factory-execution-policy.v1",
        "mode": "release",
        "live_execution": True,
        "max_cost_usd": "5.00",
        "max_runtime_seconds": 900,
        "required_live_runs": 3,
        "allowed_models": ["approved-model-id"],
        "live_capabilities": ["model.invoke"],
        "sandbox_mode": "workspace_write",
    }


def offline_payload() -> dict[str, object]:
    return {
        "schema": "captain.factory-execution-policy.v1",
        "mode": "release",
        "live_execution": False,
        "max_cost_usd": "0.00",
        "max_runtime_seconds": 900,
        "required_live_runs": 0,
        "allowed_models": [],
        "live_capabilities": [],
        "sandbox_mode": "workspace_write",
    }


def test_release_policy_requires_three_runs_and_decimal_budget() -> None:
    policy = FactoryExecutionPolicyV1.model_validate(release_payload())

    assert policy.max_cost_usd == Decimal("5.00")


@pytest.mark.parametrize("value", [5.0, "NaN", "Infinity", "-1.00", "1.001"])
def test_execution_policy_rejects_float_or_invalid_cost(value: object) -> None:
    with pytest.raises((TypeError, ValueError, ValidationError)):
        FactoryExecutionPolicyV1.model_validate(
            release_payload() | {"max_cost_usd": value}
        )


def test_demo_and_offline_policies_fail_closed() -> None:
    with pytest.raises(ValidationError, match="demo.*one"):
        FactoryExecutionPolicyV1.model_validate(
            release_payload() | {"mode": "demo", "required_live_runs": 3}
        )

    with pytest.raises(ValidationError, match="offline"):
        FactoryExecutionPolicyV1.model_validate(
            release_payload()
            | {
                "live_execution": False,
                "max_cost_usd": "5.00",
                "required_live_runs": 3,
            }
        )


def test_offline_policy_has_no_live_authority() -> None:
    policy = FactoryExecutionPolicyV1.model_validate(offline_payload())

    assert policy.allowed_models == ()
    assert policy.live_capabilities == ()


@pytest.mark.parametrize(
    ("value", "payload"),
    [
        (0, offline_payload()),
        ("false", offline_payload()),
        (1, release_payload()),
        ("true", release_payload()),
    ],
)
def test_execution_policy_rejects_coerced_live_execution(
    value: object, payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        FactoryExecutionPolicyV1.model_validate(payload | {"live_execution": value})


def test_policy_rejects_duplicate_live_authority_and_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        FactoryExecutionPolicyV1.model_validate(
            release_payload() | {"allowed_models": ["approved-model-id"] * 2}
        )

    with pytest.raises(ValidationError):
        FactoryExecutionPolicyV1.model_validate(release_payload() | {"extra": True})
