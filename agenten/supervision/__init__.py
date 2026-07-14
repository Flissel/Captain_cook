"""Supervision: retry/backoff/circuit-breaker logic for the event-driven
supply-chain pipeline. See `supervisor.py` for `SupervisorAgent`.
"""
from agenten.supervision.supervisor import SupervisorAgent

__all__ = ["SupervisorAgent"]
