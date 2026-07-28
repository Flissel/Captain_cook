from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from agenten.agent_factory.business_benchmark_contracts import BusinessCaseCategory
from agenten.agent_factory.business_benchmark_provisioning import (
    CLAIMS_PROFILE_ID,
    RENEWAL_PROFILE_ID,
    CaptainPrivateBusinessBenchmarkSuiteLoader,
    CanonicalPrivateBusinessBenchmarkProvisioner,
    default_private_business_benchmark_root,
)
from agenten.agent_factory.business_benchmark_store import BusinessBenchmarkConflictError


SEED_VERSION_ID = "canonical-business-benchmark-2026-07"


def private_root(tmp_path: Path) -> Path:
    return tmp_path / ".captain-cook" / "private" / "business-benchmarks"


def test_provisions_deterministic_anonymized_suites_with_exact_category_coverage(
    tmp_path: Path,
) -> None:
    root = private_root(tmp_path)
    provisioner = CanonicalPrivateBusinessBenchmarkProvisioner(root)

    first = provisioner.provision(suite_version=3, seed_version_id=SEED_VERSION_ID)
    second = provisioner.provision(suite_version=3, seed_version_id=SEED_VERSION_ID)
    loader = CaptainPrivateBusinessBenchmarkSuiteLoader(root)

    assert first == second
    assert tuple(item.profile_id for item in first.suites) == (
        CLAIMS_PROFILE_ID,
        RENEWAL_PROFILE_ID,
    )
    for item in first.suites:
        suite = loader.load_suite(
            item.suite_ref,
            expected_profile_id=item.profile_id,
            expected_suite_version=item.suite_version,
        )
        assert len(suite.cases) == 15
        assert Counter(case.category for case in suite.cases) == {
            category: 3 for category in BusinessCaseCategory
        }
        assert len({case.case_id for case in suite.cases}) == 15
        assert all(case.profile_id == item.profile_id for case in suite.cases)


def test_public_provisioning_result_never_contains_case_bodies(tmp_path: Path) -> None:
    provisioned = CanonicalPrivateBusinessBenchmarkProvisioner(private_root(tmp_path)).provision(
        suite_version=1,
        seed_version_id=SEED_VERSION_ID,
    )

    serialized = provisioned.model_dump_json()

    assert "redacted_input" not in serialized
    assert "expected_decision" not in serialized
    assert "required_rationale_fact_ids" not in serialized
    assert "cases" not in serialized
    assert len(provisioned.suites) == 2


def test_suite_bodies_contain_no_pii_secrets_endpoints_or_local_paths(tmp_path: Path) -> None:
    root = private_root(tmp_path)
    provisioned = CanonicalPrivateBusinessBenchmarkProvisioner(root).provision(
        suite_version=1,
        seed_version_id=SEED_VERSION_ID,
    )
    loader = CaptainPrivateBusinessBenchmarkSuiteLoader(root)

    serialized = "\n".join(
        loader.load_suite(
            item.suite_ref,
            expected_profile_id=item.profile_id,
            expected_suite_version=1,
        ).model_dump_json()
        for item in provisioned.suites
    ).lower()

    for forbidden in (
        "email",
        "customer_name",
        "person_name",
        "claim_number",
        "contract_number",
        "api_key",
        "password",
        "authorization",
        "bearer ",
        "http://",
        "https://",
        "file:",
        "c:\\\\",
        "../",
        "/users/",
    ):
        assert forbidden not in serialized


def test_profile_version_is_write_once_and_conflicting_seed_fails_closed(
    tmp_path: Path,
) -> None:
    root = private_root(tmp_path)
    provisioner = CanonicalPrivateBusinessBenchmarkProvisioner(root)
    provisioner.provision(suite_version=1, seed_version_id=SEED_VERSION_ID)
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }

    with pytest.raises(BusinessBenchmarkConflictError, match="profile and version"):
        provisioner.provision(
            suite_version=1,
            seed_version_id="different-canonical-seed-2026-07",
        )

    assert {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    } == before


def test_loader_rejects_tampered_suite_and_digest_mismatch(tmp_path: Path) -> None:
    root = private_root(tmp_path)
    provisioned = CanonicalPrivateBusinessBenchmarkProvisioner(root).provision(
        suite_version=1,
        seed_version_id=SEED_VERSION_ID,
    )
    selected = provisioned.suites[0]
    suite_path = root / "private-suites" / f"{selected.suite_ref.holdout_id}.json"
    suite_path.write_bytes(suite_path.read_bytes() + b" ")

    with pytest.raises(BusinessBenchmarkConflictError, match="content"):
        CaptainPrivateBusinessBenchmarkSuiteLoader(root).load_suite(
            selected.suite_ref,
            expected_profile_id=selected.profile_id,
            expected_suite_version=1,
        )


def test_loader_requires_the_canonical_digest_bound_reference(tmp_path: Path) -> None:
    root = private_root(tmp_path)
    provisioned = CanonicalPrivateBusinessBenchmarkProvisioner(root).provision(
        suite_version=1,
        seed_version_id=SEED_VERSION_ID,
    )
    claims = provisioned.suites[0]
    renewal = provisioned.suites[1]

    with pytest.raises(BusinessBenchmarkConflictError, match="canonical suite reference"):
        CaptainPrivateBusinessBenchmarkSuiteLoader(root).load_suite(
            renewal.suite_ref,
            expected_profile_id=claims.profile_id,
            expected_suite_version=1,
        )


def test_private_loader_resolves_both_suites_only_from_provisioned_references(
    tmp_path: Path,
) -> None:
    root = private_root(tmp_path)
    provisioned = CanonicalPrivateBusinessBenchmarkProvisioner(root).provision(
        suite_version=2,
        seed_version_id=SEED_VERSION_ID,
    )

    claims, renewal = CaptainPrivateBusinessBenchmarkSuiteLoader(
        root
    ).load_provisioned_suites(provisioned)

    assert claims.profile_id == CLAIMS_PROFILE_ID
    assert renewal.profile_id == RENEWAL_PROFILE_ID
    assert claims.suite_version == renewal.suite_version == 2


@pytest.mark.parametrize(
    "unsafe_seed",
    [
        "sk-demo-credential-value",
        "benchmark-api-key",
        "benchmark-secret-token",
        "authorization-value",
    ],
)
def test_secret_shaped_seed_identifier_is_rejected_before_writes(
    tmp_path: Path, unsafe_seed: str
) -> None:
    root = private_root(tmp_path)

    with pytest.raises(ValueError, match="non-secret"):
        CanonicalPrivateBusinessBenchmarkProvisioner(root).provision(
            suite_version=1,
            seed_version_id=unsafe_seed,
        )

    assert not root.exists()


def test_private_root_must_use_the_gitignored_captain_namespace(tmp_path: Path) -> None:
    assert default_private_business_benchmark_root(tmp_path) == private_root(tmp_path)

    with pytest.raises(ValueError, match=".captain-cook"):
        CanonicalPrivateBusinessBenchmarkProvisioner(tmp_path / "public-artifacts")
