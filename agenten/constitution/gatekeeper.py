"""ConstitutionGatekeeper: independently judges every proposed subproblem.

Event flow (shared spec across units U2/U3/U4/U8/U9 — do not deviate):
`DecomposerAgent` (unit U3) publishes `SubproblemProposed` for each candidate
child. Two independent subscribers react to it: (a) the Ledger Recorder
(unit U8) handles budget reservation and the QUEUED -> VALIDATING ledger
write — not this module's concern, it has no BudgetLedger dependency; (b)
`ConstitutionGatekeeper` (this module) independently runs a two-layer check
and publishes either `SubproblemAccepted` (with `block_index=None` — the
Recorder fills that in later when it applies this verdict) or
`SubproblemRejected`, based on its verdict alone.

This module has no `autogen_core` import in its core class — it depends only
on `EventBus`/`LedgerQuery` ports (agenten/runtime/event_bus.py,
agenten/ledger_bridge/stage_machine.py) — so `ConstitutionGatekeeper` stays
importable and unit-testable with zero AutoGen installed. The optional
`GatekeeperRoutedAgent` adapter at the bottom of this file is the only piece
that touches `autogen_core`, and it is gated behind a soft import so this
module still imports cleanly when `autogen_core` is absent.
"""
import asyncio
import logging
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from agenten.constitution.ruleset import ConstitutionRuleset
from agenten.constitution.validators import run_deterministic_checks
from agenten.events.schemas import (
    SubproblemAccepted,
    SubproblemProposed,
    SubproblemRejected,
    make_meta,
    topic_for,
)
from agenten.ledger_bridge.stage_machine import LedgerQuery, Stage
from agenten.runtime.event_bus import EventBus

logger = logging.getLogger(__name__)

LlmJudge = Callable[[str, ConstitutionRuleset], Awaitable[bool]]


def _field_from_block(block: Any, key: str) -> Optional[Any]:
    """Best-effort lookup of `key` on a ledger Block: block shape (beyond
    index/data/metadata) is owned by unit U8, so this checks `data` first,
    then `metadata`, and tolerates blocks that don't have the attribute at
    all rather than raising.
    """
    data = getattr(block, "data", None) or {}
    if key in data:
        return data[key]
    metadata = getattr(block, "metadata", None) or {}
    if key in metadata:
        return metadata[key]
    return None


class ConstitutionGatekeeper:
    """Two-layer admission check for proposed subproblems.

    Layer 1 (validators.py): deterministic, no LLM, no flakiness — malformed
    input, non-minimal descriptions, and cheap duplicate detection.

    Layer 2 (only if layer 1 passes): a semantic in-scope/quality check
    against `ruleset.scope_statement` / `ruleset.quality_rubric` /
    `ruleset.prohibited_topics`, delegated to an injected async
    `llm_judge` callable so this class never imports an LLM/AutoGen
    dependency directly. The call is wrapped with a timeout; on timeout or
    on any exception raised by the judge, the subproblem is rejected
    conservatively — this gate never silently admits and never hangs.

    If no `llm_judge` is supplied at all (the default, `None`), layer 2 is
    skipped and a subproblem that passes layer 1 is accepted. This is a
    deliberate escape hatch for running without an LLM configured (e.g.
    local dev, or tests that only exercise layer-1 rejections) — it is NOT
    the same code path as a judge that times out or raises, both of which
    reject.
    """

    def __init__(
        self,
        bus: EventBus,
        ruleset: ConstitutionRuleset,
        ledger_query: LedgerQuery,
        llm_judge: Optional[LlmJudge] = None,
        llm_timeout_seconds: float = 15.0,
    ):
        self.bus = bus
        self.ruleset = ruleset
        self.ledger_query = ledger_query
        self.llm_judge = llm_judge
        self.llm_timeout_seconds = llm_timeout_seconds

    async def handle_subproblem_proposed(self, event: SubproblemProposed) -> None:
        """Publishes SubproblemAccepted or SubproblemRejected via self.bus."""
        root_problem_id = event.meta.root_problem_id

        parent_description = self._lookup_parent_description(event.parent_id)
        pending_descriptions = self._collect_pending_descriptions(exclude_subproblem_id=event.subproblem_id)

        failure = run_deterministic_checks(
            description=event.description,
            capability_tags=list(event.capability_tags),
            root_problem_id=root_problem_id,
            parent_description=parent_description,
            pending_descriptions=pending_descriptions,
        )
        if failure is not None:
            reason, detail = failure
            await self._reject(event, reason, detail)
            return

        if self.llm_judge is not None:
            passed = await self._run_llm_judge(event.description)
            if not passed:
                await self._reject(event, "quality_bar", "semantic scope/quality check did not pass")
                return

        await self._accept(event)

    async def _run_llm_judge(self, description: str) -> bool:
        assert self.llm_judge is not None
        try:
            return await asyncio.wait_for(
                self.llm_judge(description, self.ruleset), timeout=self.llm_timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.warning("llm_judge timed out after %ss — rejecting conservatively", self.llm_timeout_seconds)
            return False
        except Exception:
            logger.exception("llm_judge raised — rejecting conservatively")
            return False

    def _lookup_parent_description(self, parent_id: Optional[str]) -> Optional[str]:
        if parent_id is None:
            return None
        for stage in Stage:
            try:
                blocks = self.ledger_query.blocks_in_stage(stage)
            except Exception:
                continue
            for block in blocks:
                if _field_from_block(block, "subproblem_id") == parent_id:
                    description = _field_from_block(block, "description")
                    if isinstance(description, str):
                        return description
        return None

    def _collect_pending_descriptions(self, exclude_subproblem_id: str) -> List[Tuple[str, str]]:
        pending: List[Tuple[str, str]] = []
        try:
            blocks = self.ledger_query.blocks_in_stage(Stage.VALIDATING)
        except Exception:
            return pending
        for block in blocks:
            if _field_from_block(block, "subproblem_id") == exclude_subproblem_id:
                continue
            block_root_id = _field_from_block(block, "root_problem_id")
            block_description = _field_from_block(block, "description")
            if isinstance(block_root_id, str) and isinstance(block_description, str):
                pending.append((block_root_id, block_description))
        return pending

    async def _accept(self, event: SubproblemProposed) -> None:
        accepted = SubproblemAccepted(
            meta=make_meta(
                correlation_id=event.subproblem_id,
                root_problem_id=event.meta.root_problem_id,
                attempt=event.meta.attempt,
                constitution_version=self.ruleset.version,
            ),
            subproblem_id=event.subproblem_id,
            block_index=None,
        )
        await self.bus.publish(topic_for(SubproblemAccepted), accepted)

    async def _reject(self, event: SubproblemProposed, reason: str, detail: str) -> None:
        rejected = SubproblemRejected(
            meta=make_meta(
                correlation_id=event.subproblem_id,
                root_problem_id=event.meta.root_problem_id,
                attempt=event.meta.attempt,
                constitution_version=self.ruleset.version,
            ),
            subproblem_id=event.subproblem_id,
            reason=reason,  # type: ignore[arg-type]
            detail=detail,
        )
        await self.bus.publish(topic_for(SubproblemRejected), rejected)


try:
    import autogen_core
    from autogen_core import MessageContext, RoutedAgent, message_handler
except ImportError:  # pragma: no cover - exercised by the no-autogen_core smoke test
    autogen_core = None


if autogen_core is not None:  # pragma: no cover - requires autogen_core to be installed

    class GatekeeperRoutedAgent(RoutedAgent):
        """Thin AutoGen Core adapter: forwards `SubproblemProposed` messages
        delivered by the runtime's pub/sub into a plain `ConstitutionGatekeeper`
        instance. Kept intentionally minimal — all actual judging logic lives
        in `ConstitutionGatekeeper` so it can be unit-tested without AutoGen.
        """

        def __init__(self, gatekeeper: ConstitutionGatekeeper, description: str = "Constitution gatekeeper"):
            super().__init__(description)
            self._gatekeeper = gatekeeper

        @message_handler
        async def on_subproblem_proposed(self, message: SubproblemProposed, ctx: MessageContext) -> None:
            await self._gatekeeper.handle_subproblem_proposed(message)
