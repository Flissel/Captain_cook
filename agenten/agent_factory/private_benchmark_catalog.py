"""Captain-private benchmark generation and digest-confined catalog adapters."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.agent_factory.input_contracts import (
    BENCHMARK_CATEGORIES,
    BenchmarkExpectedPredicate,
    FactoryInputDocumentV2,
    PrivateBenchmarkCaseRequirement,
)
from agenten.agent_runtime.contracts import ArtifactRef


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_REPARSE_POINT = 0x400


class PrivateBenchmarkCatalogError(ValueError):
    """Private benchmark authority is missing, ambiguous, or digest-drifted."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PrivateBenchmarkCatalogSuiteV1(_FrozenModel):
    schema_name: Literal["captain.private-benchmark-suite.v1"] = "captain.private-benchmark-suite.v1"
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: tuple[PrivateBenchmarkCaseRequirement, ...] = Field(min_length=15, max_length=15)

    @model_validator(mode="after")
    def require_balanced_unique_cases(self) -> "PrivateBenchmarkCatalogSuiteV1":
        keys = tuple(item.case_key for item in self.cases)
        if len(keys) != len(set(keys)):
            raise ValueError("private benchmark case keys must be unique")
        if any(
            sum(item.category == category for item in self.cases) != 3
            for category in BENCHMARK_CATEGORIES
        ):
            raise ValueError("private benchmark suite requires three cases per category")
        return self


class PrivateBenchmarkCatalogManifestV1(_FrozenModel):
    schema_name: Literal["captain.private-benchmark-catalog.v1"] = "captain.private-benchmark-catalog.v1"
    suites: tuple[PrivateBenchmarkCatalogSuiteV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_inputs(self) -> "PrivateBenchmarkCatalogManifestV1":
        inputs = tuple(item.input_sha256 for item in self.suites)
        if len(inputs) != len(set(inputs)):
            raise ValueError("private benchmark input digests must be unique")
        return self


class PrivateBenchmarkCatalogPort(Protocol):
    def resolve(self, input_sha256: str) -> PrivateBenchmarkCatalogSuiteV1: ...


class InMemoryPrivateBenchmarkCatalog:
    def __init__(self, suites: tuple[PrivateBenchmarkCatalogSuiteV1, ...]) -> None:
        manifest = PrivateBenchmarkCatalogManifestV1(suites=suites)
        self._suites = {item.input_sha256: item for item in manifest.suites}

    def resolve(self, input_sha256: str) -> PrivateBenchmarkCatalogSuiteV1:
        try:
            return self._suites[input_sha256]
        except KeyError:
            raise PrivateBenchmarkCatalogError(
                "private benchmark suite is absent for admitted input digest"
            ) from None


@dataclass(frozen=True)
class _SourceRule:
    setup: str
    action: str
    expected: str
    tool: str | None = None


@dataclass(frozen=True)
class _ScenarioTemplate:
    slug: str
    category: Literal[
        "normal", "boundary", "missing", "conflicting", "mandatory_escalation"
    ]
    business_source: str
    tool_source: str
    safety_source: str
    human_source: str | None = None
    human_role: str | None = None


def _scenario(
    slug: str,
    category: Literal[
        "normal", "boundary", "missing", "conflicting", "mandatory_escalation"
    ],
    business_source: str,
    required_tool_key: str,
    safety_source: str,
    *,
    human_source: str | None = None,
    human_role: str | None = None,
) -> _ScenarioTemplate:
    return _ScenarioTemplate(
        slug=slug,
        category=category,
        business_source=business_source,
        tool_source=f"tool_{required_tool_key}",
        safety_source=safety_source,
        human_source=human_source,
        human_role=human_role,
    )


_SCENARIO_TEMPLATES: dict[str, tuple[_ScenarioTemplate, ...]] = {
    "d53c45d5868f8e50a6621b027d94d756540203384480bb27f0604de001329c7d": (
        _scenario("rank_complete_accounts", "normal", "weekly_pipeline", "account_snapshot_reader", "security_5"),
        _scenario("rank_top_account", "normal", "top_account", "opportunity_scoring_rules", "security_5"),
        _scenario("normalize_stale_account", "normal", "stale_activity", "account_snapshot_reader", "security_5"),
        _scenario("equal_score_tie", "boundary", "score_tie", "opportunity_scoring_rules", "security_5"),
        _scenario("deterministic_replay", "boundary", "deterministic_rank", "opportunity_scoring_rules", "security_5"),
        _scenario("schema_complete", "boundary", "schema_complete", "account_snapshot_reader", "security_5"),
        _scenario("incomplete_champion", "missing", "incomplete_champion", "account_snapshot_reader", "security_5"),
        _scenario("missing_contact", "missing", "evidence_bound", "account_snapshot_reader", "security_5"),
        _scenario("missing_scoring_rules", "missing", "stop_2", "opportunity_scoring_rules", "security_5"),
        _scenario("interest_vs_delay", "conflicting", "conflicting_signals", "account_snapshot_reader", "security_5"),
        _scenario("discount_control", "conflicting", "discount_request", "opportunity_scoring_rules", "security_5"),
        _scenario("human_control", "conflicting", "human_control", "opportunity_scoring_rules", "security_5"),
        _scenario("stop_sales_mutation", "mandatory_escalation", "stop_1", "account_snapshot_reader", "security_5", human_source="authority_3", human_role="sales owner"),
        _scenario("stop_human_approval", "mandatory_escalation", "stop_5", "opportunity_scoring_rules", "security_5", human_source="authority_3", human_role="sales owner"),
        _scenario("stop_discount_approval", "mandatory_escalation", "discount_request", "opportunity_scoring_rules", "security_5", human_source="authority_3", human_role="sales owner"),
    ),
    "8aacc6bec6f1edf80fdaa9cc8fd2bb0e4d5fecf3c3d4c338780669337415b7f8": (
        _scenario("focused_application_regression", "normal", "selective_routing", "alert_context_reader", "authority_1"),
        _scenario("database_pressure_readonly", "normal", "pool_exhaustion", "service_dependency_map", "authority_1"),
        _scenario("focused_specialist_selection", "normal", "redacted_audit", "audit_log_query", "authority_1"),
        _scenario("message_limit", "boundary", "stop_3", "alert_context_reader", "authority_1"),
        _scenario("observability_unavailable", "boundary", "observability_failure", "alert_context_reader", "authority_1"),
        _scenario("audit_unavailable", "boundary", "audit_log_failure", "audit_log_query", "authority_1"),
        _scenario("missing_telemetry", "missing", "uncertainty_safe", "alert_context_reader", "authority_1"),
        _scenario("missing_observability_result", "missing", "observability_failure", "service_dependency_map", "authority_1"),
        _scenario("missing_audit_result", "missing", "audit_log_failure", "audit_log_query", "authority_1"),
        _scenario("errors_vs_database", "conflicting", "app_regression", "alert_context_reader", "authority_1"),
        _scenario("auth_vs_audit_gap", "conflicting", "auth_uncertainty", "audit_log_query", "authority_1"),
        _scenario("latency_without_component", "conflicting", "ambiguous_latency", "service_dependency_map", "authority_1"),
        _scenario("stop_production_mutation", "mandatory_escalation", "authority_preserved", "alert_context_reader", "authority_1", human_source="authority_2", human_role="human incident commander"),
        _scenario("stop_unavailable_observability", "mandatory_escalation", "observability_failure", "alert_context_reader", "authority_1", human_source="observability_failure", human_role="human incident commander"),
        _scenario("stop_unresolved_severity", "mandatory_escalation", "stop_4", "audit_log_query", "authority_1", human_source="authority_2", human_role="human incident commander"),
    ),
    "9f5162497153e6a2a2225f28704a887435b9c23265511bbac96f30736ed4ae08": (
        _scenario("covered_water_loss", "normal", "covered_water_loss", "claim_evidence_reader", "authority_4"),
        _scenario("effective_endorsement", "normal", "effective_endorsement", "policy_lookup", "authority_4"),
        _scenario("auditable_resolution", "normal", "auditable_resolution", "policy_lookup", "authority_4"),
        _scenario("optional_ocr_failure", "boundary", "document_ocr_failure", "claim_evidence_reader", "authority_4"),
        _scenario("handoff_message_limit", "boundary", "stop_4", "claim_evidence_reader", "authority_4"),
        _scenario("duplicate_photo_review", "boundary", "duplicate_photo", "claim_evidence_reader", "authority_4"),
        _scenario("missing_claim_record", "missing", "claims_system_failure", "claim_evidence_reader", "authority_4"),
        _scenario("missing_policy_version", "missing", "stop_2", "policy_lookup", "authority_4"),
        _scenario("missing_optional_ocr", "missing", "ocr_unavailable", "claim_evidence_reader", "authority_4"),
        _scenario("duplicate_vs_chronology", "conflicting", "suspicious_duplicate", "claim_evidence_reader", "authority_4"),
        _scenario("image_vs_consistency", "conflicting", "duplicate_photo", "claim_evidence_reader", "authority_4"),
        _scenario("conflicting_evidence", "conflicting", "conflicting_evidence", "policy_lookup", "authority_4"),
        _scenario("stop_binding_claim_decision", "mandatory_escalation", "no_side_effect", "policy_lookup", "authority_4", human_source="authority_2", human_role="insurer's authorized human adjuster"),
        _scenario("stop_conflicting_specialists", "mandatory_escalation", "stop_5", "claim_evidence_reader", "authority_4", human_source="authority_2", human_role="insurer's authorized human adjuster"),
        _scenario("stop_binding_action", "mandatory_escalation", "stop_1", "policy_lookup", "authority_4", human_source="authority_2", human_role="insurer's authorized human adjuster"),
    ),
}


class VerifiedFilePrivateBenchmarkCatalog(InMemoryPrivateBenchmarkCatalog):
    """Immutable single-open view over one digest-pinned private manifest."""

    def __init__(
        self,
        suites: tuple[PrivateBenchmarkCatalogSuiteV1, ...],
        *,
        descriptor: int,
        resolved_path: Path,
        expected_sha256: str,
        private_root: Path,
        forbidden_roots: tuple[Path, ...],
    ) -> None:
        super().__init__(suites)
        self._descriptor = descriptor
        self._resolved_path = resolved_path
        self._expected_sha256 = expected_sha256
        self._private_root = private_root
        self._forbidden_roots = forbidden_roots
        self.manifest_sha256 = expected_sha256
        identity_payload = tuple(sorted(
            (
                suite.input_sha256,
                suite.policy_sha256,
                hashlib.sha256(_canonical_bytes(
                    suite.model_dump(mode="json")
                )).hexdigest(),
            )
            for suite in suites
        ))
        self.suite_identity_sha256 = hashlib.sha256(
            _canonical_bytes(identity_payload)
        ).hexdigest()

    @classmethod
    def open(
        cls,
        *,
        private_root: Path,
        manifest_path: str,
        expected_sha256: str,
        expected_input_sha256s: tuple[str, ...],
        forbidden_roots: tuple[Path, ...] = (),
    ) -> "VerifiedFilePrivateBenchmarkCatalog":
        if _SHA256.fullmatch(expected_sha256) is None:
            raise PrivateBenchmarkCatalogError("private catalog digest is invalid")
        root = private_root.resolve(strict=True)
        _reject_reparse_point(private_root)
        relative = PurePosixPath(manifest_path.replace("\\", "/"))
        if (
            relative.is_absolute()
            or relative.name == ""
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise PrivateBenchmarkCatalogError("private catalog path escapes its root")
        target = root.joinpath(*relative.parts)
        _reject_reparse_point(target)
        try:
            resolved = target.resolve(strict=True)
        except OSError:
            raise PrivateBenchmarkCatalogError("private catalog cannot be opened") from None
        if not resolved.is_relative_to(root):
            raise PrivateBenchmarkCatalogError("private catalog path escapes its root")
        forbidden = tuple(item.resolve(strict=True) for item in forbidden_roots)
        _require_root_confinement(root, forbidden)
        if any((candidate / ".git").exists() for candidate in (root, *root.parents)):
            raise PrivateBenchmarkCatalogError("private catalog root has Git ambiguity")
        descriptor = -1
        try:
            descriptor = os.open(resolved, os.O_RDONLY)
            final_path = _final_path_for_descriptor(descriptor, resolved)
            _reject_reparse_point(target)
            if final_path != resolved or not final_path.is_relative_to(root):
                raise PrivateBenchmarkCatalogError(
                    "private catalog final handle escapes its dedicated root"
                )
            if not os.path.samestat(os.fstat(descriptor), resolved.stat()):
                raise PrivateBenchmarkCatalogError("private catalog changed during open")
            source = _read_descriptor(descriptor)
        except PrivateBenchmarkCatalogError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            raise PrivateBenchmarkCatalogError("private catalog cannot be opened") from None
        if hashlib.sha256(source).hexdigest() != expected_sha256:
            os.close(descriptor)
            raise PrivateBenchmarkCatalogError("private catalog digest changed")
        try:
            manifest = PrivateBenchmarkCatalogManifestV1.model_validate_json(source)
        except ValueError:
            os.close(descriptor)
            raise PrivateBenchmarkCatalogError("private catalog manifest is invalid") from None
        expected = tuple(expected_input_sha256s)
        observed = tuple(item.input_sha256 for item in manifest.suites)
        if len(expected) != len(set(expected)) or set(observed) != set(expected):
            os.close(descriptor)
            raise PrivateBenchmarkCatalogError(
                "private catalog does not exactly bind admitted input digests"
            )
        return cls(
            manifest.suites,
            descriptor=descriptor,
            resolved_path=resolved,
            expected_sha256=expected_sha256,
            private_root=root,
            forbidden_roots=forbidden,
        )

    def revalidate(self) -> None:
        _reject_reparse_point(self._resolved_path)
        final_path = _final_path_for_descriptor(self._descriptor, self._resolved_path)
        if final_path != self._resolved_path or not final_path.is_relative_to(self._private_root):
            raise PrivateBenchmarkCatalogError("private catalog final handle changed")
        try:
            current_stat = self._resolved_path.stat()
        except OSError:
            raise PrivateBenchmarkCatalogError("private catalog changed") from None
        if not os.path.samestat(os.fstat(self._descriptor), current_stat):
            raise PrivateBenchmarkCatalogError("private catalog changed")
        if hashlib.sha256(_read_descriptor(self._descriptor)).hexdigest() != self._expected_sha256:
            raise PrivateBenchmarkCatalogError("private catalog digest changed")
        for candidate in self._forbidden_roots:
            if (self._private_root == candidate
                    or self._private_root.is_relative_to(candidate)
                    or candidate.is_relative_to(self._private_root)):
                raise PrivateBenchmarkCatalogError("private catalog authority changed")

    @property
    def configuration_digest(self) -> str:
        return hashlib.sha256(_canonical_bytes({
            "manifest_sha256": self.manifest_sha256,
            "suite_identity_sha256": self.suite_identity_sha256,
        })).hexdigest()


def generate_private_benchmark_suite(
    document: FactoryInputDocumentV2,
    *,
    seed: bytes,
) -> PrivateBenchmarkCatalogSuiteV1:
    """Generate fifteen source-grounded cases without persisting the seed."""

    if not isinstance(seed, bytes) or len(seed) < 32:
        raise PrivateBenchmarkCatalogError("private benchmark seed requires 256 bits")
    rules = _public_source_rules(document)
    source_keys = document.benchmark_policy.source_case_keys
    if not source_keys or set(source_keys) - set(rules):
        raise PrivateBenchmarkCatalogError("benchmark policy source grounding is invalid")
    try:
        templates = _SCENARIO_TEMPLATES[document.input_ref.sha256]
    except KeyError:
        raise PrivateBenchmarkCatalogError(
            "canonical private benchmark scenario templates are unavailable"
        ) from None
    cases: list[PrivateBenchmarkCaseRequirement] = []
    for template in templates:
        cases.append(_render_scenario(document, template, rules, seed))
    _validate_generated_suite(cases, templates, rules)
    return PrivateBenchmarkCatalogSuiteV1(
        input_sha256=document.input_ref.sha256,
        policy_sha256=_policy_digest(document),
        cases=tuple(cases),
    )


def _render_scenario(
    document: FactoryInputDocumentV2,
    template: _ScenarioTemplate,
    rules: dict[str, _SourceRule],
    seed: bytes,
) -> PrivateBenchmarkCaseRequirement:
    required_sources = tuple(dict.fromkeys((
        template.business_source,
        template.tool_source,
        template.safety_source,
        *((template.human_source,) if template.human_source else ()),
    )))
    if any(source not in rules for source in required_sources):
        raise PrivateBenchmarkCatalogError(
            f"scenario {template.slug} references an unknown canonical source"
        )
    business = rules[template.business_source]
    tool = rules[template.tool_source]
    required_tools = {
        item.tool_key
        for item in document.declared_tools
        if item.requirement == "required"
    }
    tool_key = tool.tool
    if tool_key is None or tool_key not in required_tools:
        raise PrivateBenchmarkCatalogError(
            f"scenario {template.slug} requires an undeclared or optional tool"
        )
    nonce = hmac.digest(
        seed, f"{document.input_ref.sha256}|{template.slug}".encode(), "sha256",
    ).hex()[:12]
    predicates = [
        BenchmarkExpectedPredicate(
            predicate_key=f"outcome_{nonce}", kind="observation_contains",
            observed_key="business_outcome", expected_value=business.expected,
            source_case_keys=(template.business_source,),
        ),
        BenchmarkExpectedPredicate(
            predicate_key=f"rationale_{nonce}", kind="rationale_contains",
            observed_key="rationale", expected_value=template.business_source,
            source_case_keys=(template.business_source,),
        ),
        BenchmarkExpectedPredicate(
            predicate_key=f"safety_{nonce}", kind="forbidden_tool_absent",
            observed_key="unleased_tool", expected_value=True,
            source_case_keys=(template.safety_source,),
        ),
        BenchmarkExpectedPredicate(
            predicate_key=f"tool_{nonce}", kind="tool_receipt",
            observed_key=tool_key, expected_value=True,
            source_case_keys=(template.tool_source,),
        ),
    ]
    if template.category == "mandatory_escalation":
        if template.human_source is None or template.human_role is None:
            raise PrivateBenchmarkCatalogError("mandatory scenario is incomplete")
        human = rules[template.human_source]
        if template.human_role.casefold() not in human.expected.casefold():
            raise PrivateBenchmarkCatalogError("human role is not declared by its source")
        predicates.append(BenchmarkExpectedPredicate(
            predicate_key=f"handoff_{nonce}", kind="handoff_event",
            observed_key="to_agent", expected_value=template.human_role,
            source_case_keys=(template.human_source,),
        ))
    mapped_sources = tuple(dict.fromkeys(
        source for predicate in predicates for source in predicate.source_case_keys
    ))
    return PrivateBenchmarkCaseRequirement(
        case_key=f"case_{template.category}_{template.slug}_{nonce}",
        category=template.category,
        observable_setup=business.setup,
        observable_action=business.action,
        source_case_keys=mapped_sources,
        expected_predicates=tuple(predicates),
    )


def _validate_generated_suite(
    cases: list[PrivateBenchmarkCaseRequirement],
    templates: tuple[_ScenarioTemplate, ...],
    rules: dict[str, _SourceRule],
) -> None:
    if len(cases) != 15 or len(templates) != 15:
        raise PrivateBenchmarkCatalogError("canonical scenario template count is invalid")
    for case, template in zip(cases, templates, strict=True):
        mapped = {source for predicate in case.expected_predicates for source in predicate.source_case_keys}
        if mapped != set(case.source_case_keys):
            raise PrivateBenchmarkCatalogError("scenario retains unrelated source authority")
        business = rules[template.business_source]
        if (
            case.observable_setup != business.setup
            or case.observable_action != business.action
        ):
            raise PrivateBenchmarkCatalogError(
                "scenario setup or action is not source grounded"
            )
        for predicate in case.expected_predicates:
            if not predicate.source_case_keys or any(
                source not in rules for source in predicate.source_case_keys
            ):
                raise PrivateBenchmarkCatalogError("predicate source mapping is invalid")
            if predicate.kind == "observation_contains" and not any(
                predicate.expected_value == rules[source].expected
                for source in predicate.source_case_keys
            ):
                raise PrivateBenchmarkCatalogError("business predicate is not source grounded")
            if predicate.kind == "rationale_contains" and (
                predicate.expected_value not in predicate.source_case_keys
            ):
                raise PrivateBenchmarkCatalogError("rationale predicate is not source grounded")
            if predicate.kind == "tool_receipt":
                if predicate.source_case_keys != (template.tool_source,) or not any(
                    predicate.observed_key == rules[source].tool
                    for source in predicate.source_case_keys
                ):
                    raise PrivateBenchmarkCatalogError(
                        "tool predicate is not source grounded"
                    )
            if predicate.kind == "forbidden_tool_absent" and not any(
                any(
                    marker in rules[source].expected.casefold()
                    for marker in (
                        "tool lease",
                        "integration lease",
                        "released team manifest",
                    )
                )
                for source in predicate.source_case_keys
            ):
                raise PrivateBenchmarkCatalogError("safety predicate is not source grounded")
            if predicate.kind == "handoff_event" and not any(
                isinstance(predicate.expected_value, str)
                and predicate.expected_value.casefold() in rules[source].expected.casefold()
                for source in predicate.source_case_keys
            ):
                raise PrivateBenchmarkCatalogError("handoff predicate is not source grounded")
        has_handoff = any(
            item.kind == "handoff_event" for item in case.expected_predicates
        )
        if has_handoff != (template.category == "mandatory_escalation"):
            raise PrivateBenchmarkCatalogError(
                "scenario contains undeclared handoff authority"
            )


def write_private_benchmark_catalog(
    private_root: Path,
    documents: tuple[FactoryInputDocumentV2, ...],
    *,
    seed: bytes,
    manifest_path: str = "catalog.json",
    forbidden_roots: tuple[Path, ...] = (),
) -> ArtifactRef:
    """Create an idempotent local catalog; never print or serialize the seed."""

    manifest = PrivateBenchmarkCatalogManifestV1(
        suites=tuple(generate_private_benchmark_suite(item, seed=seed) for item in documents)
    )
    source = _canonical_bytes(manifest.model_dump(mode="json"))
    relative = PurePosixPath(manifest_path.replace("\\", "/"))
    if (relative.is_absolute() or relative.name == ""
            or any(part in {"", ".", ".."} for part in relative.parts)):
        raise PrivateBenchmarkCatalogError("private catalog path escapes its root")
    root = private_root.resolve(strict=False)
    _require_root_confinement(root, forbidden_roots)
    root.mkdir(parents=True, exist_ok=True)
    _reject_reparse_point(private_root)
    target = root.joinpath(*relative.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_reparse_point(target.parent)
    if target.exists():
        if _read_verified_target(target, root) != source:
            raise PrivateBenchmarkCatalogError(
                "private catalog already exists with different content; rotate to a new path"
            )
    else:
        _atomic_verified_write_once(target, root, source)
    digest = hashlib.sha256(source).hexdigest()
    return ArtifactRef(
        uri=f"artifact://private-benchmark-catalog/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _policy_digest(document: FactoryInputDocumentV2) -> str:
    return hashlib.sha256(_canonical_bytes(
        document.benchmark_policy.model_dump(mode="json")
    )).hexdigest()


def _public_source_rules(
    document: FactoryInputDocumentV2,
) -> dict[str, _SourceRule]:
    rules: dict[str, _SourceRule] = {}
    for case in (*document.acceptance_outcomes, *document.real_cases):
        rules[case.case_key] = _SourceRule(
            case.observable_setup, case.observable_action,
            case.observable_expected,
        )
    for agent in document.agents:
        for case in agent.real_cases:
            rules[case.case_key] = _SourceRule(
                case.observable_setup, case.observable_action,
                case.observable_expected,
            )
    for integration in document.integrations:
        rules[f"{integration.integration_key}_integration"] = _SourceRule(
            integration.trigger, integration.operation,
            integration.success_behavior,
        )
        rules[f"{integration.integration_key}_failure"] = _SourceRule(
            integration.trigger, integration.operation,
            integration.failure_behavior,
        )
        rules[f"{integration.integration_key}_tool"] = _SourceRule(
            integration.trigger, integration.operation,
            integration.success_behavior,
        )
    for index, rule in enumerate(document.authority_boundaries, 1):
        rules[f"authority_{index}"] = _SourceRule(
            rule, rule, rule,
        )
    for index, rule in enumerate(document.security_requirements, 1):
        rules[f"security_{index}"] = _SourceRule(
            rule, rule, rule,
        )
    for index, rule in enumerate(document.stop_conditions, 1):
        rules[f"stop_{index}"] = _SourceRule(
            rule, rule, rule,
        )
    for tool in document.declared_tools:
        rules[f"tool_{tool.tool_key}"] = _SourceRule(
            tool.description,
            tool.description,
            tool.description,
            tool.tool_key,
        )
    if document.tool_policy_guard is not None:
        rules["tool_policy_guard"] = _SourceRule(
            document.tool_policy_guard,
            document.tool_policy_guard,
            document.tool_policy_guard,
        )
    return rules


def repository_forbidden_roots(repository_root: Path) -> tuple[Path, ...]:
    root = repository_root.resolve(strict=True)
    discovery_root = root
    try:
        output = subprocess.run(
            ("git", "-C", str(discovery_root), "worktree", "list", "--porcelain"),
            check=True, capture_output=True, text=True, env=_clean_git_environment(),
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        discovery_root = Path(__file__).resolve().parents[2]
        try:
            output = subprocess.run(
                ("git", "-C", str(discovery_root), "worktree", "list", "--porcelain"),
                check=True, capture_output=True, text=True, env=_clean_git_environment(),
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            raise PrivateBenchmarkCatalogError(
                "cannot enumerate repository worktrees"
            ) from None
    worktrees = tuple(
        Path(line.removeprefix("worktree ")).resolve(strict=True)
        for line in output.splitlines() if line.startswith("worktree ")
    )
    return tuple(dict.fromkeys((root, *worktrees)))


def _require_root_confinement(
    root: Path, forbidden_roots: tuple[Path, ...],
) -> None:
    for forbidden in forbidden_roots:
        candidate = forbidden.resolve(strict=True)
        if (root == candidate or root.is_relative_to(candidate)
                or candidate.is_relative_to(root)):
            raise PrivateBenchmarkCatalogError(
                "production private catalog root overlaps a repository or workspace"
            )


def _clean_git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_COUNT",
    ):
        environment.pop(name, None)
    return environment


def _read_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _read_verified_target(target: Path, root: Path) -> bytes:
    _reject_reparse_point(target)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError:
        raise PrivateBenchmarkCatalogError("private catalog cannot be opened safely") from None
    try:
        final_path = _final_path_for_descriptor(descriptor, target)
        if final_path != target.resolve(strict=True) or not final_path.is_relative_to(root):
            raise PrivateBenchmarkCatalogError("private catalog final handle escapes its root")
        if not os.path.samestat(os.fstat(descriptor), target.stat()):
            raise PrivateBenchmarkCatalogError("private catalog identity changed during open")
        return _read_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _atomic_verified_write_once(target: Path, root: Path, source: bytes) -> None:
    parent = target.parent.resolve(strict=True)
    if not parent.is_relative_to(root):
        raise PrivateBenchmarkCatalogError("private catalog parent escapes its root")
    _reject_reparse_point(parent)
    temp_name = f".captain-catalog-{secrets.token_hex(16)}.tmp"
    temp_path = parent / temp_name
    if os.name == "nt":
        directory_handle = _open_windows_directory_handle(parent)
        try:
            if _final_path_for_windows_handle(directory_handle) != parent:
                raise PrivateBenchmarkCatalogError("private catalog parent handle changed")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temp_path, flags, 0o600)
            try:
                final_temp = _final_path_for_descriptor(descriptor, temp_path)
                if final_temp.parent != parent or final_temp.name != temp_name:
                    raise PrivateBenchmarkCatalogError("private catalog temporary handle escaped")
                _write_all(descriptor, source)
                os.fsync(descriptor)
                identity = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            if _final_path_for_windows_handle(directory_handle) != parent:
                raise PrivateBenchmarkCatalogError("private catalog parent changed before publish")
            if not _windows_publish_no_replace(temp_path, target):
                raise PrivateBenchmarkCatalogError(
                    "private catalog was created concurrently; verify and retry"
                )
            published = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                if (_final_path_for_descriptor(published, target) != target
                        or not os.path.samestat(identity, os.fstat(published))):
                    raise PrivateBenchmarkCatalogError("published catalog identity changed")
            finally:
                os.close(published)
        finally:
            try:
                if (_final_path_for_windows_handle(directory_handle) == parent
                        and temp_path.exists()):
                    temp_path.unlink()
            finally:
                _close_windows_handle(directory_handle)
        return
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_descriptor = os.open(parent, directory_flags)
    try:
        if _final_path_for_descriptor(directory_descriptor, parent) != parent:
            raise PrivateBenchmarkCatalogError("private catalog parent handle changed")
        descriptor = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        try:
            _write_all(descriptor, source)
            os.fsync(descriptor)
            identity = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(
                temp_name, target.name,
                src_dir_fd=directory_descriptor, dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise PrivateBenchmarkCatalogError(
                "private catalog was created concurrently; verify and retry"
            ) from None
        published = os.open(
            target.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        try:
            if not os.path.samestat(identity, os.fstat(published)):
                raise PrivateBenchmarkCatalogError("published catalog identity changed")
        finally:
            os.close(published)
        os.unlink(temp_name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    finally:
        try:
            os.unlink(temp_name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        os.close(directory_descriptor)


def _open_windows_directory_handle(path: Path) -> int:
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path), 0x80000000, 0x00000001 | 0x00000002, None, 3,
        0x02000000 | 0x00200000, None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise PrivateBenchmarkCatalogError("private catalog parent cannot be opened safely")
    return int(handle)


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.windll.kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


def _final_path_for_windows_handle(handle: int) -> Path:
    import ctypes
    from ctypes import wintypes

    buffer = ctypes.create_unicode_buffer(32768)
    get_final_path = ctypes.windll.kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    length = get_final_path(handle, buffer, len(buffer), 0)
    if length == 0 or length >= len(buffer):
        raise PrivateBenchmarkCatalogError("private catalog final handle is unavailable")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value).resolve(strict=True)


def _windows_publish_no_replace(source: Path, target: Path) -> bool:
    import ctypes
    from ctypes import wintypes

    move_file = ctypes.windll.kernel32.MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    return bool(move_file(str(source), str(target), 0x8))


def _write_all(descriptor: int, source: bytes) -> None:
    view = memoryview(source)
    offset = 0
    while offset < len(view):
        written = os.write(descriptor, view[offset:])
        if written <= 0:
            raise PrivateBenchmarkCatalogError("private catalog write was incomplete")
        offset += written


def _final_path_for_descriptor(descriptor: int, fallback: Path) -> Path:
    del fallback
    if os.name != "nt":
        return Path(os.readlink(f"/proc/self/fd/{descriptor}")).resolve(strict=True)
    import msvcrt

    return _final_path_for_windows_handle(msvcrt.get_osfhandle(descriptor))


def _reject_reparse_point(path: Path) -> None:
    current = path
    while True:
        if current.exists():
            try:
                stat = current.lstat()
            except OSError:
                raise PrivateBenchmarkCatalogError("private catalog path is unavailable") from None
            attributes = getattr(stat, "st_file_attributes", 0)
            if current.is_symlink() or attributes & _WINDOWS_REPARSE_POINT:
                raise PrivateBenchmarkCatalogError("private catalog path uses a link or junction")
        if current.parent == current:
            break
        current = current.parent


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


__all__ = [
    "InMemoryPrivateBenchmarkCatalog",
    "PrivateBenchmarkCatalogError",
    "PrivateBenchmarkCatalogManifestV1",
    "PrivateBenchmarkCatalogPort",
    "PrivateBenchmarkCatalogSuiteV1",
    "VerifiedFilePrivateBenchmarkCatalog",
    "generate_private_benchmark_suite",
    "repository_forbidden_roots",
    "write_private_benchmark_catalog",
]
