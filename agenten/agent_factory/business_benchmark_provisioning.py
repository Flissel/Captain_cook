"""Deterministic provisioning for Captain-private business benchmark suites.

The provisioner exposes only digest-bound holdout references.  Case bodies are
available solely through the explicitly private loader after the canonical
profile/version index and the content digest have both been verified.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkSuiteV1,
    BusinessCaseCategory,
    canonical_business_benchmark_model_bytes,
)
from agenten.agent_factory.business_benchmark_store import (
    BusinessBenchmarkConflictError,
    FilesystemBusinessBenchmarkEvidenceStore,
    PrivateBusinessBenchmarkStore,
)
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_runtime.contracts import IDENTIFIER_PATTERN, IntegrationIntent


CLAIMS_PROFILE_ID = "insurance_claims_resolution_swarm"
RENEWAL_PROFILE_ID = "customer_renewal_orchestration_team"
BusinessBenchmarkProfileId = Literal[
    "insurance_claims_resolution_swarm",
    "customer_renewal_orchestration_team",
]
_CANONICAL_CREATED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SECRET_SEED_PATTERN = re.compile(
    r"(?i)(?:^sk-|(?:^|[._:-])(?:api[._:-]?key|authorization|credential|password|secret|token)(?:$|[._:-]))"
)


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ProvisionedBusinessBenchmarkSuiteRefV1(_FrozenContract):
    """Public-safe identity of one privately stored canonical suite."""

    schema_name: Literal["captain.provisioned-business-benchmark-suite-ref.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    profile_id: BusinessBenchmarkProfileId
    suite_version: int = Field(ge=1, strict=True)
    suite_ref: PrivateHoldoutRef


class ProvisionedBusinessBenchmarkSuitesV1(_FrozenContract):
    """Public-safe provisioning result; deliberately contains no case bodies."""

    schema_name: Literal["captain.provisioned-business-benchmark-suites.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    seed_version_id: str = Field(pattern=IDENTIFIER_PATTERN)
    suites: tuple[ProvisionedBusinessBenchmarkSuiteRefV1, ...] = Field(
        min_length=2, max_length=2
    )

    @model_validator(mode="after")
    def require_exact_profiles(self) -> "ProvisionedBusinessBenchmarkSuitesV1":
        profiles = tuple(item.profile_id for item in self.suites)
        if profiles != (CLAIMS_PROFILE_ID, RENEWAL_PROFILE_ID):
            raise ValueError("provisioning result must contain Claims then Renewal")
        if len({item.suite_version for item in self.suites}) != 1:
            raise ValueError("provisioned suites must use one suite version")
        return self


class _CanonicalSuiteIndexV1(_FrozenContract):
    schema_name: Literal["captain.private-business-benchmark-suite-index.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    profile_id: BusinessBenchmarkProfileId
    suite_version: int = Field(ge=1, strict=True)
    seed_version_id: str = Field(pattern=IDENTIFIER_PATTERN)
    suite_ref: PrivateHoldoutRef


def default_private_business_benchmark_root(workspace_root: Path) -> Path:
    """Return the repository-gitignored Captain private benchmark namespace."""

    return workspace_root / ".captain-cook" / "private" / "business-benchmarks"


class CanonicalPrivateBusinessBenchmarkProvisioner:
    """Create each canonical profile/version exactly once under Captain ownership."""

    def __init__(self, root: Path) -> None:
        self._root = _validated_private_root(root)

    def provision(
        self,
        *,
        suite_version: int,
        seed_version_id: str,
    ) -> ProvisionedBusinessBenchmarkSuitesV1:
        _validate_inputs(suite_version=suite_version, seed_version_id=seed_version_id)
        planned: list[
            tuple[
                BusinessBenchmarkProfileId,
                BusinessBenchmarkSuiteV1,
                PrivateHoldoutRef,
            ]
        ] = []
        for profile_id in (CLAIMS_PROFILE_ID, RENEWAL_PROFILE_ID):
            suite = _build_suite(
                profile_id=profile_id,
                suite_version=suite_version,
                seed_version_id=seed_version_id,
            )
            reference = _suite_reference(suite)
            self._preflight_profile_version_binding(
                profile_id=profile_id,
                suite_version=suite_version,
                seed_version_id=seed_version_id,
                reference=reference,
            )
            planned.append((profile_id, suite, reference))

        provisioned: list[ProvisionedBusinessBenchmarkSuiteRefV1] = []
        for profile_id, suite, reference in planned:
            suite_store = PrivateBusinessBenchmarkStore.from_fixture(suite, self._root)
            if suite_store.public_suite_ref() != reference:
                raise BusinessBenchmarkConflictError(
                    "private suite store returned an unexpected content reference"
                )
            self._bind_profile_version_once(
                profile_id=profile_id,
                suite_version=suite_version,
                seed_version_id=seed_version_id,
                reference=reference,
            )
            provisioned.append(
                ProvisionedBusinessBenchmarkSuiteRefV1(
                    schema="captain.provisioned-business-benchmark-suite-ref.v1",
                    profile_id=profile_id,
                    suite_version=suite_version,
                    suite_ref=reference,
                )
            )
        return ProvisionedBusinessBenchmarkSuitesV1(
            schema="captain.provisioned-business-benchmark-suites.v1",
            seed_version_id=seed_version_id,
            suites=tuple(provisioned),
        )

    def _preflight_profile_version_binding(
        self,
        *,
        profile_id: BusinessBenchmarkProfileId,
        suite_version: int,
        seed_version_id: str,
        reference: PrivateHoldoutRef,
    ) -> None:
        path = _index_path(self._root, profile_id, suite_version)
        if not path.exists():
            return
        expected = _canonical_model_bytes(
            _CanonicalSuiteIndexV1(
                schema="captain.private-business-benchmark-suite-index.v1",
                profile_id=profile_id,
                suite_version=suite_version,
                seed_version_id=seed_version_id,
                suite_ref=reference,
            )
        )
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise BusinessBenchmarkConflictError(
                "business benchmark profile and version binding cannot be read"
            ) from exc
        if current != expected:
            raise BusinessBenchmarkConflictError(
                "business benchmark profile and version already bind a different canonical suite"
            )

    def _bind_profile_version_once(
        self,
        *,
        profile_id: BusinessBenchmarkProfileId,
        suite_version: int,
        seed_version_id: str,
        reference: PrivateHoldoutRef,
    ) -> None:
        index = _CanonicalSuiteIndexV1(
            schema="captain.private-business-benchmark-suite-index.v1",
            profile_id=profile_id,
            suite_version=suite_version,
            seed_version_id=seed_version_id,
            suite_ref=reference,
        )
        content = _canonical_model_bytes(index)
        try:
            FilesystemBusinessBenchmarkEvidenceStore._write_once(
                _index_path(self._root, profile_id, suite_version), content
            )
        except BusinessBenchmarkConflictError as exc:
            raise BusinessBenchmarkConflictError(
                "business benchmark profile and version already bind a different canonical suite"
            ) from exc


class CaptainPrivateBusinessBenchmarkSuiteLoader:
    """Captain-private body loader with index and content-digest verification."""

    def __init__(self, root: Path) -> None:
        self._root = _validated_private_root(root)

    def load_suite(
        self,
        reference: PrivateHoldoutRef,
        *,
        expected_profile_id: BusinessBenchmarkProfileId,
        expected_suite_version: int,
    ) -> BusinessBenchmarkSuiteV1:
        if not isinstance(reference, PrivateHoldoutRef):
            raise TypeError("private suite loading requires a PrivateHoldoutRef")
        index = self._load_index(expected_profile_id, expected_suite_version)
        if index.suite_ref != reference:
            raise BusinessBenchmarkConflictError(
                "requested reference is not the canonical suite reference for profile and version"
            )
        suite = PrivateBusinessBenchmarkStore(self._root, reference).private_suite(reference)
        if (
            suite.profile_id != expected_profile_id
            or suite.suite_version != expected_suite_version
        ):
            raise BusinessBenchmarkConflictError(
                "private suite body does not match its canonical profile and version"
            )
        return suite

    def load_provisioned_suites(
        self,
        provisioned: ProvisionedBusinessBenchmarkSuitesV1,
    ) -> tuple[BusinessBenchmarkSuiteV1, BusinessBenchmarkSuiteV1]:
        """Resolve the canonical Claims/Renewal pair through their exact references."""

        loaded: list[BusinessBenchmarkSuiteV1] = []
        for item in provisioned.suites:
            index = self._load_index(item.profile_id, item.suite_version)
            if index.seed_version_id != provisioned.seed_version_id:
                raise BusinessBenchmarkConflictError(
                    "provisioned seed does not match the canonical suite reference"
                )
            loaded.append(
                self.load_suite(
                    item.suite_ref,
                    expected_profile_id=item.profile_id,
                    expected_suite_version=item.suite_version,
                )
            )
        if len(loaded) != 2:
            raise BusinessBenchmarkConflictError("canonical benchmark pair is incomplete")
        return loaded[0], loaded[1]

    def _load_index(
        self,
        profile_id: BusinessBenchmarkProfileId,
        suite_version: int,
    ) -> _CanonicalSuiteIndexV1:
        path = _index_path(self._root, profile_id, suite_version)
        if not path.is_file():
            raise BusinessBenchmarkConflictError("canonical suite reference is missing")
        try:
            index = _CanonicalSuiteIndexV1.model_validate_json(path.read_bytes())
        except (OSError, ValidationError, ValueError) as exc:
            raise BusinessBenchmarkConflictError("canonical suite reference is invalid") from exc
        if index.profile_id != profile_id or index.suite_version != suite_version:
            raise BusinessBenchmarkConflictError("canonical suite reference identity is invalid")
        return index


def _validated_private_root(root: Path) -> Path:
    resolved = root.resolve()
    if ".captain-cook" not in {part.lower() for part in resolved.parts}:
        raise ValueError("private benchmark root must be inside the gitignored .captain-cook namespace")
    return resolved


def _validate_inputs(*, suite_version: int, seed_version_id: str) -> None:
    if isinstance(suite_version, bool) or not isinstance(suite_version, int) or suite_version < 1:
        raise ValueError("suite_version must be a positive integer")
    if not seed_version_id or len(seed_version_id) > 128:
        raise ValueError("seed_version_id must be a non-secret identifier")
    if (
        re.fullmatch(IDENTIFIER_PATTERN, seed_version_id) is None
        or _SECRET_SEED_PATTERN.search(seed_version_id)
    ):
        raise ValueError("seed_version_id must be a non-secret identifier")


def _index_path(root: Path, profile_id: str, suite_version: int) -> Path:
    return root / "suite-index" / f"{profile_id}.v{suite_version}.json"


def _canonical_model_bytes(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _suite_reference(suite: BusinessBenchmarkSuiteV1) -> PrivateHoldoutRef:
    digest = hashlib.sha256(canonical_business_benchmark_model_bytes(suite)).hexdigest()
    holdout_id = f"holdout-{digest[:12]}"
    return PrivateHoldoutRef(
        holdout_id=holdout_id,
        uri=f"holdout://{holdout_id}",
        sha256=digest,
    )


def _build_suite(
    *,
    profile_id: BusinessBenchmarkProfileId,
    suite_version: int,
    seed_version_id: str,
) -> BusinessBenchmarkSuiteV1:
    prefix = "claims" if profile_id == CLAIMS_PROFILE_ID else "renewal"
    seed_digest = _digest(seed_version_id, profile_id, str(suite_version))[:12]
    cases = tuple(
        _build_case(
            profile_id=profile_id,
            suite_version=suite_version,
            seed_version_id=seed_version_id,
            category=category,
            ordinal=ordinal,
        )
        for category in BusinessCaseCategory
        for ordinal in range(1, 4)
    )
    return BusinessBenchmarkSuiteV1(
        schema="captain.business-benchmark-suite.v1",
        suite_id=f"{prefix}-business-benchmark-v{suite_version}-{seed_digest}",
        profile_id=profile_id,
        suite_version=suite_version,
        cases=cases,
        created_at=_CANONICAL_CREATED_AT + timedelta(days=suite_version - 1),
    )


def _build_case(
    *,
    profile_id: BusinessBenchmarkProfileId,
    suite_version: int,
    seed_version_id: str,
    category: BusinessCaseCategory,
    ordinal: int,
) -> BusinessBenchmarkCaseV1:
    profile_prefix = "claims" if profile_id == CLAIMS_PROFILE_ID else "renewal"
    case_token = _digest(
        seed_version_id,
        profile_id,
        str(suite_version),
        category.value,
        str(ordinal),
    )[:12]
    common_input = {
        "synthetic_organization_id": f"org-{case_token[:6]}",
        "synthetic_subject_id": f"subject-{case_token[6:]}",
        "scenario_version": suite_version,
        "evidence_partition": category.value,
        "signal_variant": ordinal,
    }
    if profile_id == CLAIMS_PROFILE_ID:
        payload = _claims_case(category, ordinal, common_input)
    else:
        payload = _renewal_case(category, ordinal, common_input)
    return BusinessBenchmarkCaseV1(
        schema="captain.business-benchmark-case.v1",
        case_id=f"{profile_prefix}-{category.value}-{ordinal}-{case_token}",
        profile_id=profile_id,
        category=category,
        redacted_input=payload["redacted_input"],
        expected_decision=payload["expected_decision"],
        required_rationale_fact_ids=payload["required_rationale_fact_ids"],
        allowed_tool_intents=payload["allowed_tool_intents"],
        human_handoff_required=payload["human_handoff_required"],
        severity=payload["severity"],
    )


def _claims_case(
    category: BusinessCaseCategory,
    ordinal: int,
    common_input: dict[str, str | int],
) -> dict[str, object]:
    category_inputs: dict[BusinessCaseCategory, dict[str, object]] = {
        BusinessCaseCategory.ORDINARY: {
            "coverage_state": "active",
            "documentation_state": "complete",
            "loss_band": ("low", "moderate", "elevated")[ordinal - 1],
            "reported_delay_band": "within_standard_window",
        },
        BusinessCaseCategory.BOUNDARY: {
            "coverage_state": "active_near_boundary",
            "documentation_state": "complete",
            "loss_band": "upper_standard_boundary",
            "reported_delay_band": f"boundary_variant_{ordinal}",
        },
        BusinessCaseCategory.INCOMPLETE: {
            "coverage_state": "unverified",
            "documentation_state": f"missing_required_evidence_{ordinal}",
            "loss_band": "undetermined",
            "reported_delay_band": "unknown",
        },
        BusinessCaseCategory.CONTRADICTORY: {
            "coverage_state": "conflicting_sources",
            "documentation_state": "internally_inconsistent",
            "loss_band": f"conflicting_band_{ordinal}",
            "reported_delay_band": "conflicting_sources",
        },
        BusinessCaseCategory.MANDATORY_ESCALATION: {
            "coverage_state": "specialist_interpretation_required",
            "documentation_state": "complete_for_escalation",
            "loss_band": "critical_complexity",
            "reported_delay_band": f"escalation_trigger_{ordinal}",
        },
    }
    decisions = {
        BusinessCaseCategory.ORDINARY: "route_standard_review",
        BusinessCaseCategory.BOUNDARY: "route_standard_review",
        BusinessCaseCategory.INCOMPLETE: "request_information",
        BusinessCaseCategory.CONTRADICTORY: "escalate_coverage",
        BusinessCaseCategory.MANDATORY_ESCALATION: "escalate_coverage",
    }
    facts = {
        BusinessCaseCategory.ORDINARY: ("coverage_state_verified", "evidence_complete"),
        BusinessCaseCategory.BOUNDARY: ("boundary_condition_identified", "coverage_state_verified"),
        BusinessCaseCategory.INCOMPLETE: ("required_evidence_missing", "decision_deferred"),
        BusinessCaseCategory.CONTRADICTORY: ("evidence_conflict_detected", "specialist_review_required"),
        BusinessCaseCategory.MANDATORY_ESCALATION: (
            "critical_coverage_question_detected",
            "human_authority_required",
        ),
    }
    return {
        "redacted_input": common_input | category_inputs[category],
        "expected_decision": decisions[category],
        "required_rationale_fact_ids": facts[category],
        "allowed_tool_intents": (IntegrationIntent.NONE,),
        "human_handoff_required": category is BusinessCaseCategory.MANDATORY_ESCALATION,
        "severity": "critical"
        if category is BusinessCaseCategory.MANDATORY_ESCALATION
        else ("high" if category is BusinessCaseCategory.CONTRADICTORY else "normal"),
    }


def _renewal_case(
    category: BusinessCaseCategory,
    ordinal: int,
    common_input: dict[str, str | int],
) -> dict[str, object]:
    category_inputs: dict[BusinessCaseCategory, dict[str, object]] = {
        BusinessCaseCategory.ORDINARY: {
            "renewal_window": "open",
            "engagement_band": ("stable", "softening", "growth_ready")[ordinal - 1],
            "commercial_evidence_state": "complete",
            "consent_state": "verified",
        },
        BusinessCaseCategory.BOUNDARY: {
            "renewal_window": f"boundary_variant_{ordinal}",
            "engagement_band": "threshold",
            "commercial_evidence_state": "complete",
            "consent_state": "verified",
        },
        BusinessCaseCategory.INCOMPLETE: {
            "renewal_window": "open",
            "engagement_band": "undetermined",
            "commercial_evidence_state": f"missing_required_signal_{ordinal}",
            "consent_state": "unverified",
        },
        BusinessCaseCategory.CONTRADICTORY: {
            "renewal_window": "open",
            "engagement_band": "conflicting_sources",
            "commercial_evidence_state": f"commercial_conflict_{ordinal}",
            "consent_state": "verified",
        },
        BusinessCaseCategory.MANDATORY_ESCALATION: {
            "renewal_window": "executive_review_required",
            "engagement_band": "strategic_risk",
            "commercial_evidence_state": f"authority_trigger_{ordinal}",
            "consent_state": "verified",
        },
    }
    decisions = {
        BusinessCaseCategory.ORDINARY: "propose_next_best_action",
        BusinessCaseCategory.BOUNDARY: "propose_next_best_action",
        BusinessCaseCategory.INCOMPLETE: "request_information",
        BusinessCaseCategory.CONTRADICTORY: "human_commercial_review",
        BusinessCaseCategory.MANDATORY_ESCALATION: "human_commercial_review",
    }
    facts = {
        BusinessCaseCategory.ORDINARY: ("renewal_window_verified", "next_action_supported"),
        BusinessCaseCategory.BOUNDARY: ("commercial_boundary_identified", "next_action_bounded"),
        BusinessCaseCategory.INCOMPLETE: ("required_signal_missing", "action_deferred"),
        BusinessCaseCategory.CONTRADICTORY: ("commercial_conflict_detected", "human_review_required"),
        BusinessCaseCategory.MANDATORY_ESCALATION: (
            "strategic_authority_threshold_met",
            "human_commercial_authority_required",
        ),
    }
    uses_integration = category in {
        BusinessCaseCategory.ORDINARY,
        BusinessCaseCategory.BOUNDARY,
    }
    requires_handoff = category in {
        BusinessCaseCategory.CONTRADICTORY,
        BusinessCaseCategory.MANDATORY_ESCALATION,
    }
    return {
        "redacted_input": common_input | category_inputs[category],
        "expected_decision": decisions[category],
        "required_rationale_fact_ids": facts[category],
        "allowed_tool_intents": (
            (IntegrationIntent.N8N,) if uses_integration else (IntegrationIntent.NONE,)
        ),
        "human_handoff_required": requires_handoff,
        "severity": "critical"
        if category is BusinessCaseCategory.MANDATORY_ESCALATION
        else ("high" if category is BusinessCaseCategory.CONTRADICTORY else "normal"),
    }


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


__all__ = [
    "CLAIMS_PROFILE_ID",
    "RENEWAL_PROFILE_ID",
    "CaptainPrivateBusinessBenchmarkSuiteLoader",
    "CanonicalPrivateBusinessBenchmarkProvisioner",
    "ProvisionedBusinessBenchmarkSuiteRefV1",
    "ProvisionedBusinessBenchmarkSuitesV1",
    "default_private_business_benchmark_root",
]
