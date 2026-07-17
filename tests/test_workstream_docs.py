from pathlib import Path


def test_main_is_the_canonical_integration_baseline() -> None:
    text = Path("docs/WORKSTREAMS.md").read_text(encoding="utf-8")

    assert "`main` is the canonical integration baseline" in text
    assert "`feat/devpost-demo-readiness` is the current reviewable baseline" not in text


def test_modular_workstream_plan_defines_the_household_roles():
    plan = Path("docs/WORKSTREAMS.md").read_text(encoding="utf-8")

    assert "feat/ledger-gateway" in plan
    assert "feat/householder-runtime-contract" in plan
    assert "feat/householder-runtime" in plan
    assert "feat/captain-pipeline" in plan
    assert "feat/n8n-delivery" in plan
    assert "feat/worker-fleet" in plan
    assert "feat/release-evidence" in plan
    assert Path("agents/household/architect.md").is_file()
    assert Path("agents/household/ledger-steward.md").is_file()
    assert Path("agents/household/delivery-builder.md").is_file()
    assert Path("agents/household/quality-warden.md").is_file()
