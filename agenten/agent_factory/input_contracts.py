"""Frozen contracts for canonical ``TO_BE_BUILT.md`` requests."""

from __future__ import annotations

import re
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
BENCHMARK_CATEGORIES = (
    "normal", "boundary", "missing", "conflicting", "mandatory_escalation",
)
BENCHMARK_DIMENSIONS = (
    "business_outcome", "rationale", "safety", "required_tool",
)
BENCHMARK_RULESET_REFS = (
    "captain.business-outcome.v1",
    "captain.rationale.v1",
    "captain.safety.v1",
    "captain.tool-decision.v1",
    "captain.handoff.v1",
)
BenchmarkSourceAuthorityType = Literal[
    "business", "rationale", "safety", "tool", "handoff",
]


def benchmark_predicate_authority_type(
    predicate_kind: str,
) -> BenchmarkSourceAuthorityType:
    if predicate_kind in BUSINESS_BENCHMARK_PREDICATE_KINDS:
        return "business"
    if predicate_kind in RATIONALE_BENCHMARK_PREDICATE_KINDS:
        return "rationale"
    if predicate_kind in SAFETY_BENCHMARK_PREDICATE_KINDS:
        return "safety"
    if predicate_kind in TOOL_BENCHMARK_PREDICATE_KINDS:
        return "tool"
    if predicate_kind in HANDOFF_BENCHMARK_PREDICATE_KINDS:
        return "handoff"
    raise ValueError("private benchmark predicate authority type is invalid")


def benchmark_source_supports_authority(
    authority_type: BenchmarkSourceAuthorityType,
    *,
    assertion_kind: str,
    source_path: tuple[str, ...],
    observable_expected: str,
) -> bool:
    if not source_path:
        return False
    source_section = source_path[0]
    if authority_type in {"business", "rationale"}:
        return (
            assertion_kind == "business"
            and source_section in {"Acceptance outcomes", "Real cases", "Agents"}
        ) or (
            assertion_kind == "recovery"
            and source_section in {"Integrations", "Stop conditions"}
        )
    if authority_type == "safety":
        return (
            assertion_kind == "security"
            and source_section in {
                "Authority boundaries", "Security requirements", "Tool policy",
            }
            and any(
                marker in observable_expected.casefold()
                for marker in (
                    "tool lease", "integration lease", "released team manifest",
                )
            )
        )
    if authority_type == "tool":
        return (
            assertion_kind == "integration"
            and source_section == "Tool policy"
            and source_path[-1].startswith("tool_")
        )
    return (
        (
            assertion_kind == "security"
            and source_section == "Authority boundaries"
        )
        or (
            assertion_kind == "recovery"
            and source_section == "Integrations"
        )
    ) and re.search(
        r"\b(?:human|owner|approver|commander|adjuster|reviewer)\b",
        observable_expected,
        re.IGNORECASE,
    ) is not None


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
    source_case_keys: tuple[str, ...] = ()

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
    declared_tool_keys: tuple[str, ...] = Field(min_length=1)
    expected_predicates: tuple[BenchmarkExpectedPredicate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_predicates(self) -> "PrivateBenchmarkCaseRequirement":
        if len(self.declared_tool_keys) != len(set(self.declared_tool_keys)):
            raise ValueError("declared benchmark tool keys must be unique")
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
        mapped = tuple(
            source
            for predicate in self.expected_predicates
            for source in predicate.source_case_keys
        )
        if mapped:
            if any(not predicate.source_case_keys for predicate in self.expected_predicates):
                raise ValueError("all benchmark predicates require source mappings")
            if set(mapped) != set(self.source_case_keys):
                raise ValueError(
                    "benchmark case sources must exactly equal predicate source mappings"
                )
        return self


class BenchmarkPolicyRequirement(_FrozenContract):
    """Public benchmark shape; it never carries cases or expected answers."""

    categories: tuple[str, ...] = BENCHMARK_CATEGORIES
    cases_per_category: int = Field(default=3, strict=True)
    required_dimensions: tuple[str, ...] = BENCHMARK_DIMENSIONS
    ruleset_refs: tuple[str, ...] = BENCHMARK_RULESET_REFS
    source_case_keys: tuple[str, ...] = Field(min_length=1)
    public_policy_statement: str | None = None

    @model_validator(mode="after")
    def require_canonical_public_shape(self) -> "BenchmarkPolicyRequirement":
        if self.categories != BENCHMARK_CATEGORIES or self.cases_per_category != 3:
            raise ValueError("benchmark policy requires exactly three cases per category")
        if self.required_dimensions != BENCHMARK_DIMENSIONS:
            raise ValueError("benchmark policy dimensions are not canonical")
        if self.ruleset_refs != BENCHMARK_RULESET_REFS:
            raise ValueError("benchmark policy rulesets are not canonical")
        if len(self.source_case_keys) != len(set(self.source_case_keys)):
            raise ValueError("benchmark policy source keys must be unique")
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


class DeclaredTool(_FrozenContract):
    tool_key: str = Field(pattern=IDENTIFIER_PATTERN)
    requirement: Literal["required", "optional"]
    location: Literal["local", "integration"]
    description: str = Field(min_length=1)


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
    declared_tools: tuple[DeclaredTool, ...] = ()
    tool_policy_guard: str | None = None
    benchmark_policy: BenchmarkPolicyRequirement
    sections: tuple[InputSection, ...] = Field(min_length=10)
    extra_sections: tuple[InputSection, ...] = ()

    @model_validator(mode="after")
    def validate_stable_names(self) -> "FactoryInputDocumentV2":
        agent_keys = tuple(item.agent_key for item in self.agents)
        integration_keys = tuple(item.integration_key for item in self.integrations)
        tool_keys = tuple(item.tool_key for item in self.declared_tools)
        if len(agent_keys) != len(set(agent_keys)):
            raise ValueError("duplicate agent stable name")
        if len(integration_keys) != len(set(integration_keys)):
            raise ValueError("duplicate integration stable name")
        if len(tool_keys) != len(set(tool_keys)):
            raise ValueError("duplicate declared tool stable name")
        known_agents = set(agent_keys)
        known_integrations = set(integration_keys)
        public_source_keys = (
            *(item.case_key for item in self.real_cases),
            *(item.case_key for item in self.acceptance_outcomes),
            *(case.case_key for agent in self.agents for case in agent.real_cases),
            *(f"{item.integration_key}_integration" for item in self.integrations),
            *(f"{item.integration_key}_failure" for item in self.integrations),
            *(f"{item.integration_key}_tool" for item in self.integrations),
            *(f"authority_{index}" for index, _ in enumerate(self.authority_boundaries, 1)),
            *(f"security_{index}" for index, _ in enumerate(self.security_requirements, 1)),
            *(f"stop_{index}" for index, _ in enumerate(self.stop_conditions, 1)),
            *(f"tool_{item.tool_key}" for item in self.declared_tools),
            *(("tool_policy_guard",) if self.tool_policy_guard else ()),
        )
        if len(public_source_keys) != len(set(public_source_keys)):
            raise ValueError("benchmark public source keys must be unique")
        source_case_keys = set(public_source_keys)
        if set(self.benchmark_policy.source_case_keys) - source_case_keys:
            raise ValueError("benchmark policy references an unknown public source")
        for agent in self.agents:
            unknown_handoffs = set(agent.handoffs) - known_agents
            if unknown_handoffs:
                raise ValueError(f"unknown handoff: {sorted(unknown_handoffs)[0]}")
            unknown_integrations = set(agent.integration_keys) - known_integrations
            if unknown_integrations:
                raise ValueError(f"unknown integration: {sorted(unknown_integrations)[0]}")
        return self
