"""ConstitutionRuleset: the shared ruleset every spawned agent inherits.

The dataclass and `load_constitution` signature are frozen here (unit U0).
Unit U2 (agenten/constitution/gatekeeper.py + default_constitution.yaml)
extends `load_constitution` to actually read a YAML ruleset file instead of
returning the hardcoded default below — callers should not depend on the
default's exact wording, only on the shape of ConstitutionRuleset.
"""
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from agenten.decomposition.budget import DecompositionBudget

DEFAULT_CONSTITUTION_VERSION = "v0-default"

_DEFAULT_CONSTITUTION_FILENAME = "default_constitution.yaml"


@dataclass(frozen=True)
class ConstitutionRuleset:
    version: str
    scope_statement: str
    quality_rubric: str
    prohibited_topics: List[str] = field(default_factory=list)
    default_budget: DecompositionBudget = field(default_factory=DecompositionBudget)


def load_constitution(path: Optional[str] = None) -> ConstitutionRuleset:
    """Load a ConstitutionRuleset from a YAML file.

    Defaults to `default_constitution.yaml` next to this module when `path`
    is not given. The YAML shape mirrors the dataclass fields directly:

        version: "v1-default"
        scope_statement: "..."
        quality_rubric: "..."
        prohibited_topics: [...]
        default_budget:
          max_depth: 4
          max_total_subproblems: 200
          max_fanout_per_node: 6
          max_tokens: null

    Missing optional keys fall back to the dataclass/DecompositionBudget
    defaults so a minimal YAML file is still valid.
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), _DEFAULT_CONSTITUTION_FILENAME)

    with open(path, "r", encoding="utf-8") as fh:
        raw: Dict[str, Any] = yaml.safe_load(fh) or {}

    budget_raw: Dict[str, Any] = raw.get("default_budget") or {}
    default_budget_fields = {
        "max_depth": budget_raw.get("max_depth", DecompositionBudget().max_depth),
        "max_total_subproblems": budget_raw.get(
            "max_total_subproblems", DecompositionBudget().max_total_subproblems
        ),
        "max_fanout_per_node": budget_raw.get(
            "max_fanout_per_node", DecompositionBudget().max_fanout_per_node
        ),
        "max_tokens": budget_raw.get("max_tokens", DecompositionBudget().max_tokens),
    }

    return ConstitutionRuleset(
        version=raw.get("version", DEFAULT_CONSTITUTION_VERSION),
        scope_statement=raw.get("scope_statement", ""),
        quality_rubric=raw.get("quality_rubric", ""),
        prohibited_topics=list(raw.get("prohibited_topics") or []),
        default_budget=DecompositionBudget(**default_budget_fields),
    )
