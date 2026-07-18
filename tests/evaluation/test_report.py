import hashlib

from agenten.evaluation.models import (
    ComponentOutcome,
    EvaluationManifest,
    EvaluationOutcome,
    EvaluationSource,
    EvaluationStatus,
    SourceBlock,
)
from agenten.evaluation.report import render_evaluation_markdown


def test_report_is_reproducible_from_manifest_and_redacts_credentials() -> None:
    text = "# Delivery\nOPENAI_API_KEY=[REDACTED]"
    source = EvaluationSource(
        source_reference="inputs/project.md",
        sha256="a" * 64,
        byte_length=len(text.encode("utf-8")),
        blocks=(
            SourceBlock(
                block_id="block-0001",
                heading_path=("Delivery",),
                line_start=1,
                line_end=2,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                text=text,
            ),
        ),
    )
    manifest = EvaluationManifest(
        run_id="eval-001",
        idempotency_key="input-v1",
        status=EvaluationStatus.PARTIAL,
        source=source,
        component_outcomes=(ComponentOutcome(component_key="delivery-api", outcome=EvaluationOutcome.UNRESOLVED, revision=1),),
        model_identifier="planned-model-v1",
        prompt_version="PASSWORD = rendered-secret",
        call_count=0,
        token_total=0,
        cost_total=0.0,
        artifact_digests=("source-manifest.json:" + "a" * 64,),
    )

    report = render_evaluation_markdown(manifest)

    assert report == render_evaluation_markdown(manifest)
    assert "Acceptance tests are planned, not executed by this evaluation." in report
    assert "[REDACTED]" in report
    assert "rendered-secret" not in report
    assert "planned-model-v1" in report
    assert "delivery-api" in report
