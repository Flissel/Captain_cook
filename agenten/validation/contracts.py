"""Versioned Captain contracts shared with external execution systems.

The Captain owns these models.  Executors receive a :class:`WorkBatch` and
report observations against its assertions.  Hidden evaluation inputs use a
separate :class:`HoldoutSuite`, so serialising a work batch can never disclose
them accidentally.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


CONTRACT_VERSION = "captain-work-batch/v2"
RUBRIC_VERSION = "captain-observation-rubric/v1"


class AssertionKind(str, Enum):
    OUTPUT_EQUALS = "output_equals"
    OUTPUT_CONTAINS = "output_contains"
    STATUS_EQUALS = "status_equals"
    SCHEMA_MATCHES = "schema_matches"
    SIDE_EFFECT_OBSERVED = "side_effect_observed"


class AcceptanceAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assertion_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    kind: AssertionKind
    path: Optional[str] = None
    expected: Any = None
    description: str = ""

    @model_validator(mode="after")
    def validate_observation_contract(self) -> "AcceptanceAssertion":
        comparisons = {
            AssertionKind.OUTPUT_EQUALS,
            AssertionKind.OUTPUT_CONTAINS,
            AssertionKind.STATUS_EQUALS,
            AssertionKind.SCHEMA_MATCHES,
        }
        if self.kind in comparisons and self.expected is None:
            raise ValueError(f"{self.kind.value} requires expected")
        if self.kind in {AssertionKind.OUTPUT_EQUALS, AssertionKind.OUTPUT_CONTAINS} and not self.path:
            raise ValueError(f"{self.kind.value} requires path")
        return self


class ExampleCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    input: Dict[str, Any]
    expected_observations: Dict[str, Any] = Field(default_factory=dict)


class WorkBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = CONTRACT_VERSION
    rubric_version: str = RUBRIC_VERSION
    batch_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    subtask_ids: List[str] = Field(min_length=1)
    target: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    runtime: str = Field(default="generic", min_length=1)
    runtime_version: str = Field(default="v1", min_length=1)
    interface_schema: str = Field(default="captain-artifact/v1", min_length=1)
    capability_tags: List[str] = Field(default_factory=list)
    depends_on: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    acceptance_criteria: List[AcceptanceAssertion]
    golden_cases: List[ExampleCase] = Field(default_factory=list)
    satisfied_by: Optional[str] = None

    @model_validator(mode="after")
    def validate_unique_references(self) -> "WorkBatch":
        for name, values in (
            ("subtask_ids", self.subtask_ids),
            ("capability_tags", self.capability_tags),
            ("depends_on", self.depends_on),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must not contain duplicates")
        if self.batch_id in self.depends_on:
            raise ValueError("a batch cannot depend on itself")
        return self


class HoldoutSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = CONTRACT_VERSION
    batch_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    cases: List[ExampleCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_case_ids(self) -> "HoldoutSuite":
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("holdout case ids must be unique")
        return self
