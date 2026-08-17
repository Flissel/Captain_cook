"""Stable D12 request constants; production topology is owned by D11."""

CANONICAL_INPUT_PATHS = (
    "demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md",
    "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md",
    "demo_inputs/agent_factory/claims_resolution/TO_BE_BUILT.md",
)

CANONICAL_INPUT_IDS = (
    "sales-pipeline-brief",
    "incident-command",
    "claims-resolution",
)

__all__ = ["CANONICAL_INPUT_IDS", "CANONICAL_INPUT_PATHS"]
