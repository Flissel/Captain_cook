"""Capability-based agent spawning: which specialized agent TYPE should
handle a subproblem, resolved by tag rather than hardcoded per-workflow
wiring (see agenten/spawning/coordinator.py, unit U4).
"""
from .capability_registry import CapabilityRegistry, NoCapableAgentType

__all__ = ["CapabilityRegistry", "NoCapableAgentType"]
