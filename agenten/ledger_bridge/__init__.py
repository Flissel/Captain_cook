"""Glue between the event-driven pipeline and the blockchain ledger.

agenten/ledger_bridge/stage_machine.py (this package, unit U0): the pipeline
stage enum + transition validation + the LedgerQuery read-side interface.
agenten/ledger_bridge/recorder.py / query.py (unit U8): the sole ledger
writer and its concrete LedgerQuery implementation.
agenten/ledger_bridge/recovery.py (unit U9): startup recovery.
"""
from .stage_machine import ALLOWED_TRANSITIONS, TERMINAL_STAGES, LedgerQuery, Stage, validate_transition

__all__ = ["ALLOWED_TRANSITIONS", "TERMINAL_STAGES", "LedgerQuery", "Stage", "validate_transition"]
