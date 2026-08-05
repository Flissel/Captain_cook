"""Deterministic offline compilation of canonical Factory input."""

from __future__ import annotations

import hashlib
import json
import re
from graphlib import CycleError, TopologicalSorter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.holdout_store import PrivateHoldoutStore
from agenten.agent_factory.input_contracts import FactoryInputDocumentV2, RealCaseRequirement
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
        return self


class FactoryInputCompiler:
    def __init__(self, *, holdout_store: PrivateHoldoutStore) -> None:
        self._holdout_store = holdout_store

    def compile(self, document: FactoryInputDocumentV2, subject_version: int) -> CompiledFactorySpecification:
        if isinstance(subject_version, bool) or subject_version < 1:
            raise ValueError("subject_version must be a positive integer")
        assertions = self._assertions(document)
        holdouts = self._holdouts(document)
        nodes = self._nodes(document)
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
        }
        compilation_digest = hashlib.sha256(_canonical_json(digest_payload).encode()).hexdigest()
        return CompiledFactorySpecification(source_ref=document.input_ref, subject_version=subject_version, capability_key=_capability_key(document.title), assertions=assertions, private_holdout_refs=holdouts, work_nodes=nodes, dependency_order=order, compilation_digest=compilation_digest)

    def _assertions(self, document: FactoryInputDocumentV2) -> tuple[AcceptanceAssertion, ...]:
        result = []
        sources: list[tuple[tuple[str, ...], RealCaseRequirement, str]] = []
        sources.extend((("Acceptance outcomes", case.case_key), case, "business") for case in document.acceptance_outcomes)
        sources.extend((("Real cases", case.case_key), case, "business") for case in document.real_cases)
        for integration in document.integrations:
            case = RealCaseRequirement(case_key=f"{integration.integration_key}_integration", observable_setup=integration.trigger, observable_action=integration.operation, observable_expected=integration.success_behavior)
            sources.append((("Integrations", integration.integration_key), case, "integration"))
        for index, rule in enumerate(document.security_requirements, 1):
            case = RealCaseRequirement(case_key=f"security_{index}", observable_setup="Given the compiled capability", observable_action="Exercise the security boundary", observable_expected=rule)
            sources.append((("Security requirements", _semantic(rule)), case, "security"))
        for path, case, kind in sources:
            assertion_id = "assert-" + _short_id(document.input_ref.sha256, path, case.observable_expected)
            result.append(AcceptanceAssertion(assertion_id=assertion_id, source_path=path, observable_setup=case.observable_setup, observable_action=case.observable_action, observable_expected=case.observable_expected, kind=kind))
        return tuple(result)

    def _holdouts(self, document: FactoryInputDocumentV2) -> tuple[PrivateHoldoutRef, ...]:
        refs = []
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
