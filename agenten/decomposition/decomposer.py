"""DecomposerAgent: turns a ``ProblemSubmitted`` or ``EscalateToRedecompose``
event into zero or more ``SubproblemProposed`` events.

The actual "how to split this problem" logic is injected via
``llm_decompose`` so this module needs no AutoGen import: a real
implementation (an AutoGen ``AssistantAgent`` doing tool-calling) is wired
in later by the integration unit (U11); unit tests here inject a fake
coroutine instead.

Before publishing, three LOCAL caps are enforced (see class docstring):
depth, fanout, and a "progress" invariant that rejects candidates that
don't actually shrink the problem. The total-subproblem budget
(``DecompositionBudget.max_total_subproblems``) is deliberately NOT
enforced here — it is reserved atomically downstream by the Ledger
Recorder (unit U8) to avoid a check-then-act race across concurrent
decomposition batches for the same root problem.
"""
import logging
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from agenten.decomposition.budget import DecompositionBudget
from agenten.events.schemas import (
    EscalateToRedecompose,
    ProblemSubmitted,
    SubproblemProposed,
    make_meta,
    topic_for,
)
from agenten.runtime.event_bus import EventBus

logger = logging.getLogger(__name__)

# (description, depth) -> candidate children, each a dict with keys
# "description" (str), "capability_tags" (list[str], optional) and
# "atomic" (bool, optional).
LlmDecompose = Callable[[str, int], Awaitable[List[Dict[str, Any]]]]

# subproblem_id -> (description, depth) for the subproblem being
# re-decomposed after an EscalateToRedecompose.
DescribeSubproblem = Callable[[str], Awaitable[Tuple[str, int]]]


class DecomposerAgent:
    """Decomposes a problem (or a failed subproblem being re-decomposed)
    into candidate children, enforcing local budget caps before
    publishing each accepted child as a ``SubproblemProposed`` event.
    """

    def __init__(
        self,
        bus: EventBus,
        budget: DecompositionBudget,
        llm_decompose: LlmDecompose,
        describe_subproblem: Optional[DescribeSubproblem] = None,
    ) -> None:
        self._bus = bus
        self._budget = budget
        self._llm_decompose = llm_decompose
        # Only required if handle_escalate_to_redecompose is actually used.
        self._describe_subproblem = describe_subproblem

    async def handle_problem_submitted(self, event: ProblemSubmitted) -> None:
        """A fresh problem always starts decomposition at depth=0."""
        budget = event.budget if event.budget is not None else self._budget
        await self._decompose(
            description=event.description,
            depth=0,
            parent_id=None,
            root_problem_id=event.meta.root_problem_id,
            budget=budget,
        )

    async def handle_escalate_to_redecompose(self, event: EscalateToRedecompose) -> None:
        """A subproblem that failed too many times gets decomposed further
        instead of being retried as-is. Needs a lookup of the failed
        subproblem's current description/depth, injected via
        ``describe_subproblem`` so this class stays decoupled from the
        ledger.
        """
        if self._describe_subproblem is None:
            raise RuntimeError(
                "DecomposerAgent.handle_escalate_to_redecompose requires "
                "describe_subproblem to be provided at construction time"
            )
        description, depth = await self._describe_subproblem(event.subproblem_id)
        await self._decompose(
            description=description,
            depth=depth,
            parent_id=event.subproblem_id,
            root_problem_id=event.meta.root_problem_id,
            budget=self._budget,
        )

    async def _decompose(
        self,
        *,
        description: str,
        depth: int,
        parent_id: Optional[str],
        root_problem_id: str,
        budget: DecompositionBudget,
    ) -> None:
        # Cap 1: depth. `depth` here is the depth of the node currently
        # being decomposed (0 for a fresh problem); children get depth+1
        # below. Once the node being decomposed is at (or past) max_depth,
        # no further recursion is allowed — every candidate is forced
        # atomic regardless of what the LLM returned, so recursion stops
        # instead of producing depth+1 children beyond the cap.
        force_atomic = depth >= budget.max_depth

        candidates = await self._llm_decompose(description, depth)

        # Cap 2: fanout. Take at most max_fanout_per_node candidates; log
        # (don't silently drop) how many were truncated. The Gatekeeper
        # (a sibling unit) handles formal fanout_exceeded rejection
        # bookkeeping if it ever sees more than the cap, but we should
        # never hand it more than the cap in the first place.
        accepted_candidates = candidates[: budget.max_fanout_per_node]
        truncated = len(candidates) - len(accepted_candidates)
        if truncated > 0:
            logger.warning(
                "DecomposerAgent: truncating %d of %d candidate subproblems "
                "(fanout cap=%d) for parent_id=%r depth=%d",
                truncated,
                len(candidates),
                budget.max_fanout_per_node,
                parent_id,
                depth,
            )

        for candidate in accepted_candidates:
            child_description = candidate.get("description", "")
            atomic = True if force_atomic else bool(candidate.get("atomic", False))

            # Cap 3: progress invariant. Defense in depth against
            # degenerate non-decompositions — an LLM re-stating the same
            # (or a longer) problem as its own "subproblem" must not be
            # forwarded, unless it's explicitly marked atomic (a leaf).
            if not atomic and len(child_description) > len(description):
                logger.warning(
                    "DecomposerAgent: dropping non-shrinking candidate "
                    "subproblem for parent_id=%r depth=%d (description not "
                    "atomic and not shorter than parent description)",
                    parent_id,
                    depth,
                )
                continue

            subproblem_id = str(uuid.uuid4())
            meta = make_meta(
                correlation_id=subproblem_id,
                root_problem_id=root_problem_id,
                attempt=0,
            )
            proposed = SubproblemProposed(
                meta=meta,
                subproblem_id=subproblem_id,
                parent_id=parent_id,
                depth=depth + 1,
                description=child_description,
                capability_tags=list(candidate.get("capability_tags") or []),
                atomic=atomic,
            )
            await self._bus.publish(topic_for(SubproblemProposed), proposed)
