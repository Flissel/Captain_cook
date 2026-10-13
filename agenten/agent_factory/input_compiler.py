"""Deterministic offline compilation of canonical Factory input."""

from __future__ import annotations

import hashlib
import json
import re
from graphlib import CycleError, TopologicalSorter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from agenten.agent_factory.forge_contracts import (
    AgentHandoffV1,
    FactoryToolRequirementV1,
)
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.holdout_store import PrivateHoldoutStore
from agenten.agent_factory.input_contracts import (
    BenchmarkExpectedPredicate,
    FactoryInputDocumentV2,
    RealCaseRequirement,
    benchmark_predicate_authority_type,
    benchmark_source_supports_authority,
)
from agenten.agent_factory.private_benchmark_catalog import (
    PrivateBenchmarkCatalogError,
    PrivateBenchmarkCatalogPort,
)
from agenten.agent_runtime.contracts import ArtifactRef, IDENTIFIER_PATTERN


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AcceptanceAssertion(_FrozenContract):
    assertion_id: str = Field(pattern=r"^assert-[0-9a-f]{12}$")
    source_path: tuple[str, ...] = Field(min_length=1)
    observable_setup: str = Field(min_length=1)
    observable_action: str = Field(min_length=1)
    observable_expected: str = Field(min_length=1)
    kind: Literal["business", "schema", "integration", "security", "recovery"]


class FactoryWorkNode(_FrozenContract):
    node_id: str = Field(pattern=IDENTIFIER_PATTERN)
    kind: Literal["architecture", "integration_decision", "autogen_implementation", "n8n_workflow", "local_adapter", "package_assembly", "real_cases", "quality", "recovery", "release"]
    dependencies: tuple[str, ...] = ()
    agent_keys: tuple[str, ...] = ()
    integration_keys: tuple[str, ...] = ()


class CompiledFactorySpecification(_FrozenContract):
    schema_name: Literal["captain.compiled-factory-spec.v1"] = "captain.compiled-factory-spec.v1"
    source_ref: ArtifactRef
    subject_version: int = Field(ge=1, strict=True)
    capability_key: str = Field(pattern=IDENTIFIER_PATTERN)
    assertions: tuple[AcceptanceAssertion, ...] = Field(min_length=1)
    private_holdout_refs: tuple[PrivateHoldoutRef, ...] = Field(min_length=1)
    work_nodes: tuple[FactoryWorkNode, ...] = Field(min_length=1)
    dependency_order: tuple[str, ...] = Field(min_length=1)
    compilation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_agent_ids: tuple[str, ...] = ()
    ordered_handoffs: tuple[AgentHandoffV1, ...] = ()
    tool_requirements: tuple[FactoryToolRequirementV1, ...] = ()
    allowed_optional_tool_gaps: tuple[str, ...] = ()
    public_assertion_ids: tuple[str, ...] = ()

    @computed_field
    @property
    def assertion_ids(self) -> tuple[str, ...]:
        return tuple(item.assertion_id for item in self.assertions)

    @property
    def dependencies(self) -> dict[str, set[str]]:
        return {node.node_id: set(node.dependencies) for node in self.work_nodes}

    @model_validator(mode="after")
    def validate_uniqueness(self) -> "CompiledFactorySpecification":
        for label, values in (
            ("assertion", self.assertion_ids),
            ("holdout", tuple(item.holdout_id for item in self.private_holdout_refs)),
            ("work node", tuple(item.node_id for item in self.work_nodes)),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} ID")
        authority_present = any(
            (
                self.required_agent_ids,
                self.ordered_handoffs,
                self.tool_requirements,
                self.allowed_optional_tool_gaps,
                self.public_assertion_ids,
            )
        )
        if authority_present:
            agents = set(self.required_agent_ids)
            if not agents or len(agents) != len(self.required_agent_ids):
                raise ValueError("compiled required agents must be unique and nonempty")
            handoff_pairs = tuple(
                (item.from_agent_id, item.to_agent_id)
                for item in self.ordered_handoffs
            )
            if len(handoff_pairs) != len(set(handoff_pairs)) or any(
                left not in agents or right not in agents
                for left, right in handoff_pairs
            ):
                raise ValueError("compiled handoffs must bind unique required agents")
            tool_ids = tuple(item.tool_id for item in self.tool_requirements)
            # Inputs without integrations compile to a team with no tools; the
            # compiler must not invent tool intent, so only uniqueness is enforced.
            if len(tool_ids) != len(set(tool_ids)):
                raise ValueError("compiled tool requirements must be unique")
            expected_optional = tuple(
                item.tool_id for item in self.tool_requirements if not item.required
            )
            if self.allowed_optional_tool_gaps != expected_optional:
                raise ValueError("compiled optional tool gaps must match optional tools")
            if self.public_assertion_ids != self.assertion_ids:
                raise ValueError("compiled public assertions must match Captain assertions")
        return self


class FactoryInputCompiler:
    def __init__(self, *, holdout_store: PrivateHoldoutStore,
                 private_benchmark_catalog: PrivateBenchmarkCatalogPort | None = None) -> None:
        self._holdout_store = holdout_store
        self._private_benchmark_catalog = private_benchmark_catalog

    def compile(self, document: FactoryInputDocumentV2, subject_version: int) -> CompiledFactorySpecification:
        if isinstance(subject_version, bool) or subject_version < 1:
            raise ValueError("subject_version must be a positive integer")
        assertions = self._assertions(document)
        holdouts = self._holdouts(document, assertions)
        nodes = self._nodes(document)
        required_agent_ids = tuple(agent.agent_key for agent in document.agents)
        ordered_handoffs = tuple(
            AgentHandoffV1(
                from_agent_id=agent.agent_key,
                to_agent_id=target,
            )
            for agent in document.agents
            for target in agent.handoffs
        )
        tool_requirements = tuple(
            FactoryToolRequirementV1(
                tool_id=integration.integration_key,
                required=integration.required,
            )
            for integration in document.integrations
        )
        optional_tool_gaps = tuple(
            item.tool_id for item in tool_requirements if not item.required
        )
        public_assertion_ids = tuple(item.assertion_id for item in assertions)
        dependencies = {node.node_id: set(node.dependencies) for node in nodes}
        sorter = TopologicalSorter(dependencies)
        try:
            order = tuple(sorter.static_order())
        except CycleError as exc:
            raise ValueError("compiled factory work graph is cyclic") from exc
        digest_payload = {
            "source_sha256": document.input_ref.sha256,
            "subject_version": subject_version,
            "assertions": [item.model_dump(mode="json") for item in assertions],
            "holdouts": [item.model_dump(mode="json") for item in holdouts],
            "nodes": [item.model_dump(mode="json") for item in nodes],
            "required_agent_ids": required_agent_ids,
            "ordered_handoffs": [
                item.model_dump(mode="json") for item in ordered_handoffs
            ],
            "tool_requirements": [
                item.model_dump(mode="json") for item in tool_requirements
            ],
            "allowed_optional_tool_gaps": optional_tool_gaps,
            "public_assertion_ids": public_assertion_ids,
        }
        compilation_digest = hashlib.sha256(_canonical_json(digest_payload).encode()).hexdigest()
        return CompiledFactorySpecification(
            source_ref=document.input_ref,
            subject_version=subject_version,
            capability_key=_capability_key(document.title),
            assertions=assertions,
            private_holdout_refs=holdouts,
            work_nodes=nodes,
            dependency_order=order,
            compilation_digest=compilation_digest,
            required_agent_ids=required_agent_ids,
            ordered_handoffs=ordered_handoffs,
            tool_requirements=tool_requirements,
            allowed_optional_tool_gaps=optional_tool_gaps,
            public_assertion_ids=public_assertion_ids,
        )

    def _assertions(self, document: FactoryInputDocumentV2) -> tuple[AcceptanceAssertion, ...]:
        result = []
        sources: list[tuple[tuple[str, ...], RealCaseRequirement, str]] = []
        sources.extend((("Acceptance outcomes", case.case_key), case, "business") for case in document.acceptance_outcomes)
        sources.extend((("Real cases", case.case_key), case, "business") for case in document.real_cases)
        sources.extend(
            (("Agents", agent.agent_key, "Real cases", case.case_key), case, "business")
            for agent in document.agents for case in agent.real_cases
        )
        for integration in document.integrations:
            case = RealCaseRequirement(case_key=f"{integration.integration_key}_integration", observable_setup=integration.trigger, observable_action=integration.operation, observable_expected=integration.success_behavior)
            sources.append((("Integrations", case.case_key), case, "integration"))
            failure = RealCaseRequirement(case_key=f"{integration.integration_key}_failure", observable_setup=integration.trigger, observable_action=integration.operation, observable_expected=integration.failure_behavior)
            sources.append((("Integrations", failure.case_key), failure, "recovery"))
            tool = RealCaseRequirement(case_key=f"{integration.integration_key}_tool", observable_setup=integration.trigger, observable_action=integration.operation, observable_expected=integration.success_behavior)
            sources.append((("Integrations", tool.case_key), tool, "integration"))
        for index, rule in enumerate(document.authority_boundaries, 1):
            case = RealCaseRequirement(case_key=f"authority_{index}", observable_setup="Given a bounded Captain authority", observable_action="Exercise the authority boundary", observable_expected=rule)
            sources.append((("Authority boundaries", case.case_key), case, "security"))
        for index, rule in enumerate(document.security_requirements, 1):
            case = RealCaseRequirement(case_key=f"security_{index}", observable_setup="Given the compiled capability", observable_action="Exercise the security boundary", observable_expected=rule)
            sources.append((("Security requirements", case.case_key), case, "security"))
        for index, rule in enumerate(document.stop_conditions, 1):
            case = RealCaseRequirement(case_key=f"stop_{index}", observable_setup="Given a controlled failure", observable_action="Exercise the stop condition", observable_expected=rule)
            sources.append((("Stop conditions", case.case_key), case, "recovery"))
        for tool in document.declared_tools:
            case = RealCaseRequirement(
                case_key=f"tool_{tool.tool_key}",
                observable_setup=tool.description,
                observable_action=tool.description,
                observable_expected=tool.description,
            )
            sources.append((("Tool policy", case.case_key), case, "integration"))
        if document.tool_policy_guard is not None:
            case = RealCaseRequirement(
                case_key="tool_policy_guard",
                observable_setup=document.tool_policy_guard,
                observable_action=document.tool_policy_guard,
                observable_expected=document.tool_policy_guard,
            )
            sources.append((("Tool policy", case.case_key), case, "security"))
        for path, case, kind in sources:
            assertion_id = "assert-" + _short_id(document.input_ref.sha256, path, case.observable_expected)
            result.append(AcceptanceAssertion(assertion_id=assertion_id, source_path=path, observable_setup=case.observable_setup, observable_action=case.observable_action, observable_expected=case.observable_expected, kind=kind))
        return tuple(result)

    def _holdouts(
        self,
        document: FactoryInputDocumentV2,
        assertions: tuple[AcceptanceAssertion, ...],
    ) -> tuple[PrivateHoldoutRef, ...]:
        refs = []
        if self._private_benchmark_catalog is not None:
            suite = self._private_benchmark_catalog.resolve(document.input_ref.sha256)
            expected_policy_digest = hashlib.sha256(
                _canonical_json(document.benchmark_policy.model_dump(mode="json")).encode()
            ).hexdigest()
            if (suite.input_sha256 != document.input_ref.sha256
                    or suite.policy_sha256 != expected_policy_digest):
                raise PrivateBenchmarkCatalogError(
                    "private benchmark suite changed admitted input or policy authority"
                )
            assertions_by_source = {
                item.source_path[-1]: item for item in assertions
            }
            entries: list[tuple[str, str]] = []
            for case in suite.cases:
                try:
                    case_assertions = tuple(
                        assertions_by_source[key] for key in case.source_case_keys
                    )
                except KeyError:
                    raise PrivateBenchmarkCatalogError(
                        "private benchmark case references an unknown admitted source"
                    ) from None
                assertion_ids = tuple(item.assertion_id for item in case_assertions)
                predicate_bindings = []
                for predicate in case.expected_predicates:
                    authority_type = benchmark_predicate_authority_type(
                        predicate.kind,
                    )
                    try:
                        source_assertions = tuple(
                            assertions_by_source[key]
                            for key in predicate.source_case_keys
                        )
                    except KeyError:
                        raise PrivateBenchmarkCatalogError(
                            "private benchmark predicate source binding is invalid"
                        ) from None
                    _validate_predicate_source_binding(
                        predicate, source_assertions,
                    )
                    predicate_bindings.append({
                        "predicate_key": predicate.predicate_key,
                        "authority_type": authority_type,
                        "sources": [
                            {
                                "source_case_key": source_case_key,
                                "assertion_id": assertion.assertion_id,
                                "assertion_sha256": _assertion_sha256(assertion),
                            }
                            for source_case_key, assertion in zip(
                                predicate.source_case_keys,
                                source_assertions,
                                strict=True,
                            )
                        ],
                    })
                bound_assertion_ids = tuple(dict.fromkeys(
                    source["assertion_id"]
                    for binding in predicate_bindings
                    for source in binding["sources"]
                ))
                if (
                    set(bound_assertion_ids) != set(assertion_ids)
                    or set(case.source_case_keys) != {
                        source
                        for predicate in case.expected_predicates
                        for source in predicate.source_case_keys
                    }
                ):
                    raise PrivateBenchmarkCatalogError(
                        "private benchmark predicate source binding is invalid"
                    )
                body = _canonical_json({
                    "case_key": case.case_key,
                    "category": case.category,
                    "observable_setup": case.observable_setup,
                    "observable_action": case.observable_action,
                    "source_assertion_ids": assertion_ids,
                    "predicate_source_bindings": predicate_bindings,
                    "declared_tool_keys": list(case.declared_tool_keys),
                    "expected_predicates": [
                        item.model_dump(mode="json")
                        for item in case.expected_predicates
                    ],
                })
                digest = hashlib.sha256(body.encode()).hexdigest()
                holdout_id = "holdout-" + _short_id(
                    document.input_ref.sha256,
                    ("private-benchmark", case.category, case.case_key),
                    digest,
                )
                uri = f"holdout://{holdout_id}"
                entries.append((uri, body))
                refs.append(PrivateHoldoutRef(
                    holdout_id=holdout_id, uri=uri, sha256=digest
                ))
            self._holdout_store.put_many(tuple(entries))
            return tuple(refs)
        cases = list(document.real_cases)
        for index, stop in enumerate(document.stop_conditions):
            source = cases[index % len(cases)]
            body = _canonical_json({"source_path": ["Stop conditions", _semantic(stop)], "observable_setup": source.observable_setup, "observable_action": "Exercise controlled recovery", "observable_expected": stop})
            digest = hashlib.sha256(body.encode()).hexdigest()
            holdout_id = "holdout-" + _short_id(document.input_ref.sha256, ("private-holdout", _semantic(stop)), digest)
            uri = f"holdout://{holdout_id}"
            self._holdout_store.put(uri, body)
            refs.append(PrivateHoldoutRef(holdout_id=holdout_id, uri=uri, sha256=digest))
        return tuple(refs)

    def _nodes(self, document: FactoryInputDocumentV2) -> tuple[FactoryWorkNode, ...]:
        nodes = [FactoryWorkNode(node_id="architecture", kind="architecture")]
        integration_nodes = []
        for integration in document.integrations:
            decision = f"integration-{integration.integration_key}"
            integration_nodes.append(decision)
            nodes.append(FactoryWorkNode(node_id=decision, kind="integration_decision", dependencies=("architecture",), integration_keys=(integration.integration_key,)))
            nodes.append(FactoryWorkNode(node_id=f"n8n-{integration.integration_key}", kind="n8n_workflow", dependencies=(decision,)))
        agent_nodes = []
        for agent in document.agents:
            node_id = f"agent-{agent.agent_key}"
            agent_nodes.append(node_id)
            deps = tuple(["architecture"] + [f"integration-{key}" for key in agent.integration_keys])
            nodes.append(FactoryWorkNode(node_id=node_id, kind="autogen_implementation", dependencies=deps, agent_keys=(agent.agent_key,)))
        nodes.append(FactoryWorkNode(node_id="local-adapters", kind="local_adapter", dependencies=tuple(integration_nodes)))
        build_deps = tuple(agent_nodes + [f"n8n-{item.integration_key}" for item in document.integrations] + ["local-adapters"])
        nodes.extend([
            FactoryWorkNode(node_id="package-assembly", kind="package_assembly", dependencies=build_deps),
            FactoryWorkNode(node_id="real-cases", kind="real_cases", dependencies=("package-assembly",)),
            FactoryWorkNode(node_id="quality", kind="quality", dependencies=("real-cases",)),
            FactoryWorkNode(node_id="recovery", kind="recovery", dependencies=("quality",)),
            FactoryWorkNode(node_id="release", kind="release", dependencies=("recovery",)),
        ])
        return tuple(nodes)


def _short_id(source_digest: str, path: tuple[str, ...], outcome: str) -> str:
    material = "\x1f".join((source_digest, *(_semantic(item) for item in path), _semantic(outcome)))
    return hashlib.sha256(material.encode()).hexdigest()[:12]


def _semantic(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _capability_key(title: str) -> str:
    key = _semantic(title)
    if not key or not key[0].isalpha():
        key = f"capability_{key}"
    return key


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _assertion_sha256(assertion: AcceptanceAssertion) -> str:
    return hashlib.sha256(
        _canonical_json(assertion.model_dump(mode="json")).encode()
    ).hexdigest()


def _validate_predicate_source_binding(
    predicate: BenchmarkExpectedPredicate,
    sources: tuple[AcceptanceAssertion, ...],
) -> None:
    authority_type = benchmark_predicate_authority_type(predicate.kind)
    if (
        not sources
        or tuple(item.source_path[-1] for item in sources)
        != predicate.source_case_keys
        or any(
            not benchmark_source_supports_authority(
                authority_type,
                assertion_kind=item.kind,
                source_path=item.source_path,
                observable_expected=item.observable_expected,
            )
            for item in sources
        )
    ):
        raise PrivateBenchmarkCatalogError(
            "private benchmark predicate source binding is invalid"
        )
    expected = predicate.expected_value
    source_expected = tuple(item.observable_expected for item in sources)
    valid = False
    if predicate.kind in {"observation_equals", "observation_contains"}:
        valid = (
            predicate.observed_key == "business_outcome"
            and isinstance(expected, str)
            and expected in source_expected
        )
    elif predicate.kind == "rationale_contains":
        valid = (
            predicate.observed_key == "business_outcome"
            and isinstance(expected, str)
            and expected in source_expected
        )
    elif predicate.kind == "forbidden_tool_absent":
        valid = (
            predicate.observed_key == "declared_tools"
            and expected is True
            and all(item.kind == "security" for item in sources)
            and any(
                marker in item.observable_expected.casefold()
                for item in sources
                for marker in (
                    "tool lease", "integration lease", "released team manifest",
                )
            )
        )
    elif predicate.kind == "tool_receipt":
        valid = (
            len(sources) == 1
            and predicate.observed_key
            == sources[0].source_path[-1].removeprefix("tool_")
            and sources[0].source_path[0] == "Tool policy"
            and expected is True
        )
    elif predicate.kind == "handoff_event":
        valid = (
            predicate.observed_key == "to_agent"
            and isinstance(expected, str)
            and any(
                expected.casefold() in item.observable_expected.casefold()
                for item in sources
            )
        )
    if not valid:
        raise PrivateBenchmarkCatalogError(
            "private benchmark predicate source binding is invalid"
        )
