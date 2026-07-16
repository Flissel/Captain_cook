"""Deterministic policy checks for Captain-generated planning content."""

import hashlib
import json
from typing import TYPE_CHECKING

from agenten.validation.contracts import ExampleCase

if TYPE_CHECKING:
    from agenten.planning.captain_pipeline import BatchEnrichment


class PlanningPolicyError(ValueError):
    """An enrichment violates a deterministic Captain planning rule."""


class PlanningPolicy:
    """Validate LLM enrichment against configured, deterministic policy."""

    def __init__(self, allowed_capability_tags: frozenset[str]) -> None:
        self.allowed_capability_tags = allowed_capability_tags

    @staticmethod
    def fingerprint_case(case: ExampleCase) -> str:
        """Return a canonical content fingerprint independent of ``case_id``."""

        payload = case.model_dump(mode="json", exclude={"case_id"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def validate_enrichment(self, enrichment: "BatchEnrichment") -> None:
        """Reject unknown capability tags and visible/hidden content overlap."""

        unknown = sorted(
            set(enrichment.capability_tags) - self.allowed_capability_tags
        )
        if unknown:
            raise PlanningPolicyError(f"unknown capability tags: {unknown}")

        visible = {
            self.fingerprint_case(case) for case in enrichment.golden_cases
        }
        hidden = {
            self.fingerprint_case(case) for case in enrichment.holdout_cases
        }
        if visible & hidden:
            raise PlanningPolicyError(
                "holdout content overlaps build-visible golden content"
            )
