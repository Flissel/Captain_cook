"""Constitution: a versioned ruleset every spawned agent inherits, enforced
mechanically by ConstitutionGatekeeper agents (unit U2,
agenten/constitution/gatekeeper.py).
"""
from .ruleset import ConstitutionRuleset, load_constitution

__all__ = ["ConstitutionRuleset", "load_constitution"]
