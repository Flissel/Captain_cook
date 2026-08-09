from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import os
from uuid import UUID, uuid5

import pytest

from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
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
def test_each_generated_category_is_a_grounded_behavioral_transformation(
    relative_path: str,
) -> None:
    document = load_factory_input(ROOT / relative_path)
    suite = generate_private_benchmark_suite(document, seed=b"semantic-transform-seed-value!!" * 2)
    sources = {
        case.case_key: (case.observable_setup, case.observable_action, case.observable_expected)
        for case in (*document.acceptance_outcomes, *document.real_cases)
    }
    sources.update({
        case.case_key: (case.observable_setup, case.observable_action, case.observable_expected)
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
        f"authority_{index}": (
            "Given a bounded Captain authority", "Exercise the authority boundary", rule,
        )
        for index, rule in enumerate(document.authority_boundaries, 1)
    })
    sources.update({
        f"security_{index}": (
            "Given the compiled capability", "Exercise the security boundary", rule,
        )
        for index, rule in enumerate(document.security_requirements, 1)
    })
    sources.update({
        f"stop_{index}": (
            "Given a controlled failure", "Exercise the stop condition", rule,
        )
        for index, rule in enumerate(document.stop_conditions, 1)
    })

    assert len(suite.cases) == 15
    for case in suite.cases:
        assert set(case.source_case_keys) <= set(sources)
        cited = [sources[key] for key in case.source_case_keys]
        predicate_values = {
            predicate.expected_value for predicate in case.expected_predicates
            if isinstance(predicate.expected_value, str)
        }
        if case.category == "normal":
            assert case.observable_setup == cited[0][0]
            assert case.observable_action == cited[0][1]
            assert cited[0][2] in predicate_values
        elif case.category == "missing":
            assert "Required evidence is absent" in case.observable_setup
            assert cited[0][0] in case.observable_setup
            assert cited[0][1] in case.observable_action
            assert cited[0][2] in predicate_values
        elif case.category == "conflicting":
            assert len(cited) >= 3
            assert cited[0][2] in case.observable_setup
            assert cited[1][2] in case.observable_setup
            assert cited[0][2] in predicate_values
            assert cited[1][2] in predicate_values
            assert any(item.kind == "handoff_event" for item in case.expected_predicates)
        elif case.category == "boundary":
            assert "Explicit boundary condition" in case.observable_setup
            assert cited[0][0] in case.observable_setup
            assert cited[0][2] in predicate_values
            assert any(token in " ".join(cited[0]).casefold() for token in (
                "tie", "optional", "unavailable", "threshold", "limit", "ninety", "eight", "seven",
                "digest mismatch", "five handoffs", "sufficient structured",
            ))
        else:
            assert len(cited) == 2
            assert cited[0][2] in case.observable_setup
            assert cited[1][2] in case.observable_setup
            handoff = next(
                item for item in case.expected_predicates if item.kind == "handoff_event"
            )
            assert handoff.expected_value.casefold() in cited[1][2].casefold()
            assert handoff.expected_value != "captain"


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


def test_private_catalog_compiles_only_opaque_refs_and_never_enters_public_input() -> None:
    document = _documents()[1]
    sentinel = "CAPTAIN-PRIVATE-EXPECTED-SENTINEL"
    suite = generate_private_benchmark_suite(document, seed=b"c" * 32)
    first = suite.cases[0]
    predicates = list(first.expected_predicates)
    predicates[0] = predicates[0].model_copy(update={"expected_value": sentinel})
    suite = suite.model_copy(update={
        "cases": (first.model_copy(update={"expected_predicates": tuple(predicates)}), *suite.cases[1:])
    })
    catalog = InMemoryPrivateBenchmarkCatalog((suite,))
    holdouts = InMemoryPrivateHoldoutStore()

    compiled = FactoryInputCompiler(
        holdout_store=holdouts,
        private_benchmark_catalog=catalog,
    ).compile(document, subject_version=1)

    public_bytes = (ROOT / INPUTS[1]).read_bytes()
    public_projection = document.model_dump_json() + compiled.model_dump_json()
    assert b"Private benchmark cases" not in public_bytes
    assert b"expected_predicates" not in public_bytes
    assert sentinel not in public_projection
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
))
def test_parser_rejects_normalized_private_case_heading_before_extra_sections(
    heading: str,
) -> None:
    sentinel = "CAPTAIN-PRIVATE-NORMALIZED-SENTINEL"
    source = (ROOT / INPUTS[0]).read_text(encoding="utf-8")
    source += f"\n## {heading}\n{sentinel}\n"

    with pytest.raises(FactoryInputError, match="must not contain private benchmark cases"):
        parse_factory_input_bytes(source.encode(), logical_name="TO_BE_BUILT.md")


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


def test_file_catalog_is_digest_bound_single_open_and_rotation_safe(tmp_path: Path) -> None:
    documents = _documents()
    root = tmp_path / ".captain-cook" / "private-benchmarks"
    first_ref = write_private_benchmark_catalog(root, documents, seed=b"d" * 32)
    first_bytes = (root / "catalog.json").read_bytes()
    replay_ref = write_private_benchmark_catalog(root, documents, seed=b"d" * 32)
    assert replay_ref == first_ref
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
