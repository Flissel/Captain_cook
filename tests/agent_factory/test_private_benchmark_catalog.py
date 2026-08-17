from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid5

import pytest
import agenten.agent_factory.private_benchmark_catalog as catalog_module

from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.execution_policy import (
    FactoryExecutionMode, FactoryExecutionPolicyV1, FactoryLiveCapability,
)
from agenten.agent_factory.job_builder import build_factory_job_v3
from agenten.agent_factory.input_compiler import FactoryInputCompiler
from agenten.agent_factory.input_document import FactoryInputError, load_factory_input, parse_factory_input_bytes
from agenten.agent_factory.private_benchmark_catalog import (
    InMemoryPrivateBenchmarkCatalog,
    PrivateBenchmarkCatalogError,
    VerifiedFilePrivateBenchmarkCatalog,
    generate_private_benchmark_suite,
    write_private_benchmark_catalog,
)
from agenten.agent_factory.capability_live_runner import CapabilityLiveReservation
from gateway.capability_assembly_admission import RepositoryCapabilityAssemblyInputAdmission
from gateway.capability_assembly_contracts import CapabilityAssemblyInputRequest
from gateway.capability_generic_benchmark import (
    GenericPrivateBenchmarkRepository,
    build_generic_benchmark_suite,
)


ROOT = Path(__file__).resolve().parents[2]
INPUTS = (
    "demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md",
    "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md",
    "demo_inputs/agent_factory/claims_resolution/TO_BE_BUILT.md",
)
CATEGORIES = {
    "normal", "boundary", "missing", "conflicting", "mandatory_escalation",
}
NAMESPACE = UUID("e9a5fc44-f475-5c40-b1c6-c51257723968")
EXPECTED_SCENARIOS = {
    INPUTS[0]: (
        ("rank_complete_accounts", "normal", "weekly_pipeline", "account_snapshot_reader"),
        ("rank_top_account", "normal", "top_account", "opportunity_scoring_rules"),
        ("normalize_stale_account", "normal", "stale_activity", "account_snapshot_reader"),
        ("equal_score_tie", "boundary", "score_tie", "opportunity_scoring_rules"),
        ("deterministic_replay", "boundary", "deterministic_rank", "opportunity_scoring_rules"),
        ("schema_complete", "boundary", "schema_complete", "account_snapshot_reader"),
        ("incomplete_champion", "missing", "incomplete_champion", "account_snapshot_reader"),
        ("missing_contact", "missing", "evidence_bound", "account_snapshot_reader"),
        ("missing_scoring_rules", "missing", "stop_2", "opportunity_scoring_rules"),
        ("interest_vs_delay", "conflicting", "conflicting_signals", "account_snapshot_reader"),
        ("discount_control", "conflicting", "discount_request", "opportunity_scoring_rules"),
        ("human_control", "conflicting", "human_control", "opportunity_scoring_rules"),
        ("stop_sales_mutation", "mandatory_escalation", "stop_1", "account_snapshot_reader"),
        ("stop_human_approval", "mandatory_escalation", "stop_5", "opportunity_scoring_rules"),
        ("stop_discount_approval", "mandatory_escalation", "discount_request", "opportunity_scoring_rules"),
    ),
    INPUTS[1]: (
        ("focused_application_regression", "normal", "selective_routing", "alert_context_reader"),
        ("database_pressure_readonly", "normal", "pool_exhaustion", "service_dependency_map"),
        ("focused_specialist_selection", "normal", "redacted_audit", "audit_log_query"),
        ("message_limit", "boundary", "stop_3", "alert_context_reader"),
        ("observability_unavailable", "boundary", "observability_failure", "alert_context_reader"),
        ("audit_unavailable", "boundary", "audit_log_failure", "audit_log_query"),
        ("missing_telemetry", "missing", "uncertainty_safe", "alert_context_reader"),
        ("missing_observability_result", "missing", "observability_failure", "service_dependency_map"),
        ("missing_audit_result", "missing", "audit_log_failure", "audit_log_query"),
        ("errors_vs_database", "conflicting", "app_regression", "alert_context_reader"),
        ("auth_vs_audit_gap", "conflicting", "auth_uncertainty", "audit_log_query"),
        ("latency_without_component", "conflicting", "ambiguous_latency", "service_dependency_map"),
        ("stop_production_mutation", "mandatory_escalation", "authority_preserved", "alert_context_reader"),
        ("stop_unavailable_observability", "mandatory_escalation", "observability_failure", "alert_context_reader"),
        ("stop_unresolved_severity", "mandatory_escalation", "stop_4", "audit_log_query"),
    ),
    INPUTS[2]: (
        ("covered_water_loss", "normal", "covered_water_loss", "claim_evidence_reader"),
        ("effective_endorsement", "normal", "effective_endorsement", "policy_lookup"),
        ("auditable_resolution", "normal", "auditable_resolution", "policy_lookup"),
        ("optional_ocr_failure", "boundary", "document_ocr_failure", "claim_evidence_reader"),
        ("handoff_message_limit", "boundary", "stop_4", "claim_evidence_reader"),
        ("duplicate_photo_review", "boundary", "duplicate_photo", "claim_evidence_reader"),
        ("missing_claim_record", "missing", "claims_system_failure", "claim_evidence_reader"),
        ("missing_policy_version", "missing", "stop_2", "policy_lookup"),
        ("missing_optional_ocr", "missing", "ocr_unavailable", "claim_evidence_reader"),
        ("duplicate_vs_chronology", "conflicting", "suspicious_duplicate", "claim_evidence_reader"),
        ("image_vs_consistency", "conflicting", "duplicate_photo", "claim_evidence_reader"),
        ("conflicting_evidence", "conflicting", "conflicting_evidence", "policy_lookup"),
        ("stop_binding_claim_decision", "mandatory_escalation", "no_side_effect", "policy_lookup"),
        ("stop_conflicting_specialists", "mandatory_escalation", "stop_5", "claim_evidence_reader"),
        ("stop_binding_action", "mandatory_escalation", "stop_1", "policy_lookup"),
    ),
}


def _documents():
    return tuple(load_factory_input(ROOT / relative) for relative in INPUTS)


def _id(label: str) -> UUID:
    return uuid5(NAMESPACE, label)


def test_seeded_generator_is_deterministic_balanced_unique_and_nonvacuous() -> None:
    document = _documents()[0]
    agent_case_keys = {
        case.case_key for agent in document.agents for case in agent.real_cases
    }

    first = generate_private_benchmark_suite(document, seed=b"a" * 32)
    replay = generate_private_benchmark_suite(document, seed=b"a" * 32)
    rotated = generate_private_benchmark_suite(document, seed=b"b" * 32)

    assert first == replay
    assert first != rotated
    assert agent_case_keys.issubset(set(document.benchmark_policy.source_case_keys))
    assert len(first.cases) == 15
    assert len({case.case_key for case in first.cases}) == 15
    assert {
        category: sum(case.category == category for case in first.cases)
        for category in CATEGORIES
    } == {category: 3 for category in CATEGORIES}
    assert all(case.source_case_keys for case in first.cases)
    assert all(case.observable_setup.strip() and case.observable_action.strip() for case in first.cases)
    for case in first.cases:
        kinds = {predicate.kind for predicate in case.expected_predicates}
        assert kinds & {"observation_equals", "observation_contains", "observation_absent"}
        assert "rationale_contains" in kinds
        assert "forbidden_tool_absent" in kinds
        assert "tool_receipt" in kinds
        if case.category == "mandatory_escalation":
            assert "handoff_event" in kinds


@pytest.mark.parametrize("relative_path", INPUTS)
def test_each_private_case_binds_the_exact_public_declared_tool_set(
    relative_path: str,
) -> None:
    document = load_factory_input(ROOT / relative_path)
    declared_tools = tuple(sorted(item.tool_key for item in document.declared_tools))

    suite = generate_private_benchmark_suite(
        document, seed=b"declared-tool-safety-binding-seed" * 2,
    )

    assert all(case.declared_tool_keys == declared_tools for case in suite.cases)


@pytest.mark.parametrize("relative_path", INPUTS)
def test_each_generated_category_is_a_grounded_behavioral_transformation(
    relative_path: str,
) -> None:
    document = load_factory_input(ROOT / relative_path)
    suite = generate_private_benchmark_suite(document, seed=b"semantic-transform-seed-value!!" * 2)
    sources = {
        case.case_key: (
            case.observable_setup, case.observable_action, case.observable_expected,
        )
        for case in (*document.acceptance_outcomes, *document.real_cases)
    }
    sources.update({
        case.case_key: (
            case.observable_setup, case.observable_action, case.observable_expected,
        )
        for agent in document.agents for case in agent.real_cases
    })
    for integration in document.integrations:
        sources[f"{integration.integration_key}_integration"] = (
            integration.trigger, integration.operation, integration.success_behavior,
        )
        sources[f"{integration.integration_key}_failure"] = (
            integration.trigger, integration.operation, integration.failure_behavior,
        )
        sources[f"{integration.integration_key}_tool"] = (
            integration.trigger, integration.operation, integration.success_behavior,
        )
    sources.update({
        f"authority_{index}": (rule, rule, rule)
        for index, rule in enumerate(document.authority_boundaries, 1)
    })
    sources.update({
        f"security_{index}": (rule, rule, rule)
        for index, rule in enumerate(document.security_requirements, 1)
    })
    sources.update({
        f"stop_{index}": (rule, rule, rule)
        for index, rule in enumerate(document.stop_conditions, 1)
    })
    sources.update({
        f"tool_{tool.tool_key}": (
            tool.description, tool.description, tool.description,
        )
        for tool in document.declared_tools
    })
    if document.tool_policy_guard is not None:
        sources["tool_policy_guard"] = (
            document.tool_policy_guard,
            document.tool_policy_guard,
            document.tool_policy_guard,
        )

    expected = EXPECTED_SCENARIOS[relative_path]
    assert len(suite.cases) == len(expected) == 15
    declared_tools = {
        item.tool_key
        for item in document.declared_tools
        if item.requirement == "required"
    }
    for case, (slug, category, business_source, required_tool) in zip(
        suite.cases, expected, strict=True,
    ):
        assert f"_{slug}_" in case.case_key
        assert case.category == category
        assert case.observable_setup == sources[business_source][0]
        assert case.observable_action == sources[business_source][1]
        assert set(case.source_case_keys) <= set(sources)
        mapped = {
            source
            for predicate in case.expected_predicates
            for source in predicate.source_case_keys
        }
        assert mapped == set(case.source_case_keys)
        assert all(predicate.source_case_keys for predicate in case.expected_predicates)
        outcome = next(
            item for item in case.expected_predicates
            if item.kind == "observation_contains"
        )
        assert outcome.source_case_keys == (business_source,)
        assert outcome.expected_value == sources[business_source][2]
        tool = next(
            item for item in case.expected_predicates if item.kind == "tool_receipt"
        )
        assert tool.observed_key == required_tool
        assert tool.observed_key in declared_tools
        assert tool.source_case_keys == (f"tool_{required_tool}",)
        for predicate in case.expected_predicates:
            assert set(predicate.source_case_keys) <= set(sources)
            if predicate.kind == "observation_contains":
                assert any(
                    predicate.expected_value == sources[source][2]
                    for source in predicate.source_case_keys
                )
            elif predicate.kind == "rationale_contains":
                assert predicate.observed_key == "business_outcome"
                assert any(
                    predicate.expected_value == sources[source][2]
                    for source in predicate.source_case_keys
                )
            elif predicate.kind == "tool_receipt":
                assert predicate.source_case_keys == (
                    f"tool_{predicate.observed_key}",
                )
            elif predicate.kind == "forbidden_tool_absent":
                assert any(
                    any(
                        marker in sources[source][2].casefold()
                        for marker in (
                            "tool lease",
                            "integration lease",
                            "released team manifest",
                        )
                    )
                    for source in predicate.source_case_keys
                )
        if case.category == "normal":
            assert not any(item.kind == "handoff_event" for item in case.expected_predicates)
        elif case.category == "missing":
            assert not any(
                item.kind == "observation_absent"
                for item in case.expected_predicates
            )
        elif case.category == "conflicting":
            assert not any(
                item.kind == "handoff_event"
                for item in case.expected_predicates
            )
        elif case.category == "mandatory_escalation":
            handoff = next(
                item for item in case.expected_predicates if item.kind == "handoff_event"
            )
            assert handoff.expected_value != "captain"
            assert any(
                isinstance(handoff.expected_value, str)
                and handoff.expected_value.casefold() in sources[source][2].casefold()
                for source in handoff.source_case_keys
            )


def test_all_canonical_suites_total_exactly_forty_five_explicit_cases() -> None:
    suites = tuple(
        generate_private_benchmark_suite(document, seed=b"all-forty-five-source-cases!!" * 2)
        for document in _documents()
    )

    assert sum(len(suite.cases) for suite in suites) == 45
    assert len({case.case_key for suite in suites for case in suite.cases}) == 45


def test_sales_generated_tool_receipts_use_only_the_two_declared_required_tools() -> None:
    sales = _documents()[0]
    suite = generate_private_benchmark_suite(
        sales, seed=b"sales-declared-tools-only-seed!!" * 2,
    )
    observed = {
        predicate.observed_key
        for case in suite.cases
        for predicate in case.expected_predicates
        if predicate.kind == "tool_receipt"
    }

    assert observed == {"account_snapshot_reader", "opportunity_scoring_rules"}
    assert "captain_decision" not in observed


@pytest.mark.parametrize("relative_path", INPUTS)
def test_real_repository_admission_compiler_and_suite_cover_each_canonical_input(
    relative_path: str,
) -> None:
    document = load_factory_input(ROOT / relative_path)
    catalog = InMemoryPrivateBenchmarkCatalog((
        generate_private_benchmark_suite(
            document, seed=("all-three-" + relative_path).encode().ljust(64, b"!"),
        ),
    ))
    holdouts = InMemoryPrivateHoldoutStore()
    revision = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    input_id = "private-" + hashlib.sha256(relative_path.encode()).hexdigest()[:12]
    run_id = _id("run-" + relative_path)
    reservation = CapabilityLiveReservation(
        reservation_id=_id("reservation-" + relative_path),
        run_id=run_id,
        input_id=input_id,
        maximum_cost_usd="1.00",
    )
    admitted = RepositoryCapabilityAssemblyInputAdmission(
        repository_root=ROOT,
        expected_repository_revision=revision,
        expected_sha256_by_path={relative_path: document.input_ref.sha256},
        compiled_tool_gaps_by_source_sha256={document.input_ref.sha256: ()},
        holdout_store=holdouts,
        private_benchmark_catalog=catalog,
        subject_version=1,
        require_private_benchmark_policy=True,
    ).admit(CapabilityAssemblyInputRequest(
        input_id=input_id,
        repository_path=relative_path,
        correlation_id=_id("correlation-" + relative_path),
        reservation=reservation,
    ))
    repository = GenericPrivateBenchmarkRepository(
        holdouts=holdouts,
        executor_authority_id=_id("executor-" + relative_path),
        evaluator_authority_id=_id("evaluator-" + relative_path),
    )
    benchmark = build_generic_benchmark_suite(admitted.input, repository)

    assert len(benchmark.cases) == 15
    assert {item.category.value for item in benchmark.cases} == CATEGORIES
    assert len(holdouts) == 15


def test_private_catalog_compiles_only_opaque_refs_and_never_enters_worker_bytes(
    tmp_path: Path,
) -> None:
    document = _documents()[1]
    sentinel = "captain_private_predicate_sentinel"
    suite = generate_private_benchmark_suite(document, seed=b"c" * 32)
    first = suite.cases[0]
    predicates = list(first.expected_predicates)
    predicates[0] = predicates[0].model_copy(update={"predicate_key": sentinel})
    suite = suite.model_copy(update={
        "cases": (first.model_copy(update={"expected_predicates": tuple(predicates)}), *suite.cases[1:])
    })
    catalog = InMemoryPrivateBenchmarkCatalog((suite,))
    holdouts = InMemoryPrivateHoldoutStore()

    compiled = FactoryInputCompiler(
        holdout_store=holdouts,
        private_benchmark_catalog=catalog,
    ).compile(document, subject_version=1)
    job = build_factory_job_v3(
        compiled,
        correlation_id=_id("private-worker-byte-boundary"),
        now=datetime(2026, 8, 9, tzinfo=timezone.utc),
        execution_policy=FactoryExecutionPolicyV1(
            schema="captain.factory-execution-policy.v1",
            mode=FactoryExecutionMode.RELEASE,
            live_execution=True,
            max_cost_usd=Decimal("1.00"),
            max_runtime_seconds=900,
            required_live_runs=3,
            allowed_models=("fixed-model",),
            live_capabilities=(FactoryLiveCapability.MODEL_INVOKE,),
        ),
    )
    worker_workspace = tmp_path / "worker"
    worker_workspace.mkdir()
    (worker_workspace / "TO_BE_BUILT.md").write_bytes(
        (ROOT / INPUTS[1]).read_bytes()
    )
    (worker_workspace / "job-input.json").write_text(
        job.model_dump_json(), encoding="utf-8",
    )

    public_bytes = (ROOT / INPUTS[1]).read_bytes()
    public_projection = document.model_dump_json() + compiled.model_dump_json()
    assert b"Private benchmark cases" not in public_bytes
    assert b"expected_predicates" not in public_bytes
    assert sentinel not in public_projection
    assert sentinel not in b"".join(
        path.read_bytes() for path in worker_workspace.iterdir()
    ).decode("utf-8")
    assert len(compiled.private_holdout_refs) == 15
    assert sentinel in "".join(holdouts.get(ref.uri) for ref in compiled.private_holdout_refs)


def test_parser_rejects_concrete_private_cases_in_worker_visible_input() -> None:
    source = (ROOT / INPUTS[1]).read_bytes() + b"\n## Private benchmark cases\nCAPTAIN-PRIVATE-SENTINEL\n"

    with pytest.raises(FactoryInputError, match="must not contain private benchmark cases"):
        parse_factory_input_bytes(source, logical_name="TO_BE_BUILT.md")


@pytest.mark.parametrize("heading", (
    "PRIVATE   BENCHMARK CASES",
    "Private\u00a0Benchmark\u00a0Cases",
    "Ｐｒｉｖａｔｅ Ｂｅｎｃｈｍａｒｋ Ｃａｓｅｓ",
    "private-benchmark_case definitions",
    "Hidden evaluation fixtures",
    "Private benchmark expected outcomes",
    "Benchmark case fixtures",
    "Evaluation expected answers",
    "Hidden predicates",
    "Private evaluation",
    "Hidden holdout",
    "PRIVATE benchmark policy",
    "Private\u00a0benchmark policy",
))
def test_parser_rejects_normalized_private_case_heading_before_extra_sections(
    heading: str,
) -> None:
    sentinel = "CAPTAIN-PRIVATE-NORMALIZED-SENTINEL"
    source = (ROOT / INPUTS[0]).read_text(encoding="utf-8")
    source += f"\n## {heading}\n{sentinel}\n"

    with pytest.raises(FactoryInputError, match="must not contain private benchmark cases"):
        parse_factory_input_bytes(source.encode(), logical_name="TO_BE_BUILT.md")


@pytest.mark.parametrize("heading", (
    "Private benchmark policy",
    "Private holdout policy",
))
def test_parser_allows_only_exact_public_private_policy_headings(heading: str) -> None:
    source = (ROOT / INPUTS[0]).read_text(encoding="utf-8")
    source = source.replace("## Private holdout policy", f"## {heading}")

    document = parse_factory_input_bytes(source.encode(), logical_name="TO_BE_BUILT.md")

    assert document.benchmark_policy.public_policy_statement is not None


def test_stale_input_digest_fails_before_any_private_holdout_write() -> None:
    original = _documents()[0]
    catalog = InMemoryPrivateBenchmarkCatalog((
        generate_private_benchmark_suite(original, seed=b"stale-digest-test-seed-value!" * 2),
    ))
    source = (ROOT / INPUTS[0]).read_bytes() + (
        b"\n## Public benchmark notes\nUpdated public request.\n"
    )
    changed = parse_factory_input_bytes(source, logical_name="TO_BE_BUILT.md")
    holdouts = InMemoryPrivateHoldoutStore()

    with pytest.raises(PrivateBenchmarkCatalogError, match="absent"):
        FactoryInputCompiler(
            holdout_store=holdouts,
            private_benchmark_catalog=catalog,
        ).compile(changed, subject_version=1)

    assert len(holdouts) == 0


def test_generator_rejects_safety_predicate_without_tool_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _documents()[0]
    digest = document.input_ref.sha256
    templates = catalog_module._SCENARIO_TEMPLATES[digest]
    unsafe = replace(templates[0], safety_source="security_1")
    monkeypatch.setitem(
        catalog_module._SCENARIO_TEMPLATES,
        digest,
        (unsafe, *templates[1:]),
    )

    with pytest.raises(PrivateBenchmarkCatalogError, match="safety predicate"):
        generate_private_benchmark_suite(document, seed=b"unsafe-source-test-seed-value" * 2)


def test_unknown_source_in_final_case_leaves_holdout_store_empty() -> None:
    document = _documents()[0]
    suite = generate_private_benchmark_suite(document, seed=b"atomic-catalog-seed-value-123456" * 2)
    final = suite.cases[-1].model_copy(update={"source_case_keys": ("unknown_final_source",)})
    catalog = InMemoryPrivateBenchmarkCatalog((
        suite.model_copy(update={"cases": (*suite.cases[:-1], final)}),
    ))
    holdouts = InMemoryPrivateHoldoutStore()

    with pytest.raises(PrivateBenchmarkCatalogError, match="unknown admitted source"):
        FactoryInputCompiler(
            holdout_store=holdouts, private_benchmark_catalog=catalog,
        ).compile(document, subject_version=1)

    assert len(holdouts) == 0


@pytest.mark.parametrize(
    "dimension",
    ("business", "rationale", "safety", "tool", "handoff"),
)
def test_compiler_rejects_per_predicate_source_cross_swaps_before_holdout_effect(
    dimension: str,
) -> None:
    document = _documents()[1]
    suite = generate_private_benchmark_suite(
        document, seed=b"predicate-source-cross-swap-seed" * 2,
    )
    case_index = next(
        index for index, item in enumerate(suite.cases)
        if (item.category == "mandatory_escalation") == (dimension == "handoff")
    )
    case = suite.cases[case_index]
    predicates = list(case.expected_predicates)
    kinds = {
        "business": "observation_contains",
        "rationale": "rationale_contains",
        "safety": "forbidden_tool_absent",
        "tool": "tool_receipt",
        "handoff": "handoff_event",
    }
    target_index = next(
        index for index, item in enumerate(predicates)
        if item.kind == kinds[dimension]
    )
    counterpart_kind = (
        "rationale_contains" if dimension == "safety"
        else "forbidden_tool_absent"
    )
    counterpart_index = next(
        index for index, item in enumerate(predicates)
        if item.kind == counterpart_kind
    )
    target_sources = predicates[target_index].source_case_keys
    counterpart_sources = predicates[counterpart_index].source_case_keys
    predicates[target_index] = predicates[target_index].model_copy(update={
        "source_case_keys": counterpart_sources,
    })
    predicates[counterpart_index] = predicates[counterpart_index].model_copy(update={
        "source_case_keys": target_sources,
    })
    cases = list(suite.cases)
    cases[case_index] = case.model_copy(update={
        "expected_predicates": tuple(predicates),
    })
    catalog = InMemoryPrivateBenchmarkCatalog((
        suite.model_copy(update={"cases": tuple(cases)}),
    ))
    holdouts = InMemoryPrivateHoldoutStore()

    with pytest.raises(PrivateBenchmarkCatalogError, match="predicate source binding"):
        FactoryInputCompiler(
            holdout_store=holdouts, private_benchmark_catalog=catalog,
        ).compile(document, subject_version=1)

    assert len(holdouts) == 0


@pytest.mark.parametrize(
    "dimension",
    ("business", "rationale", "safety", "tool", "handoff"),
)
def test_compiler_rejects_coordinated_source_and_expected_substitutions(
    dimension: str,
) -> None:
    document = _documents()[1]
    suite = generate_private_benchmark_suite(
        document, seed=b"coordinated-source-substitution" * 2,
    )
    case_index = next(
        index for index, item in enumerate(suite.cases)
        if item.category == "mandatory_escalation"
    )
    case = suite.cases[case_index]
    predicates = list(case.expected_predicates)
    by_kind = {item.kind: index for index, item in enumerate(predicates)}
    source_assertions = {
        item.source_path[-1]: item
        for item in FactoryInputCompiler(
            holdout_store=InMemoryPrivateHoldoutStore(),
        )._assertions(document)
    }
    kind_by_dimension = {
        "business": "observation_contains",
        "rationale": "rationale_contains",
        "safety": "forbidden_tool_absent",
        "tool": "tool_receipt",
        "handoff": "handoff_event",
    }
    donor_by_dimension = {
        "business": "forbidden_tool_absent",
        "rationale": "forbidden_tool_absent",
        "safety": "handoff_event",
        "tool": "forbidden_tool_absent",
        "handoff": "observation_contains",
    }
    target_index = by_kind[kind_by_dimension[dimension]]
    donor_index = by_kind[donor_by_dimension[dimension]]
    target = predicates[target_index]
    donor = predicates[donor_index]
    donor_key = donor.source_case_keys[0]
    update: dict[str, object] = {"source_case_keys": (donor_key,)}
    if isinstance(target.expected_value, str):
        update["expected_value"] = source_assertions[donor_key].observable_expected
    if dimension == "tool":
        update["observed_key"] = donor_key.removeprefix("tool_")
    predicates[target_index] = target.model_copy(update=update)

    # Keep every aggregate source present so only exact per-dimension authority
    # can reject the coordinated substitution.
    if dimension in {"safety", "tool", "handoff"}:
        rationale_index = by_kind["rationale_contains"]
        original_key = target.source_case_keys[0]
        predicates[rationale_index] = predicates[rationale_index].model_copy(update={
            "source_case_keys": (original_key,),
            "expected_value": source_assertions[original_key].observable_expected,
        })

    mapped_sources = tuple(dict.fromkeys(
        source
        for predicate in predicates
        for source in predicate.source_case_keys
    ))
    cases = list(suite.cases)
    cases[case_index] = case.model_copy(update={
        "expected_predicates": tuple(predicates),
        "source_case_keys": mapped_sources,
    })
    holdouts = InMemoryPrivateHoldoutStore()
    catalog = InMemoryPrivateBenchmarkCatalog((
        suite.model_copy(update={"cases": tuple(cases)}),
    ))

    with pytest.raises(PrivateBenchmarkCatalogError, match="predicate source binding"):
        FactoryInputCompiler(
            holdout_store=holdouts, private_benchmark_catalog=catalog,
        ).compile(document, subject_version=1)

    assert len(holdouts) == 0


def test_compiler_encodes_canonical_authority_type_for_every_dimension() -> None:
    document = _documents()[1]
    holdouts = InMemoryPrivateHoldoutStore()
    suite = generate_private_benchmark_suite(
        document, seed=b"canonical-predicate-authority" * 2,
    )
    specification = FactoryInputCompiler(
        holdout_store=holdouts,
        private_benchmark_catalog=InMemoryPrivateBenchmarkCatalog((suite,)),
    ).compile(document, subject_version=1)
    authority_by_kind = {
        "observation_contains": "business",
        "rationale_contains": "rationale",
        "forbidden_tool_absent": "safety",
        "tool_receipt": "tool",
        "handoff_event": "handoff",
    }

    for ref in specification.private_holdout_refs:
        payload = json.loads(holdouts.get(ref.uri))
        predicates = {
            item["predicate_key"]: item for item in payload["expected_predicates"]
        }
        for binding in payload["predicate_source_bindings"]:
            predicate = predicates[binding["predicate_key"]]
            assert binding["authority_type"] == authority_by_kind[predicate["kind"]]


def test_file_catalog_is_digest_bound_single_open_and_rotation_safe(tmp_path: Path) -> None:
    documents = _documents()
    root = tmp_path / ".captain-cook" / "private-benchmarks"
    first_ref = write_private_benchmark_catalog(root, documents, seed=b"d" * 32)
    first_bytes = (root / "catalog.json").read_bytes()
    replay_ref = write_private_benchmark_catalog(root, documents, seed=b"d" * 32)
    assert replay_ref == first_ref
    assert (root / "catalog.json").read_bytes() == first_bytes
    with pytest.raises(PrivateBenchmarkCatalogError, match="rotate to a new path"):
        write_private_benchmark_catalog(root, documents, seed=b"e" * 32)
    assert (root / "catalog.json").read_bytes() == first_bytes

    catalog = VerifiedFilePrivateBenchmarkCatalog.open(
        private_root=root,
        manifest_path="catalog.json",
        expected_sha256=first_ref.sha256,
        expected_input_sha256s=tuple(doc.input_ref.sha256 for doc in documents),
    )
    (root / "catalog.json").write_text("{}", encoding="utf-8")
    assert len(catalog.resolve(documents[0].input_ref.sha256).cases) == 15

    rotated_root = tmp_path / "rotated"
    rotated_ref = write_private_benchmark_catalog(rotated_root, documents, seed=b"e" * 32)
    assert rotated_ref.sha256 != first_ref.sha256
    rotated_catalog = VerifiedFilePrivateBenchmarkCatalog.open(
        private_root=rotated_root,
        manifest_path="catalog.json",
        expected_sha256=rotated_ref.sha256,
        expected_input_sha256s=tuple(doc.input_ref.sha256 for doc in documents),
    )
    assert rotated_catalog.suite_identity_sha256 != catalog.suite_identity_sha256


@pytest.mark.parametrize("mutation", ("digest", "escape", "link"))
def test_file_catalog_rejects_mismatch_escape_and_links_before_use(
    tmp_path: Path, mutation: str,
) -> None:
    documents = _documents()
    root = tmp_path / "private"
    ref = write_private_benchmark_catalog(root, documents, seed=b"f" * 32)
    manifest_path = "catalog.json"
    expected = ref.sha256
    if mutation == "digest":
        expected = "0" * 64
    elif mutation == "escape":
        manifest_path = "../private/catalog.json"
    else:
        target = tmp_path / "linked-catalog.json"
        target.write_bytes((root / "catalog.json").read_bytes())
        link = root / "linked.json"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation is unavailable")
        manifest_path = "linked.json"

    with pytest.raises(PrivateBenchmarkCatalogError):
        VerifiedFilePrivateBenchmarkCatalog.open(
            private_root=root,
            manifest_path=manifest_path,
            expected_sha256=expected,
            expected_input_sha256s=tuple(doc.input_ref.sha256 for doc in documents),
        )


def test_file_catalog_rejects_private_root_that_contains_a_forbidden_worktree(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "authority"
    forbidden = private_root / "repository-worktree"
    forbidden.mkdir(parents=True)
    documents = _documents()
    ref = write_private_benchmark_catalog(private_root, documents, seed=b"g" * 32)

    with pytest.raises(PrivateBenchmarkCatalogError, match="overlaps"):
        VerifiedFilePrivateBenchmarkCatalog.open(
            private_root=private_root,
            manifest_path="catalog.json",
            expected_sha256=ref.sha256,
            expected_input_sha256s=tuple(doc.input_ref.sha256 for doc in documents),
            forbidden_roots=(forbidden,),
        )


def test_catalog_writer_rejects_forbidden_root_before_creating_files(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    private_root = repository / ".captain-cook" / "private-benchmarks"

    with pytest.raises(PrivateBenchmarkCatalogError, match="overlaps"):
        write_private_benchmark_catalog(
            private_root,
            _documents(),
            seed=b"writer-confinement-seed-value!!" * 2,
            forbidden_roots=(repository,),
        )

    assert not private_root.exists()


def test_catalog_writer_derives_fake_repository_boundary_before_any_creation(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "fake-repository"
    (repository / ".git").mkdir(parents=True)
    private_root = repository / ".captain-cook" / "private-benchmarks"

    with pytest.raises(PrivateBenchmarkCatalogError, match="repository|worktree|boundary"):
        write_private_benchmark_catalog(
            private_root,
            _documents(),
            seed=b"derived-writer-boundary-seed-value" * 2,
        )

    assert not private_root.exists()
    assert list(repository.iterdir()) == [repository / ".git"]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction swap contract")
def test_catalog_writer_parent_swap_never_writes_plaintext_to_forbidden_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = tmp_path / "private"
    forbidden = tmp_path / "forbidden"
    forbidden.mkdir()
    attempted: list[bool] = []
    original_publish = catalog_module._windows_publish_no_replace

    def racing_publish(source: Path, target: Path) -> bool:
        attempted.append(True)
        parent = target.parent
        displaced = tmp_path / "displaced-parent"
        swapped = False
        try:
            parent.rename(displaced)
            try:
                parent.symlink_to(forbidden, target_is_directory=True)
                swapped = True
            except OSError:
                displaced.rename(parent)
            return original_publish(source, target)
        except OSError:
            return original_publish(source, target)
        finally:
            if swapped:
                parent.unlink()
                displaced.rename(parent)

    monkeypatch.setattr(catalog_module, "_windows_publish_no_replace", racing_publish)
    try:
        write_private_benchmark_catalog(
            private_root, _documents(), seed=b"parent-swap-seed-value-123456789" * 2,
            forbidden_roots=(forbidden,),
        )
    except PrivateBenchmarkCatalogError:
        pass

    assert attempted == [True]
    assert list(forbidden.iterdir()) == []


def test_file_catalog_revalidation_detects_atomic_manifest_rotation(tmp_path: Path) -> None:
    documents = _documents()
    root = tmp_path / "private"
    first = write_private_benchmark_catalog(root, documents, seed=b"h" * 32)
    catalog = VerifiedFilePrivateBenchmarkCatalog.open(
        private_root=root,
        manifest_path="catalog.json",
        expected_sha256=first.sha256,
        expected_input_sha256s=tuple(doc.input_ref.sha256 for doc in documents),
    )
    rotated_root = tmp_path / "rotation"
    write_private_benchmark_catalog(rotated_root, documents, seed=b"i" * 32)
    (root / "catalog.json").write_bytes((rotated_root / "catalog.json").read_bytes())

    with pytest.raises(PrivateBenchmarkCatalogError, match="changed"):
        catalog.revalidate()


def test_catalog_manifest_contains_no_seed_material(tmp_path: Path) -> None:
    documents = _documents()
    root = tmp_path / "private"
    write_private_benchmark_catalog(root, documents, seed=b"secret-seed-material" * 2)
    raw = (root / "catalog.json").read_bytes()
    payload = json.loads(raw)

    assert b"secret-seed-material" not in raw
    assert "seed" not in json.dumps(payload).casefold()
    assert hashlib.sha256(raw).hexdigest()
