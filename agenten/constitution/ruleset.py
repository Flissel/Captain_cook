"""ConstitutionRuleset: the shared ruleset every spawned agent inherits.

The dataclass and `load_constitution` signature are frozen here (unit U0).
Unit U2 (agenten/constitution/gatekeeper.py + default_constitution.yaml)
extends `load_constitution` to actually read a YAML ruleset file instead of
returning the hardcoded default below — callers should not depend on the
default's exact wording, only on the shape of ConstitutionRuleset.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from agenten.decomposition.budget import DecompositionBudget

DEFAULT_CONSTITUTION_VERSION = "v0-default"


@dataclass(frozen=True)
class ConstitutionRuleset:
    version: str
    scope_statement: str
    quality_rubric: str
    prohibited_topics: List[str] = field(default_factory=list)
    default_budget: DecompositionBudget = field(default_factory=DecompositionBudget)


def load_constitution(path: Optional[str] = None) -> ConstitutionRuleset:
    """Load a ConstitutionRuleset.

    Unit U0's implementation ignores `path` and returns a permissive
    built-in default so downstream units are unblocked; unit U2 replaces
    this body with real YAML loading (default_constitution.yaml) while
    keeping this signature.
    """
    return ConstitutionRuleset(
        version=DEFAULT_CONSTITUTION_VERSION,
        scope_statement="Accept any subproblem that is a concrete, actionable step toward the root problem.",
        quality_rubric="A subproblem must have a clear title, a description, and be independently verifiable.",
        prohibited_topics=[],
        default_budget=DecompositionBudget(),
    )
