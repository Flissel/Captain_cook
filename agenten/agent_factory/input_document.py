"""Strict byte-level loader for canonical ``TO_BE_BUILT.md`` input."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

from pydantic import ValidationError

from agenten.agent_factory.input_contracts import (
    BenchmarkPolicyRequirement,
    FactoryInputDocumentV2,
    InputSection,
    RealCaseRequirement,
    RequestedAgent,
    RequestedIntegration,
)
from agenten.agent_runtime.contracts import ArtifactRef


FactoryInputDocument = FactoryInputDocumentV2

REQUIRED_SECTIONS = (
    "Objective",
    "Authority boundaries",
    "Agents",
    "Integrations",
    "Shared workflows",
    "Security requirements",
    "Acceptance outcomes",
    "Real cases",
    "Helpful resources",
    "Stop conditions",
)
_HEADING = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$")
_SECRET_ASSIGNMENT = re.compile(r"\b(?P<alias>[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD))\s*[:=]\s*\S+", re.I)
_BEARER = re.compile(r"\bBearer\s+\S+", re.I)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
_CREDENTIAL_URL = re.compile(r"https?://[^\s/:]+:[^\s/@]+@", re.I)
_PUBLIC_PRIVATE_POLICY_HEADINGS = {
    "private benchmark policy",
    "private holdout policy",
}


class FactoryInputError(ValueError):
    """The canonical input is incomplete or cannot be represented safely."""


def load_factory_input(path: Path) -> FactoryInputDocumentV2:
    if path.name != "TO_BE_BUILT.md":
        raise FactoryInputError("canonical factory input must be named TO_BE_BUILT.md")
    try:
        source_bytes = path.read_bytes()
    except FileNotFoundError as exc:
        raise FactoryInputError("canonical TO_BE_BUILT.md is missing") from exc
    return parse_factory_input_bytes(source_bytes, logical_name=path.name)


def parse_factory_input_bytes(source: bytes, logical_name: str) -> FactoryInputDocumentV2:
    if logical_name != "TO_BE_BUILT.md":
        raise FactoryInputError("canonical factory input must be named TO_BE_BUILT.md")
    if source.startswith(b"\xef\xbb\xbf"):
        raise FactoryInputError("UTF-8 BOM is not allowed")
    if b"\x00" in source:
        raise FactoryInputError("NUL bytes are not allowed")
    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FactoryInputError("factory input must be strict UTF-8") from exc
    if "<!-- CAPTAIN_REVIEW_REQUIRED -->" in text:
        raise FactoryInputError("review marker must be resolved before canonical loading")
    _reject_credentials(text)
    title, ordered = _level_sections(text)
    required: dict[str, str] = {}
    extras: list[InputSection] = []
    benchmark_policy_statement: str | None = None
    for name, body in ordered:
        normalized_name = _normalized_heading(name)
        if (normalized_name in _PUBLIC_PRIVATE_POLICY_HEADINGS
                and name not in {"Private benchmark policy", "Private holdout policy"}):
            raise FactoryInputError(
                "worker-visible input must not contain private benchmark cases"
            )
        if _is_reserved_private_heading(normalized_name):
            raise FactoryInputError(
                "worker-visible input must not contain private benchmark cases"
            )
        if name in REQUIRED_SECTIONS:
            if name in required:
                raise FactoryInputError(f"duplicate required section: {name}")
            required[name] = body
        else:
            if normalized_name in _PUBLIC_PRIVATE_POLICY_HEADINGS:
                if benchmark_policy_statement is not None:
                    raise FactoryInputError("duplicate Private holdout policy section")
                benchmark_policy_statement = body
            extras.append(InputSection(heading=name, heading_path=(name,), markdown=body))
    missing = [name for name in REQUIRED_SECTIONS if name not in required]
    if missing:
        raise FactoryInputError(f"missing required section: {', '.join(missing)}")
    digest = hashlib.sha256(source).hexdigest()
    try:
        agents = _parse_agents(required["Agents"])
        integrations = _parse_integrations(required["Integrations"])
        agent_keys = tuple(agent.agent_key for agent in agents)
        if len(agent_keys) != len(set(agent_keys)):
            raise FactoryInputError("duplicate agent stable name")
        _validate_handoff_dag(agents)
        real_cases = _cases(required["Real cases"])
        if not real_cases:
            raise FactoryInputError("at least one public success case is required")
        authority_boundaries = _items(required["Authority boundaries"])
        security_requirements = _items(required["Security requirements"])
        acceptance_outcomes = _cases(required["Acceptance outcomes"])
        stop_conditions = _items(required["Stop conditions"])
        benchmark_source_keys = tuple(dict.fromkeys((
            *(case.case_key for case in acceptance_outcomes),
            *(case.case_key for case in real_cases),
            *(case.case_key for agent in agents for case in agent.real_cases),
            *(f"{item.integration_key}_integration" for item in integrations),
            *(f"{item.integration_key}_failure" for item in integrations),
            *(f"{item.integration_key}_tool" for item in integrations),
            *(f"authority_{index}" for index, _ in enumerate(authority_boundaries, 1)),
            *(f"security_{index}" for index, _ in enumerate(security_requirements, 1)),
            *(f"stop_{index}" for index, _ in enumerate(stop_conditions, 1)),
        )))
        return FactoryInputDocumentV2(
            input_ref=ArtifactRef(uri=f"artifact://factory-input/{digest}", sha256=digest, media_type="text/markdown"),
            byte_length=len(source), source_name="TO_BE_BUILT.md", title=title,
            objective=_paragraph(required["Objective"]),
            authority_boundaries=authority_boundaries,
            agents=agents, integrations=integrations,
            shared_workflows=_items(required["Shared workflows"]),
            security_requirements=security_requirements,
            acceptance_outcomes=acceptance_outcomes,
            real_cases=real_cases,
            helpful_resources=_items(required["Helpful resources"]),
            stop_conditions=stop_conditions,
            benchmark_policy=BenchmarkPolicyRequirement(
                source_case_keys=benchmark_source_keys,
                public_policy_statement=benchmark_policy_statement,
            ),
            sections=tuple(InputSection(heading=name, heading_path=(name,), markdown=required[name]) for name in REQUIRED_SECTIONS),
            extra_sections=tuple(extras),
        )
    except ValidationError as exc:
        raise FactoryInputError(str(exc)) from exc


def parse_factory_input(source: str) -> FactoryInputDocumentV2:
    """Compatibility helper with the new canonical logical name."""
    return parse_factory_input_bytes(source.encode("utf-8"), logical_name="TO_BE_BUILT.md")


def _reject_credentials(text: str) -> None:
    for number, line in enumerate(text.splitlines(), 1):
        match = _SECRET_ASSIGNMENT.search(line)
        if match:
            raise FactoryInputError(f"credential value is forbidden at line {number} for {match.group('alias')}")
        if _BEARER.search(line) or _PRIVATE_KEY.search(line) or _CREDENTIAL_URL.search(line):
            raise FactoryInputError(f"credential value is forbidden at line {number}")


def _normalized_heading(value: str) -> str:
    compatible = unicodedata.normalize("NFKC", value).casefold()
    visible = "".join(
        character if character.isalnum() else " "
        for character in compatible
        if unicodedata.category(character) != "Cf"
    )
    return " ".join(visible.split())


def _is_reserved_private_heading(normalized: str) -> bool:
    if normalized in _PUBLIC_PRIVATE_POLICY_HEADINGS:
        return False
    tokens = set(normalized.split())
    authority = bool(tokens & {"private", "hidden"})
    domain = bool(tokens & {"benchmark", "benchmarks", "holdout", "evaluation"})
    payload = bool(tokens & {
        "case", "cases", "fixture", "fixtures", "predicate", "predicates",
        "answer", "answers",
    }) or {"expected", "outcome"}.issubset(tokens) or {
        "expected", "outcomes",
    }.issubset(tokens)
    return (authority and (domain or payload)) or (domain and payload)


def _level_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    lines = text.splitlines()
    title = next((m.group("title") for line in lines if (m := _HEADING.match(line)) and len(m.group("marks")) == 1), "")
    if not title:
        raise FactoryInputError("document title is required")
    starts = [(i, m.group("title")) for i, line in enumerate(lines) if (m := _HEADING.match(line)) and len(m.group("marks")) == 2]
    result = []
    for pos, (index, name) in enumerate(starts):
        end = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
        body = "\n".join(lines[index + 1:end]).strip()
        if not body:
            if name == "Real cases":
                raise FactoryInputError("at least one public success case is required")
            raise FactoryInputError(f"section must not be empty: {name}")
        result.append((name, body))
    return title, result


def _nested_blocks(body: str, prefix: str) -> list[tuple[str, dict[str, str]]]:
    lines = body.splitlines()
    starts = [(i, line.split(":", 1)[1].strip()) for i, line in enumerate(lines) if line.startswith(f"### {prefix}:")]
    blocks = []
    for pos, (index, key) in enumerate(starts):
        end = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
        chunk = lines[index + 1:end]
        subsections: dict[str, str] = {}
        substarts = [(i, line[5:].strip()) for i, line in enumerate(chunk) if line.startswith("#### ")]
        for subpos, (subindex, name) in enumerate(substarts):
            subend = substarts[subpos + 1][0] if subpos + 1 < len(substarts) else len(chunk)
            value = "\n".join(chunk[subindex + 1:subend]).strip()
            if not value:
                raise FactoryInputError(f"empty nested subsection: {prefix} {key} / {name}")
            if name in subsections:
                raise FactoryInputError(
                    f"duplicate nested subsection: {prefix} {key} / {name}"
                )
            subsections[name] = value
        blocks.append((key, subsections))
    return blocks


def _parse_agents(body: str) -> tuple[RequestedAgent, ...]:
    result = []
    for key, values in _nested_blocks(body, "Agent"):
        handoffs = tuple(item for item in _items(values.get("Handoffs", "- none")) if item != "none")
        integrations = tuple(item for item in _items(values.get("Integrations", "- none")) if item != "none")
        result.append(RequestedAgent(agent_key=key, purpose=_paragraph(values.get("Purpose", "")), responsibilities=_items(values.get("Responsibilities", "")), input_schema_markdown=_paragraph(values.get("Input schema", "")), output_schema_markdown=_paragraph(values.get("Output schema", "")), handoffs=handoffs, prompt_requirements=_items(values.get("Prompt requirements", "")), integration_keys=integrations, n8n_requirement=_paragraph(values.get("n8n requirement", "")), success_metrics=_items(values.get("Success metrics", "")), real_cases=_cases(values.get("Real cases", ""))))
    if not result:
        raise FactoryInputError("at least one agent is required")
    return tuple(result)


def _parse_integrations(body: str) -> tuple[RequestedIntegration, ...]:
    result = []
    for key, values in _nested_blocks(body, "Integration"):
        requirement = _paragraph(values.get("Requirement", ""))
        if requirement not in {"required", "optional"}:
            raise FactoryInputError(f"integration {key} requirement must be required or optional")
        aliases = tuple(item for item in _items(values.get("Credential aliases", "- none")) if item != "none")
        result.append(RequestedIntegration(integration_key=key, purpose=_paragraph(values.get("Purpose", "")), trigger=_paragraph(values.get("Trigger", "")), operation=_paragraph(values.get("Operation", "")), required=requirement == "required", credential_aliases=aliases, success_behavior=_paragraph(values.get("Success behavior", "")), failure_behavior=_paragraph(values.get("Failure behavior", ""))))
    return tuple(result)


def _validate_handoff_dag(agents: tuple[RequestedAgent, ...]) -> None:
    known = {agent.agent_key for agent in agents}
    for agent in agents:
        unknown = set(agent.handoffs) - known
        if unknown:
            raise FactoryInputError(f"unknown handoff: {sorted(unknown)[0]}")
    graph = {agent.agent_key: set(agent.handoffs) for agent in agents}
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise FactoryInputError("cyclic handoff declaration") from exc


def _paragraph(value: str) -> str:
    normalized = " ".join(line.strip() for line in value.splitlines() if line.strip())
    if not normalized:
        raise FactoryInputError("required nested value must not be blank")
    return normalized


def _items(value: str) -> tuple[str, ...]:
    items = tuple(line.strip()[2:].strip() for line in value.splitlines() if line.strip().startswith(("- ", "* ")))
    if not items:
        raise FactoryInputError("section must contain list items")
    return items


def _cases(value: str) -> tuple[RealCaseRequirement, ...]:
    cases = []
    for item in _items(value):
        parts = tuple(part.strip() for part in item.split("|"))
        if len(parts) != 4:
            raise FactoryInputError("success case must contain key, setup, action, and expected outcome")
        cases.append(RealCaseRequirement(case_key=parts[0], observable_setup=parts[1], observable_action=parts[2], observable_expected=parts[3]))
    return tuple(cases)
