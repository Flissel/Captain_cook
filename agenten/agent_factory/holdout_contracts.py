"""Public, content-addressed references to private Captain holdouts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PrivateHoldoutRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["captain.private-holdout-ref.v1"] = "captain.private-holdout-ref.v1"
    holdout_id: str = Field(pattern=r"^holdout-[0-9a-f]{12}$")
    uri: str = Field(pattern=r"^holdout://holdout-[0-9a-f]{12}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
