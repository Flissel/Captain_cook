import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from agenten.planning.canonical_plan import (
    CanonicalPlanCompiler,
    CanonicalPlanPublisher,
    CanonicalWorkPackage,
    PlanPublishConflictError,
    WorkPackageStatus,
)
from agenten.planning.input_parser import MarkdownProjectInputParser
from agenten.validation.contracts import AcceptanceAssertion, AssertionKind, WorkBatch


def make_batch(batch_id: str, *, depends_on: list[str] | None = None, reused: bool = False) -> WorkBatch:
    return WorkBatch(
        batch_id=batch_id,
        title=batch_id.replace("-", " ").title(),
        goal=f"Deliver {batch_id}",
        subtask_ids=[f"sub-{batch_id}"],
        target="autogen" if batch_id == "agent-team" else "n8n",
        depends_on=depends_on or [],
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id=f"{batch_id}-done",
                kind=AssertionKind.STATUS_EQUALS,
                expected="succeeded",
            )
        ],
        satisfied_by=f"capability:{batch_id}:v1" if reused else None,
    )


def test_compiler_creates_a_deterministic_canonical_plan_and_five_worker_pool(tmp_path: Path) -> None:
    source = tmp_path / "input.md"
    source.write_text("# Goal\n\nBuild tools and an agent team.\n", encoding="utf-8")
    parsed = MarkdownProjectInputParser().parse(source, source_reference="input.md")
    batches = [
        make_batch("score-lead", reused=True),
        make_batch("agent-team", depends_on=["score-lead"]),
    ]
    compiler = CanonicalPlanCompiler(minimum_workers=5)

    first = compiler.compile(parsed, batches)
    second = compiler.compile(parsed, batches)

    assert first == second
    assert len(first.worker_pool) == 5
    assert [package.status.value for package in first.work_packages] == ["reused", "planned"]
    assert [package.handoff for package in first.work_packages] == [
        "HANDOFF TO WORKER 1",
        "HANDOFF TO WORKER 2",
    ]
    assert first.work_packages[1].depends_on == ("score-lead",)
    assert len({package.batch_id for package in first.work_packages}) == 2


def test_compiler_canonicalizes_equivalent_batch_order(tmp_path: Path) -> None:
    source = tmp_path / "input.md"
    source.write_text("# Goal\n\nBuild.\n", encoding="utf-8")
    parsed = MarkdownProjectInputParser().parse(source)
    foundation = make_batch("foundation")
    alpha = make_batch("alpha", depends_on=["foundation"])
    beta = make_batch("beta", depends_on=["foundation"])
    compiler = CanonicalPlanCompiler(minimum_workers=5)

    first = compiler.compile(parsed, [beta, foundation, alpha])
    second = compiler.compile(parsed, [alpha, beta, foundation])

    assert first == second
    assert [package.batch_id for package in first.work_packages] == ["foundation", "alpha", "beta"]


def test_handoff_number_must_match_worker_id() -> None:
    with pytest.raises(ValidationError, match="handoff worker number"):
        CanonicalWorkPackage(
            batch=make_batch("build"),
            status=WorkPackageStatus.PLANNED,
            worker_id="worker-02",
            handoff="HANDOFF TO WORKER 1",
        )


def test_canonical_plan_cannot_be_mutated_through_nested_batch_lists(tmp_path: Path) -> None:
    source = tmp_path / "input.md"
    source.write_text("# Goal\n\nBuild.\n", encoding="utf-8")
    parsed = MarkdownProjectInputParser().parse(source)
    plan = CanonicalPlanCompiler().compile(
        parsed,
        [make_batch("foundation"), make_batch("build", depends_on=["foundation"])],
    )
    original = plan.model_dump_json()

    plan.work_packages[1].batch.depends_on.append("injected")

    assert plan.model_dump_json() == original
    assert plan.work_packages[1].depends_on == ("foundation",)


def test_dependency_edge_order_does_not_change_plan_identity(tmp_path: Path) -> None:
    source = tmp_path / "input.md"
    source.write_text("# Goal\n\nBuild.\n", encoding="utf-8")
    parsed = MarkdownProjectInputParser().parse(source)
    compiler = CanonicalPlanCompiler()
    left = make_batch("left")
    right = make_batch("right")

    first = compiler.compile(parsed, [left, right, make_batch("join", depends_on=["left", "right"])])
    second = compiler.compile(parsed, [right, left, make_batch("join", depends_on=["right", "left"])])

    assert first == second


def test_publisher_archives_input_and_writes_one_canonical_plan_tree(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    raw = b"# Product goal\r\n\r\nBuild autonomously.\r\n"
    source.write_bytes(raw)
    parsed = MarkdownProjectInputParser().parse(source, source_reference="Autogen_AgentFarm/input.md")
    plan = CanonicalPlanCompiler(minimum_workers=5).compile(parsed, [make_batch("score-lead")])
    output = tmp_path / "release"

    CanonicalPlanPublisher(output).publish(parsed, plan)

    assert (output / "source" / "input.md").read_bytes() == raw
    manifest = json.loads((output / "plans" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["plan_id"] == plan.plan_id
    assert manifest["input_sha256"] == parsed.sha256
    index = (output / "plans" / "index.md").read_text(encoding="utf-8")
    assert index.count("score-lead") == 1
    assert "HANDOFF TO WORKER 1" in index
    assert (output / "plans" / "requirements" / "product-goal.md").exists()
    assert (output / "plans" / "architecture" / "dependency-dag.md").exists()
    assert (output / "plans" / "implementation" / "work-packages.md").exists()
    assert (output / "plans" / "reports" / "latest-evaluation.md").exists()


def test_publish_failure_leaves_no_partial_bundle(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Goal\n\nBuild.\n", encoding="utf-8")
    parsed = MarkdownProjectInputParser().parse(source)
    plan = CanonicalPlanCompiler().compile(parsed, [make_batch("build")])
    output = tmp_path / "release"

    class FailingPublisher(CanonicalPlanPublisher):
        def _write_tree(self, stage, project_input, canonical_plan, holdouts=()):
            super()._write_tree(stage, project_input, canonical_plan, holdouts)
            raise RuntimeError("injected publication failure")

    with pytest.raises(RuntimeError, match="injected"):
        FailingPublisher(output).publish(parsed, plan)

    assert not output.exists()
    assert list(tmp_path.glob(".release-*.tmp")) == []


def test_publisher_rejects_same_bytes_under_a_different_source_reference(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Goal\n\nBuild.\n", encoding="utf-8")
    first_input = MarkdownProjectInputParser().parse(source, source_reference="first/input.md")
    second_input = MarkdownProjectInputParser().parse(source, source_reference="second/input.md")
    plan = CanonicalPlanCompiler().compile(first_input, [make_batch("build")])
    output = tmp_path / "release"

    with pytest.raises(ValueError, match="does not belong"):
        CanonicalPlanPublisher(output).publish(second_input, plan)

    assert not output.exists()


def test_concurrent_identical_publishers_are_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Goal\n\nBuild.\n", encoding="utf-8")
    parsed = MarkdownProjectInputParser().parse(source)
    plan = CanonicalPlanCompiler().compile(parsed, [make_batch("build")])
    output = tmp_path / "release"

    def publish() -> Path:
        return CanonicalPlanPublisher(output).publish(parsed, plan)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish(), range(2)))

    assert results == [output / "plans", output / "plans"]
    assert json.loads((output / "plans" / "manifest.json").read_text(encoding="utf-8"))[
        "plan_id"
    ] == plan.plan_id
    assert list(tmp_path.glob(".release-*.tmp")) == []


def test_idempotent_publish_rejects_a_corrupted_existing_bundle(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Goal\n\nBuild.\n", encoding="utf-8")
    parsed = MarkdownProjectInputParser().parse(source)
    plan = CanonicalPlanCompiler().compile(parsed, [make_batch("build")])
    output = tmp_path / "release"
    publisher = CanonicalPlanPublisher(output)
    publisher.publish(parsed, plan)
    (output / "contracts" / "batches" / "build.json").unlink()

    with pytest.raises(PlanPublishConflictError, match="different canonical plan"):
        publisher.publish(parsed, plan)
