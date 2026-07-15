"""Load and validate the portable role definitions in ``agents/household``.

The Markdown files are the human-editable source of truth.  This module gives
the runtime a small, typed view of the parts it needs for deterministic
routing, without treating the Markdown prompt as executable configuration.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


class HouseholderRoleError(ValueError):
    """Raised when a portable householder definition is incomplete or unsafe."""


@dataclass(frozen=True)
class HouseholderRoleSpec:
    """The runtime contract for exactly one documented householder role."""

    role_id: str
    agent_type: str
    capability_tags: tuple[str, ...]
    prompt_path: Path
    permitted_tools: tuple[str, ...]


_ROLE_RUNTIME_DEFAULTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "architect": ("householder_architect", ("architecture_review",)),
    "ledger-steward": ("householder_ledger_steward", ("ledger_review",)),
    "delivery-builder": ("householder_delivery_builder", ("delivery_plan",)),
    "quality-warden": ("householder_quality_warden", ("quality_review",)),
}


def _default_definition_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "agents" / "household"


def _parse_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise HouseholderRoleError(f"{path}: YAML frontmatter must start on the first line")

    try:
        closing_index = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise HouseholderRoleError(f"{path}: YAML frontmatter has no closing delimiter") from exc

    values: dict[str, str] = {}
    for line in lines[1:closing_index]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator or not key.strip() or not value.strip():
            raise HouseholderRoleError(f"{path}: malformed frontmatter entry {line!r}")
        values[key.strip()] = value.strip()
    return values


def _parse_tools(path: Path, value: str) -> tuple[str, ...]:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError) as exc:
        raise HouseholderRoleError(f"{path}: tools must be a literal list of strings") from exc
    if not isinstance(parsed, list) or not parsed or not all(isinstance(tool, str) and tool for tool in parsed):
        raise HouseholderRoleError(f"{path}: tools must be a non-empty list of strings")
    return tuple(parsed)


def load_householder_roles(definition_dir: Path | None = None) -> tuple[HouseholderRoleSpec, ...]:
    """Return every supported role definition in deterministic filename order.

    Unknown Markdown files fail closed.  Adding a new role therefore requires
    an explicit routing decision in this module, rather than silently making a
    newly written prompt runnable with an accidental capability.
    """
    source_dir = definition_dir if definition_dir is not None else _default_definition_dir()
    if not source_dir.is_dir():
        raise HouseholderRoleError(f"Householder definition directory does not exist: {source_dir}")

    roles: list[HouseholderRoleSpec] = []
    for path in sorted(source_dir.glob("*.md")):
        role_id = path.stem
        frontmatter = _parse_frontmatter(path)
        if role_id not in _ROLE_RUNTIME_DEFAULTS:
            raise HouseholderRoleError(f"{path}: no runtime contract is registered for role {role_id!r}")

        for required in ("name", "description", "tools"):
            if not frontmatter.get(required):
                raise HouseholderRoleError(f"{path}: required frontmatter field {required!r} is missing")

        agent_type, capability_tags = _ROLE_RUNTIME_DEFAULTS[role_id]
        prompt_path = (
            Path("agents") / "household" / path.name
            if definition_dir is None
            else path
        )
        roles.append(
            HouseholderRoleSpec(
                role_id=role_id,
                agent_type=agent_type,
                capability_tags=capability_tags,
                prompt_path=prompt_path,
                permitted_tools=_parse_tools(path, frontmatter["tools"]),
            )
        )

    if not roles:
        raise HouseholderRoleError(f"No householder role definitions found in {source_dir}")
    return tuple(roles)
