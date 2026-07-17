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
    assert "SCHEDULE_STATE: PENDING_USER_CONFIRMATION_AND_NATIVE_UI" in ack
    assert "HANDOFF TO WORKER 1" in workers
    assert "HANDOFF FROM WORKER 1" in workers
    assert "HANDOFF TO WORKER <ID>" in orchestrator
    assert "HANDOFF FROM WORKER <ID>" in orchestrator
    assert "concurrent duplicate root batches" in ack
    assert "previous_hash" in ack


def test_agent_factory_program_has_a_canonical_input_and_spec_index():
    input_document = Path("input.md").read_text(encoding="utf-8")
    index = Path("plans/index.md").read_text(encoding="utf-8")

    assert "AutoGen" in input_document
    assert "n8n" in input_document
    assert "Hermes" in input_document
    assert "Minibook" in input_document
    assert "three consecutive successful E2E runs" in input_document

    for specification in (
        "requirements.md",
        "architecture.md",
        "audit.md",
        "implementation.md",
        "test-spec.md",
    ):
        assert Path("plans", specification).is_file()
        assert f"({specification})" in index

    audit = Path("plans/audit.md").read_text(encoding="utf-8")
    assert "availableInMCP: false" in audit
    assert "docker compose down -v" in audit
    assert "Safe minimum" in audit
    assert "User decision" in audit
