"""Render redacted human evidence from a stored evaluation manifest only."""

from __future__ import annotations

from .models import EvaluationManifest
from .redaction import redact_text


def render_evaluation_markdown(manifest: EvaluationManifest) -> str:
    """Return a deterministic report without reading a workspace or calling a service."""

    lines = [
        "# AgentFarm Evaluation Evidence",
        "",
        f"- Run ID: `{_redact(manifest.run_id)}`",
        f"- Status: `{manifest.status.value}`",
        f"- Source reference: `{_redact(manifest.source.source_reference)}`",
        f"- Source digest: `{manifest.source.sha256}`",
        "- Source content: [REDACTED]",
        f"- Model version: `{_redact(manifest.model_identifier)}`",
        f"- Prompt version: `{_redact(manifest.prompt_version)}`",
        f"- Calls: {manifest.call_count}",
        f"- Tokens: {manifest.token_total}",
        f"- Cost: {manifest.cost_total}",
        "",
        _redact(manifest.planning_disclaimer),
        "",
        "## Components",
        "",
    ]
    for component in manifest.component_outcomes:
        lines.extend(
            [
                f"### {_redact(component.component_key)}",
                "",
                f"- Outcome: `{component.outcome.value}`",
                f"- Revision: {component.revision}",
            ]
        )
        if component.candidate is not None:
            lines.append(f"- Planned scope: {_redact(' | '.join(component.candidate.scope))}")
            lines.append(
                f"- Implementation plan: {_redact(' | '.join(component.candidate.implementation_steps))}"
            )
            lines.append(
                f"- Planned acceptance tests: {_redact(' | '.join(test.test_id for test in component.candidate.acceptance_tests))}"
            )
        if component.review is not None:
            lines.append(f"- QA decision: `{_redact(component.review.decision)}` (score {component.review.score})")
        lines.append("")
    lines.extend(["## Artifacts", "", *[f"- `{_redact(digest)}`" for digest in manifest.artifact_digests], ""])
    return "\n".join(lines)


def _redact(value: str) -> str:
    return redact_text(value)
