"""Supervision package: watchdogs / lifecycle guards for the supply-chain
pipeline.

- `supervisor.py` (unit U6): SupervisorAgent — retry/backoff/escalation and
  per-agent_type circuit breaking.
- `reaper.py` (unit U7): lease/heartbeat watchdog.
"""
from agenten.supervision.supervisor import SupervisorAgent

__all__ = ["SupervisorAgent"]
