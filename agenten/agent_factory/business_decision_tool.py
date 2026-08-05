"""Deterministic public-policy tool for generated business agent teams."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping


TOOL_NAME = "captain_business_decision"
_REQUIRED_TASK_KEYS = {
    "schema",
    "case_id",
    "profile_id",
    "redacted_input",
    "allowed_tool_intents",
    "required_output_schema",
}
_OPTIONAL_TASK_KEYS = {"allowed_tools"}
_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def captain_business_decision(task_json: str) -> str:
    """Return the canonical decision for one Captain-redacted public task."""

    task = _load_task(task_json)
    profile_id = task.get("profile_id")
    facts = task["redacted_input"]
    if profile_id == "insurance_claims_resolution_swarm":
        decision, rationale = _claims_decision(facts)
    elif profile_id == "customer_renewal_orchestration_team":
        decision, rationale = _renewal_decision(facts)
    else:
        raise ValueError("business decision profile is unsupported")
    return json.dumps(
        {
            "schema": "captain.business-benchmark-terminal.v1",
            "observed_decision": decision,
            "observed_rationale_fact_ids": list(rationale),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def bind_captain_business_decision(
    expected_task_json: str,
) -> Callable[[str], str]:
    """Bind the tool to one exact Captain-redacted task value."""

    expected = _load_task(expected_task_json)

    def bound(task_json: str) -> str:
        if _load_task(task_json) != expected:
            raise ValueError("business decision task does not match Captain scope")
        return captain_business_decision(task_json)

    bound.__name__ = TOOL_NAME
    bound.__doc__ = captain_business_decision.__doc__
    return bound


def captain_business_decision_task_matches(
    task_json: str,
    expected_task_json: str,
) -> bool:
    """Return whether a proposed tool argument matches Captain's task value."""

    try:
        return _load_task(task_json) == _load_task(expected_task_json)
    except ValueError:
        return False


def _load_task(task_json: str) -> dict[str, object]:
    try:
        task = json.loads(task_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("business decision task must be strict JSON") from exc
    if not isinstance(task, dict):
        raise ValueError("business decision task contract is invalid")
    keys = set(task)
    intents = task.get("allowed_tool_intents")
    if (
        not _REQUIRED_TASK_KEYS.issubset(keys)
        or keys - _REQUIRED_TASK_KEYS - _OPTIONAL_TASK_KEYS
        or task.get("schema") != "captain.business-benchmark-redacted-task.v1"
        or task.get("required_output_schema")
        != "captain.business-benchmark-terminal.v1"
        or not isinstance(task.get("case_id"), str)
        or not task["case_id"]
        or not isinstance(task.get("profile_id"), str)
        or not isinstance(intents, list)
        or not intents
        or any(
            not isinstance(item, str) or item not in {"none", "n8n"}
            for item in intents
        )
        or len(intents) != len(set(intents))
        or not isinstance(task.get("redacted_input"), dict)
        or not task["redacted_input"]
    ):
        raise ValueError("business decision task contract is invalid")
    allowed_tools = task.get("allowed_tools")
    if allowed_tools is not None and (
        not isinstance(allowed_tools, list)
        or TOOL_NAME not in allowed_tools
        or any(
            not isinstance(item, str) or _TOOL_NAME.fullmatch(item) is None
            for item in allowed_tools
        )
        or len(allowed_tools) != len(set(allowed_tools))
    ):
        raise ValueError("business decision task tool allowlist is invalid")
    return task


def _claims_decision(facts: Mapping[str, object]) -> tuple[str, tuple[str, str]]:
    required = {
        "coverage_state",
        "documentation_state",
        "loss_band",
        "reported_delay_band",
    }
    if not required.issubset(facts):
        raise ValueError("claims decision facts are incomplete")
    values = tuple(str(facts[key]) for key in sorted(required))
    if any("conflict" in value for value in values):
        return "escalate_coverage", (
            "evidence_conflict_detected",
            "specialist_review_required",
        )
    if (
        facts["coverage_state"] == "specialist_interpretation_required"
        or facts["loss_band"] == "critical_complexity"
        or str(facts["reported_delay_band"]).startswith("escalation_trigger_")
    ):
        return "escalate_coverage", (
            "critical_coverage_question_detected",
            "human_authority_required",
        )
    if (
        facts["coverage_state"] == "active_near_boundary"
        and facts["documentation_state"] == "complete"
        and facts["loss_band"] == "upper_standard_boundary"
        and str(facts["reported_delay_band"]).startswith("boundary_variant_")
    ):
        return "route_standard_review", (
            "boundary_condition_identified",
            "coverage_state_verified",
        )
    if (
        facts["coverage_state"] != "active"
        or facts["documentation_state"] != "complete"
    ):
        return "request_information", (
            "required_evidence_missing",
            "decision_deferred",
        )
    if (
        str(facts["loss_band"]).startswith("boundary_")
        or facts["reported_delay_band"] == "near_boundary"
    ):
        return "route_standard_review", (
            "boundary_condition_identified",
            "coverage_state_verified",
        )
    return "route_standard_review", (
        "coverage_state_verified",
        "evidence_complete",
    )


def _renewal_decision(facts: Mapping[str, object]) -> tuple[str, tuple[str, str]]:
    required = {
        "renewal_window",
        "engagement_band",
        "commercial_evidence_state",
        "consent_state",
    }
    if not required.issubset(facts):
        raise ValueError("renewal decision facts are incomplete")
    values = tuple(str(facts[key]) for key in sorted(required))
    if any("conflict" in value for value in values):
        return "human_commercial_review", (
            "commercial_conflict_detected",
            "human_review_required",
        )
    if (
        facts["renewal_window"] == "executive_review_required"
        or facts["engagement_band"] == "strategic_risk"
        or str(facts["commercial_evidence_state"]).startswith("authority_trigger_")
    ):
        return "human_commercial_review", (
            "strategic_authority_threshold_met",
            "human_commercial_authority_required",
        )
    if (
        facts["consent_state"] != "verified"
        or facts["engagement_band"] == "undetermined"
        or str(facts["commercial_evidence_state"]).startswith(
            "missing_required_signal_"
        )
    ):
        return "request_information", (
            "required_signal_missing",
            "action_deferred",
        )
    if (
        str(facts["renewal_window"]).startswith("boundary_")
        or facts["engagement_band"] == "threshold"
    ):
        return "propose_next_best_action", (
            "commercial_boundary_identified",
            "next_action_bounded",
        )
    if (
        facts["renewal_window"] != "open"
        or facts["commercial_evidence_state"] != "complete"
    ):
        raise ValueError("renewal decision facts are unsupported")
    return "propose_next_best_action", (
        "renewal_window_verified",
        "next_action_supported",
    )


__all__ = [
    "TOOL_NAME",
    "bind_captain_business_decision",
    "captain_business_decision",
    "captain_business_decision_task_matches",
]
