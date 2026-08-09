"""Frozen contracts for canonical ``TO_BE_BUILT.md`` requests."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.agent_runtime.contracts import ArtifactRef, IDENTIFIER_PATTERN


ALIAS_PATTERN = r"^[A-Z][A-Z0-9_]*$"
BUSINESS_BENCHMARK_PREDICATE_KINDS = frozenset({
    "observation_equals", "observation_contains", "observation_absent",
})
RATIONALE_BENCHMARK_PREDICATE_KINDS = frozenset({"rationale_contains"})
SAFETY_BENCHMARK_PREDICATE_KINDS = frozenset({"forbidden_tool_absent"})
TOOL_BENCHMARK_PREDICATE_KINDS = frozenset({"tool_receipt"})
HANDOFF_BENCHMARK_PREDICATE_KINDS = frozenset({"handoff_event"})


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CredentialAlias(_FrozenContract):
    alias: str = Field(pattern=ALIAS_PATTERN)


class InputSection(_FrozenContract):
    heading: str = Field(min_length=1)
    heading_path: tuple[str, ...] = Field(min_length=1)
    markdown: str = Field(min_length=1)


class RealCaseRequirement(_FrozenContract):
    case_key: str = Field(pattern=IDENTIFIER_PATTERN)
    observable_setup: str = Field(min_length=1)
    observable_action: str = Field(min_length=1)
    observable_expected: str = Field(min_length=1)


class BenchmarkExpectedPredicate(_FrozenContract):
    predicate_key: str = Field(pattern=IDENTIFIER_PATTERN)
    kind: Literal[
        "observation_equals",
        "observation_contains",
        "observation_absent",
        "handoff_event",
        "tool_receipt",
        "rationale_contains",
        "forbidden_tool_absent",
    ]
    observed_key: str = Field(pattern=IDENTIFIER_PATTERN)
    expected_value: str | bool

    @model_validator(mode="after")
    def validate_expected_value_semantics(self) -> "BenchmarkExpectedPredicate":
        if self.kind in {
            "observation_absent", "tool_receipt", "forbidden_tool_absent",
        } and self.expected_value is not True:
            raise ValueError(f"{self.kind} predicate must require true")
        if self.kind in {
            "observation_contains", "handoff_event", "rationale_contains",
        } and not isinstance(self.expected_value, str):
            raise ValueError(f"{self.kind} predicate must require a string")
        return self


class PrivateBenchmarkCaseRequirement(_FrozenContract):
    case_key: str = Field(pattern=IDENTIFIER_PATTERN)
    category: Literal[
        "normal", "boundary", "missing", "conflicting", "mandatory_escalation"
    ]
    observable_setup: str = Field(min_length=1)
    observable_action: str = Field(min_length=1)
    source_case_keys: tuple[str, ...] = Field(min_length=1)
    expected_predicates: tuple[BenchmarkExpectedPredicate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_predicates(self) -> "PrivateBenchmarkCaseRequirement":
        keys = tuple(item.predicate_key for item in self.expected_predicates)
        if len(keys) != len(set(keys)):
            raise ValueError("benchmark predicate keys must be unique")
        kinds = {item.kind for item in self.expected_predicates}
        required_dimensions = (
            (BUSINESS_BENCHMARK_PREDICATE_KINDS, "business outcome"),
            (RATIONALE_BENCHMARK_PREDICATE_KINDS, "rationale"),
            (SAFETY_BENCHMARK_PREDICATE_KINDS, "safety"),
            (TOOL_BENCHMARK_PREDICATE_KINDS, "required tool"),
        )
        for allowed, label in required_dimensions:
            if kinds.isdisjoint(allowed):
                raise ValueError(f"private benchmark case requires a {label} predicate")
        if (self.category == "mandatory_escalation"
                and kinds.isdisjoint(HANDOFF_BENCHMARK_PREDICATE_KINDS)):
            raise ValueError(
                "mandatory escalation benchmark case requires a handoff predicate"
            )
        return self


class RequestedIntegration(_FrozenContract):
    integration_key: str = Field(pattern=IDENTIFIER_PATTERN)
    purpose: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    required: bool
    credential_aliases: tuple[str, ...] = ()
    success_behavior: str = Field(min_length=1)
    failure_behavior: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_aliases(self) -> "RequestedIntegration":
        if len(self.credential_aliases) != len(set(self.credential_aliases)):
            raise ValueError("credential aliases must be unique")
        for alias in self.credential_aliases:
            CredentialAlias(alias=alias)
        return self


class RequestedAgent(_FrozenContract):
    agent_key: str = Field(pattern=IDENTIFIER_PATTERN)
    purpose: str = Field(min_length=1)
    responsibilities: tuple[str, ...] = Field(min_length=1)
    input_schema_markdown: str = Field(min_length=1)
    output_schema_markdown: str = Field(min_length=1)
    handoffs: tuple[str, ...]
    prompt_requirements: tuple[str, ...] = Field(min_length=1)
    integration_keys: tuple[str, ...] = ()
    n8n_requirement: Literal["required", "not_required"]
    success_metrics: tuple[str, ...] = Field(min_length=1)
    real_cases: tuple[RealCaseRequirement, ...] = Field(min_length=1)


class FactoryInputDocumentV2(_FrozenContract):
    schema_name: Literal["captain.to-be-built.v1"] = "captain.to-be-built.v1"
    input_ref: ArtifactRef
    byte_length: int = Field(ge=1, strict=True)
    source_name: Literal["TO_BE_BUILT.md"]
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    authority_boundaries: tuple[str, ...] = Field(min_length=1)
    agents: tuple[RequestedAgent, ...] = Field(min_length=1)
    integrations: tuple[RequestedIntegration, ...]
    shared_workflows: tuple[str, ...] = Field(min_length=1)
    security_requirements: tuple[str, ...] = Field(min_length=1)
    acceptance_outcomes: tuple[RealCaseRequirement, ...] = Field(min_length=1)
    real_cases: tuple[RealCaseRequirement, ...] = Field(min_length=1)
    helpful_resources: tuple[str, ...] = Field(min_length=1)
    stop_conditions: tuple[str, ...] = Field(min_length=1)
    private_benchmark_cases: tuple[PrivateBenchmarkCaseRequirement, ...] = ()
    sections: tuple[InputSection, ...] = Field(min_length=10)
    extra_sections: tuple[InputSection, ...] = ()

    @model_validator(mode="after")
    def validate_stable_names(self) -> "FactoryInputDocumentV2":
        agent_keys = tuple(item.agent_key for item in self.agents)
        integration_keys = tuple(item.integration_key for item in self.integrations)
        if len(agent_keys) != len(set(agent_keys)):
            raise ValueError("duplicate agent stable name")
        if len(integration_keys) != len(set(integration_keys)):
            raise ValueError("duplicate integration stable name")
        known_agents = set(agent_keys)
        known_integrations = set(integration_keys)
        benchmark_keys = tuple(item.case_key for item in self.private_benchmark_cases)
        if len(benchmark_keys) != len(set(benchmark_keys)):
            raise ValueError("duplicate private benchmark case stable name")
        if self.private_benchmark_cases:
            categories = (
                "normal", "boundary", "missing", "conflicting", "mandatory_escalation"
            )
            if (len(self.private_benchmark_cases) != 15
                    or any(sum(item.category == category for item in self.private_benchmark_cases) != 3
                           for category in categories)):
                raise ValueError(
                    "private benchmark cases require exactly three cases per category"
                )
            semantic_digests = tuple(
                hashlib.sha256(json.dumps({
                    "category": item.category,
                    "setup": item.observable_setup,
                    "action": item.observable_action,
                    "source_case_keys": item.source_case_keys,
                    "expected_predicates": sorted(
                        (
                            predicate.kind,
                            predicate.observed_key,
                            predicate.expected_value,
                        )
                        for predicate in item.expected_predicates
                    ),
                }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                for item in self.private_benchmark_cases
            )
            if len(semantic_digests) != len(set(semantic_digests)):
                raise ValueError("private benchmark semantic definitions must be unique")
        source_case_keys = {
            *(item.case_key for item in self.real_cases),
            *(item.case_key for item in self.acceptance_outcomes),
            *(item.integration_key for item in self.integrations),
        }
        for item in self.private_benchmark_cases:
            if set(item.source_case_keys) - source_case_keys:
                raise ValueError("private benchmark case references an unknown source case")
        for agent in self.agents:
            unknown_handoffs = set(agent.handoffs) - known_agents
            if unknown_handoffs:
                raise ValueError(f"unknown handoff: {sorted(unknown_handoffs)[0]}")
            unknown_integrations = set(agent.integration_keys) - known_integrations
            if unknown_integrations:
                raise ValueError(f"unknown integration: {sorted(unknown_integrations)[0]}")
        return self
