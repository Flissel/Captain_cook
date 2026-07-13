"""Layer-1 (deterministic, no LLM) checks for a proposed subproblem.

These functions are intentionally small and standalone: no network, no
randomness, no dependency on autogen_core or the event bus, so they run in
microseconds and never flake. `ConstitutionGatekeeper` (gatekeeper.py) calls
them in order and rejects on the first failure before ever touching the LLM
judge.

Each validator returns `None` on success or a `(reason, detail)` tuple ready
to hand straight to `SubproblemRejected(reason=..., detail=...)` on failure,
using the `RejectionReason` literal values from `agenten.events.schemas`.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

from agenten.events.schemas import RejectionReason

# Reasonable bounds for a "minimal" subproblem description. These are
# deliberately generous — layer 1 exists to catch garbage input (empty,
# truncated, absurdly long), not to make judgment calls; that's layer 2's job.
MIN_DESCRIPTION_LENGTH = 8
MAX_DESCRIPTION_LENGTH = 4000
MAX_CAPABILITY_TAGS = 20

ValidationFailure = Tuple[RejectionReason, str]


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace, for cheap near-duplicate comparison."""
    return re.sub(r"\s+", " ", text.strip().lower())


def check_required_fields(description: str, capability_tags: List[str]) -> Optional[ValidationFailure]:
    """description must be a non-empty, reasonably-sized string; capability_tags
    must be a non-empty list of strings.
    """
    if not isinstance(description, str) or not description.strip():
        return ("malformed", "description is missing or empty")
    if len(description.strip()) < MIN_DESCRIPTION_LENGTH:
        return ("malformed", f"description shorter than {MIN_DESCRIPTION_LENGTH} chars")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        return ("malformed", f"description longer than {MAX_DESCRIPTION_LENGTH} chars")

    if not isinstance(capability_tags, list) or len(capability_tags) == 0:
        return ("malformed", "capability_tags must be a non-empty list")
    if len(capability_tags) > MAX_CAPABILITY_TAGS:
        return ("malformed", f"capability_tags exceeds {MAX_CAPABILITY_TAGS} entries")
    if not all(isinstance(tag, str) and tag.strip() for tag in capability_tags):
        return ("malformed", "capability_tags must all be non-empty strings")

    return None


def check_minimality(description: str, parent_description: Optional[str]) -> Optional[ValidationFailure]:
    """A "minimal" subproblem should not be longer than the parent it was
    split from. Skipped entirely if the parent description isn't available
    (e.g. root-level subproblems, or the ledger query can't find the parent).
    """
    if parent_description is None:
        return None
    if len(description) > len(parent_description):
        return (
            "malformed",
            "subproblem description is longer than its parent's — not a minimal decomposition",
        )
    return None


def check_duplicate(
    description: str,
    root_problem_id: str,
    pending_descriptions: List[Tuple[str, str]],
) -> Optional[ValidationFailure]:
    """Cheap duplicate check: exact match (after normalization) against other
    subproblems currently pending under the same root problem.

    `pending_descriptions` is a list of (root_problem_id, description) pairs
    gathered by the caller from the ledger's VALIDATING-stage blocks — kept
    as a plain argument here so this function stays a pure, dependency-free
    string comparison that's trivial to unit test.
    """
    normalized = _normalize(description)
    for other_root_id, other_description in pending_descriptions:
        if other_root_id != root_problem_id:
            continue
        if _normalize(other_description) == normalized:
            return ("duplicate", "an identical subproblem is already pending under this root problem")
    return None


def run_deterministic_checks(
    description: str,
    capability_tags: List[str],
    root_problem_id: str,
    parent_description: Optional[str],
    pending_descriptions: List[Tuple[str, str]],
) -> Optional[ValidationFailure]:
    """Run all layer-1 checks in order, short-circuiting on the first failure."""
    failure = check_required_fields(description, capability_tags)
    if failure is not None:
        return failure

    failure = check_minimality(description, parent_description)
    if failure is not None:
        return failure

    failure = check_duplicate(description, root_problem_id, pending_descriptions)
    if failure is not None:
        return failure

    return None
