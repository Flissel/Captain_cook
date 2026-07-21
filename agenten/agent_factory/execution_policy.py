"""Frozen Captain policy for bounded Agent Factory execution."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FactoryExecutionMode(str, Enum):
    DEMO = "demo"
    RELEASE = "release"


class FactorySandboxMode(str, Enum):
    WORKSPACE_WRITE = "workspace_write"
    ISOLATED_DANGER_FULL_ACCESS = "isolated_danger_full_access"


class FactoryLiveCapability(str, Enum):
    MODEL_INVOKE = "model.invoke"
    DOCKER_RUN = "docker.run"
    CAPTAIN_TEST_DATABASE = "database.captain_test"
    BROWSER_USE = "browser.use"
    COMPUTER_USE = "computer.use"


class FactoryExecutionPolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.factory-execution-policy.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    mode: FactoryExecutionMode
    live_execution: bool
    max_cost_usd: Decimal
    max_runtime_seconds: int = Field(ge=1, le=86400, strict=True)
    required_live_runs: int = Field(ge=0, le=3, strict=True)
    allowed_models: tuple[str, ...] = ()
    live_capabilities: tuple[FactoryLiveCapability, ...] = ()
    sandbox_mode: FactorySandboxMode = FactorySandboxMode.WORKSPACE_WRITE

    @field_validator("max_cost_usd", mode="before")
    @classmethod
    def require_decimal_string(cls, value: object) -> Decimal:
        if isinstance(value, (bool, float)) or not isinstance(value, (str, Decimal)):
            raise TypeError("max_cost_usd must be a decimal string")
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("max_cost_usd must be finite") from exc
        if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
            raise ValueError(
                "max_cost_usd must be finite, non-negative, and use cents"
            )
        return amount

    @model_validator(mode="after")
    def require_mode_consistency(self) -> "FactoryExecutionPolicyV1":
        if not self.live_execution:
            if (
                self.max_cost_usd != 0
                or self.required_live_runs != 0
                or self.allowed_models
                or self.live_capabilities
            ):
                raise ValueError(
                    "offline execution requires zero live budget and no models"
                )
            return self
        if (
            self.max_cost_usd <= 0
            or not self.allowed_models
            or FactoryLiveCapability.MODEL_INVOKE not in self.live_capabilities
        ):
            raise ValueError(
                "live execution requires a positive budget and allowed models"
            )
        required = 1 if self.mode is FactoryExecutionMode.DEMO else 3
        if self.required_live_runs != required:
            required_label = "one" if required == 1 else str(required)
            raise ValueError(
                f"{self.mode.value} execution requires exactly {required_label} live run(s)"
            )
        if len(self.allowed_models) != len(set(self.allowed_models)):
            raise ValueError("allowed_models must not contain duplicates")
        if len(self.live_capabilities) != len(set(self.live_capabilities)):
            raise ValueError("live_capabilities must not contain duplicates")
        return self
