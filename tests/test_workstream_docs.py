from pathlib import Path


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


def test_workstreams_name_mariadb_gateway_as_delivery_truth():
    plan = Path("docs/WORKSTREAMS.md").read_text(encoding="utf-8")

    assert "MariaDB gateway is the sole production delivery source of truth" in plan
    assert "SQLite delivery ledger is a production control plane" not in plan
