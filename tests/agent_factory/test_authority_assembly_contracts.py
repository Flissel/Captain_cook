"""Acceptance tests for the production authority assembly contract."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agenten.agent_factory.authority_assembly_contracts import (
    AuthorityAdapterRefV1,
    AuthorityAssemblyError,
    assemble_production_authority,
    canonical_assembly_bytes,
)
from agenten.agent_factory.input_document import load_factory_input

FIXTURE_ROOT = Path("tests/fixtures/to_be_built")
REPOSITORIES = ("repo-a", "repo-b", "repo-c")


def _adapter(role: str, *, digest_seed: str = "a") -> AuthorityAdapterRefV1:
    return AuthorityAdapterRefV1(
        role=role,
        artifact_uri=f"artifact://authority/{role}",
        sha256=digest_seed * 64,
        version="v1",
    )


def _complete_adapters() -> tuple[AuthorityAdapterRefV1, ...]:
    return tuple(
        _adapter(role) for role in ("captain", "gateway", "runtime", "minibook", "n8n")
    )


def test_three_repository_owned_inputs_assemble_into_distinct_assemblies() -> None:
    assemblies = []
    for repository in REPOSITORIES:
        document = load_factory_input(FIXTURE_ROOT / repository / "TO_BE_BUILT.md")
        assemblies.append(
            assemble_production_authority(
                document,
                _complete_adapters(),
                source_repository=repository,
            )
        )
    assembly_ids = {assembly.assembly_id for assembly in assemblies}
    input_digests = {assembly.input_sha256 for assembly in assemblies}
    assert len(assembly_ids) == 3
    assert len(input_digests) == 3
    for assembly in assemblies:
        assert len(assembly.adapters) == 5
        assert tuple(adapter.role for adapter in assembly.adapters) == (
            "captain",
            "gateway",
            "runtime",
            "minibook",
            "n8n",
        )


def test_assembly_is_deterministic_and_canonically_serializable() -> None:
    document = load_factory_input(FIXTURE_ROOT / "repo-a" / "TO_BE_BUILT.md")
    first = assemble_production_authority(
        document, _complete_adapters(), source_repository="repo-a"
    )
    second = assemble_production_authority(
        document, tuple(reversed(_complete_adapters())), source_repository="repo-a"
    )
    assert first == second
    assert canonical_assembly_bytes(first) == canonical_assembly_bytes(second)


def test_missing_role_fails_closed() -> None:
    document = load_factory_input(FIXTURE_ROOT / "repo-a" / "TO_BE_BUILT.md")
    incomplete = tuple(
        adapter for adapter in _complete_adapters() if adapter.role != "minibook"
    )
    with pytest.raises(AuthorityAssemblyError, match="minibook"):
        assemble_production_authority(
            document, incomplete, source_repository="repo-a"
        )


def test_duplicate_role_fails_closed() -> None:
    document = load_factory_input(FIXTURE_ROOT / "repo-a" / "TO_BE_BUILT.md")
    duplicated = _complete_adapters() + (_adapter("gateway", digest_seed="b"),)
    with pytest.raises(AuthorityAssemblyError, match="gateway"):
        assemble_production_authority(
            document, duplicated, source_repository="repo-a"
        )


def test_adapter_ref_rejects_malformed_digest_and_unknown_role() -> None:
    with pytest.raises(ValidationError):
        AuthorityAdapterRefV1(
            role="captain",
            artifact_uri="artifact://authority/captain",
            sha256="not-a-digest",
            version="v1",
        )
    with pytest.raises(ValidationError):
        AuthorityAdapterRefV1(
            role="orchestrator",
            artifact_uri="artifact://authority/orchestrator",
            sha256="a" * 64,
            version="v1",
        )


def test_adapter_ref_rejects_non_artifact_uri() -> None:
    with pytest.raises(ValidationError):
        AuthorityAdapterRefV1(
            role="captain",
            artifact_uri="https://example.invalid/adapter",
            sha256="a" * 64,
            version="v1",
        )


def test_credential_bearing_input_is_rejected_before_assembly() -> None:
    credential_fixture = Path(
        "tests/fixtures/agent_factory/TO_BE_BUILT.credential-bearing.md"
    )
    with pytest.raises(Exception):
        load_factory_input(credential_fixture)
