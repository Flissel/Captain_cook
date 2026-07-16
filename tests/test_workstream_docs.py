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


def test_orchestrator_ack_and_worker_handoff_protocol_are_tracked():
    ack = Path("docs/superpowers/IMPLEMENTATION_ACK.md").read_text(encoding="utf-8")
    orchestrator = Path(
        "docs/superpowers/prompts/2026-07-16-orchestrator-loop-goal.md"
    ).read_text(encoding="utf-8")
    workers = Path(
        "docs/superpowers/prompts/2026-07-16-worker-goals.md"
    ).read_text(encoding="utf-8")

    assert "ACK_OWNER: ORCHESTRATOR_ONLY" in ack
    assert "MAX_PARALLEL_WORKERS: 3" in ack
    assert "SCHEDULE_STATE: PENDING_USER_CONFIRMATION" in ack
    assert "HANDOFF TO WORKER 1" in workers
    assert "HANDOFF FROM WORKER 1" in workers
    assert "HANDOFF TO WORKER <ID>" in orchestrator
    assert "HANDOFF FROM WORKER <ID>" in orchestrator
    assert "concurrent duplicate root batches" in ack
    assert "previous_hash" in ack
