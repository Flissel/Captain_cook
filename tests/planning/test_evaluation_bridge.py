from __future__ import annotations

import hashlib

import pytest

from agenten.evaluation.models import (
    AcceptanceTestPlan,
    ComponentOutcome,
    ComponentPlanCandidate,
    EvaluationManifest,
    EvaluationOutcome,
    EvaluationSource,
    EvaluationStatus,
    QaReview,
    SourceBlock,
)
from agenten.planning.evaluation_bridge import (
    EvaluationBridgeError,
    EvaluationBridgePolicy,
    compile_accepted_evaluation,
    release_accepted_evaluation,
)
from agenten.planning.captain_pipeline import CaptainPipeline
from agenten.planning.run_models import CaptainRunConflictError
from agenten.planning.run_store import JsonCaptainRunStore
from agenten.evaluation.release_cli import async_main as release_cli_main


def _source() -> EvaluationSource:
    text = "# Foundation\nBuild the deterministic boundary."
    return EvaluationSource(
        source_reference="agentfarm/input.md",
        sha256="a" * 64,
        byte_length=len(text.encode()),
        blocks=(
            SourceBlock(
                block_id="block-0001",
                heading_path=("Foundation",),
                line_start=1,
                line_end=2,
                sha256=hashlib.sha256(text.encode()).hexdigest(),
                text=text,
            ),
        ),
    )


def _candidate(key: str, *, dependencies: tuple[str, ...] = ()) -> ComponentPlanCandidate:
    return ComponentPlanCandidate(
        component_key=key,
        scope=(f"Own {key}.",),
        non_goals=("Do not deploy.",),
        team_roles=("Builder",),
        implementation_steps=("Implement the typed boundary.",),
        interfaces=("TypedAdapter.run",),
        acceptance_tests=(
            AcceptanceTestPlan(
                test_id=f"{key}-unit",
                test_type="unit",
                setup="Create a deterministic fixture.",
                action="Invoke the adapter.",
                expected="A typed receipt is returned.",
                command="python -m pytest -q tests/unit",
            ),
        ),
        definition_of_done=("The planned unit test passes.",),
        risks=("Interface drift.",),
        dependencies=dependencies,
        source_citations=("block-0001",),
    )


def _manifest(*outcomes: ComponentOutcome, status: EvaluationStatus = EvaluationStatus.ACCEPTED) -> EvaluationManifest:
    return EvaluationManifest(
        run_id="eval-001",
        idempotency_key="input-v1",
        status=status,
        source=_source(),
        component_outcomes=outcomes,
        model_identifier="test-model",
        prompt_version="test-v1",
        artifact_digests=("component-inventory.json:abc",),
    )


def _accepted(key: str, *, dependencies: tuple[str, ...] = ()) -> ComponentOutcome:
    candidate = _candidate(key, dependencies=dependencies)
    return ComponentOutcome(
        component_key=key,
        outcome=EvaluationOutcome.ACCEPTED,
        revision=1,
        candidate=candidate,
        review=QaReview(
            component_key=key,
            revision=1,
            decision="approved",
            score=7,
            defect_codes=(),
            revision_requests=(),
        ),
    )


def _policy() -> EvaluationBridgePolicy:
    return EvaluationBridgePolicy(
        target="codex",
        capability_tags=("codex-cli",),
        allowed_targets=frozenset({"codex"}),
        allowed_capability_tags=frozenset({"codex-cli"}),
    )


def test_compiles_only_accepted_evaluation_into_source_bound_dependency_dag() -> None:
    compiled = compile_accepted_evaluation(
        _manifest(_accepted("foundation"), _accepted("delivery", dependencies=("foundation",))),
        policy=_policy(),
    )

    assert [batch.title for batch in compiled.batches] == ["foundation", "delivery"]
    assert compiled.batches[1].depends_on == [compiled.batches[0].batch_id]
    assert compiled.batches[0].capability_tags == ["codex-cli"]
    assert compiled.batches[0].acceptance_criteria[0].expected == "passed"
    assert "A typed receipt is returned." in compiled.batches[0].acceptance_criteria[0].description
    assert compiled.holdouts[0].batch_id == compiled.batches[0].batch_id
    assert any("Evaluation source SHA-256" in item for item in compiled.batches[0].constraints)


def test_rejects_non_accepted_or_unreviewed_evaluation() -> None:
    candidate = _candidate("foundation")
    unresolved = ComponentOutcome(
        component_key="foundation",
        outcome=EvaluationOutcome.UNRESOLVED,
        revision=1,
        candidate=candidate,
    )

    with pytest.raises(EvaluationBridgeError, match="accepted"):
        compile_accepted_evaluation(_manifest(unresolved, status=EvaluationStatus.PARTIAL), policy=_policy())


def test_rejects_unknown_dependency_and_unapproved_capability() -> None:
    with pytest.raises(EvaluationBridgeError, match="unknown component"):
        compile_accepted_evaluation(_manifest(_accepted("delivery", dependencies=("missing",))), policy=_policy())

    with pytest.raises(EvaluationBridgeError, match="unknown capability"):
        compile_accepted_evaluation(
            _manifest(_accepted("foundation")),
            policy=EvaluationBridgePolicy(
                target="codex",
                capability_tags=("n8n-builder",),
                allowed_targets=frozenset({"codex"}),
                allowed_capability_tags=frozenset({"codex-cli"}),
            ),
        )


@pytest.mark.asyncio
async def test_release_checkpoints_accepted_evaluation_without_duplicate_gateway_publish(tmp_path) -> None:
    class RecordingRelease:
        def __init__(self) -> None:
            self.batch_ids: list[str] = []

        async def release(self, batch, holdouts) -> None:
            assert batch.batch_id == holdouts.batch_id
            self.batch_ids.append(batch.batch_id)

    async def unused_decompose(_: str):
        raise AssertionError("evaluation bridge must not invoke another LLM decomposition")

    async def unused_align(*_):
        raise AssertionError("evaluation bridge must not invoke alignment")

    async def unused_enrich(*_):
        raise AssertionError("evaluation bridge must not invoke enrichment")

    release = RecordingRelease()
    pipeline = CaptainPipeline(
        decompose=unused_decompose,
        align=unused_align,
        enrich=unused_enrich,
        release_client=release,
        run_store=JsonCaptainRunStore(tmp_path / "runs"),
        target="codex",
    )
    manifest = _manifest(_accepted("foundation"))

    await release_accepted_evaluation(manifest, policy=_policy(), pipeline=pipeline, run_id="evaluation-001")
    await release_accepted_evaluation(manifest, policy=_policy(), pipeline=pipeline, run_id="evaluation-001")

    assert len(release.batch_ids) == 1
    changed = manifest.model_copy(update={"token_total": 1})
    with pytest.raises(CaptainRunConflictError, match="different project"):
        await release_accepted_evaluation(changed, policy=_policy(), pipeline=pipeline, run_id="evaluation-001")


@pytest.mark.asyncio
async def test_release_cli_publishes_an_accepted_manifest_without_llm_calls(tmp_path, capsys) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(_manifest(_accepted("foundation")).model_dump_json(), encoding="utf-8")

    exit_code = await release_cli_main(
        [
            str(manifest_path),
            "--capability",
            "codex-cli",
            "--run-id",
            "evaluation-001",
            "--output",
            str(tmp_path / "release"),
            "--run-dir",
            str(tmp_path / "runs"),
        ]
    )

    assert exit_code == 0
    assert '"status": "released"' in capsys.readouterr().out
    assert len(list((tmp_path / "release" / "batches").glob("*.json"))) == 1
