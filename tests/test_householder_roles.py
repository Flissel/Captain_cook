"""Tests for the human-authored householder role manifests."""
from pathlib import Path

import pytest

from agenten.household.roles import HouseholderRoleError, load_householder_roles


def test_role_loader_maps_every_documented_role_to_one_runtime_contract():
    roles = load_householder_roles()

    assert [(role.role_id, role.agent_type, role.capability_tags) for role in roles] == [
        ("architect", "householder_architect", ("architecture_review",)),
        ("delivery-builder", "householder_delivery_builder", ("delivery_plan",)),
        ("ledger-steward", "householder_ledger_steward", ("ledger_review",)),
        ("quality-warden", "householder_quality_warden", ("quality_review",)),
    ]
    assert all(role.prompt_path.as_posix().startswith("agents/household/") for role in roles)
    assert all(role.permitted_tools for role in roles)


def test_role_loader_rejects_a_definition_without_required_frontmatter(tmp_path: Path):
    (tmp_path / "architect.md").write_text("# Missing frontmatter\n", encoding="utf-8")

    with pytest.raises(HouseholderRoleError, match="YAML frontmatter"):
        load_householder_roles(tmp_path)
