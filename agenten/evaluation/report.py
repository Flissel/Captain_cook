"""Render redacted human evidence from a stored evaluation manifest only."""

from __future__ import annotations

import re

from .models import EvaluationManifest


_SECRET_VALUE = re.compile(r"(?im)^([ \t]*[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN)|[ \t]*password)=([^\r\n]*)$")


def render_evaluation_markdown(manifest: EvaluationManifest) -> str:
    """Return a deterministic report without reading a workspace or calling a service."""

    lines = [
        "# AgentFarm Evaluation Evidence",
        "",
        f"- Run ID: `{manifest.run_id}`",
        f"- Status: `{manifest.status.value}`",
        f"- Source reference: `{manifest.source.source_reference}`",
        f"- Source digest: `{manifest.source.sha256}`",
        "- Source content: [REDACTED]",
        f"- Model version: `{manifest.model_identifier}`",
        f"- Prompt version: `{manifest.prompt_version}`",
        f"- Calls: {manifest.call_count}",
        f"- Tokens: {manifest.token_total}",
        f"- Cost: {manifest.cost_total}",
        "",
        manifest.planning_disclaimer,
        "",
        "## Components",
        "",
    ]
    for component in manifest.component_outcomes:
        lines.extend(
            [
                f"### {component.component_key}",
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
            lines.append(f"- QA decision: `{component.review.decision}` (score {component.review.score})")
        lines.append("")
    lines.extend(["## Artifacts", "", *[f"- `{digest}`" for digest in manifest.artifact_digests], ""])
    return "\n".join(lines)


def _redact(value: str) -> str:
    return _SECRET_VALUE.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
