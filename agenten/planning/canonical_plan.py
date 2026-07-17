"""Deterministic canonical plan contracts and filesystem publication."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import List, Sequence

from agenten.planning.canonical_contracts import (
    PLAN_SCHEMA_VERSION,
    CanonicalPlan,
    CanonicalWorkPackage,
    WorkPackageStatus,
)
from agenten.planning.input_parser import ParsedProjectInput
from agenten.validation.contracts import HoldoutSuite, WorkBatch


class PlanPublishConflictError(RuntimeError):
    """A different canonical plan already occupies the publication path."""


class CanonicalPlanCompiler:
    """Convert validated batches into one deterministic aggregate plan."""

    def __init__(self, *, minimum_workers: int = 5) -> None:
        if minimum_workers < 1:
            raise ValueError("minimum_workers must be at least 1")
        self._minimum_workers = minimum_workers

    def compile(
        self,
        project_input: ParsedProjectInput,
        batches: Sequence[WorkBatch],
        holdouts: Sequence[HoldoutSuite] = (),
    ) -> CanonicalPlan:
        if not batches:
            raise ValueError("at least one work batch is required")
        canonical_batches = [self._canonicalize_batch(batch) for batch in batches]
        ordered_batches = self._canonical_order(canonical_batches)
        holdouts_by_batch = {holdout.batch_id: holdout for holdout in holdouts}
        if len(holdouts_by_batch) != len(holdouts):
            raise ValueError("duplicate holdout suites")
        unknown_holdouts = sorted(set(holdouts_by_batch) - {batch.batch_id for batch in batches})
        if unknown_holdouts:
            raise ValueError(f"holdouts reference unknown batches: {unknown_holdouts}")
        worker_pool = tuple(
            f"worker-{index:02d}" for index in range(1, self._minimum_workers + 1)
        )
        packages = [
            CanonicalWorkPackage(
                batch=batch,
                status=(WorkPackageStatus.REUSED if batch.satisfied_by else WorkPackageStatus.PLANNED),
                worker_id=worker_pool[index % len(worker_pool)],
                handoff=f"HANDOFF TO WORKER {(index % len(worker_pool)) + 1}",
                holdout_digest=self._holdout_digest(holdouts_by_batch.get(batch.batch_id)),
            )
            for index, batch in enumerate(ordered_batches)
        ]
        identity = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "input_sha256": project_input.sha256,
            "source_reference": project_input.source_reference,
            "worker_pool": worker_pool,
            "work_packages": [package.model_dump(mode="json") for package in packages],
        }
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return CanonicalPlan(
            plan_id=f"plan-{digest[:24]}",
            input_sha256=project_input.sha256,
            source_reference=project_input.source_reference,
            worker_pool=worker_pool,
            work_packages=tuple(packages),
        )

    @staticmethod
    def _canonicalize_batch(batch: WorkBatch) -> WorkBatch:
        payload = batch.model_dump(mode="json")
        payload["depends_on"] = sorted(payload["depends_on"])
        return WorkBatch.model_validate(payload)

    @staticmethod
    def _holdout_digest(holdout: HoldoutSuite | None) -> str | None:
        if holdout is None:
            return None
        payload = json.dumps(
            holdout.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _canonical_order(batches: Sequence[WorkBatch]) -> List[WorkBatch]:
        by_id = {batch.batch_id: batch for batch in batches}
        if len(by_id) != len(batches):
            raise ValueError("duplicate batch ids")
        known = set(by_id)
        unknown = sorted(
            dependency
            for batch in batches
            for dependency in batch.depends_on
            if dependency not in known
        )
        if unknown:
            raise ValueError(f"unknown dependencies: {sorted(set(unknown))}")

        remaining = {
            batch_id: set(batch.depends_on) for batch_id, batch in by_id.items()
        }
        ordered: List[WorkBatch] = []
        while remaining:
            ready = sorted(batch_id for batch_id, dependencies in remaining.items() if not dependencies)
            if not ready:
                raise ValueError("dependency cycle")
            for batch_id in ready:
                ordered.append(by_id[batch_id])
                del remaining[batch_id]
                for dependencies in remaining.values():
                    dependencies.discard(batch_id)
        return ordered


class CanonicalPlanPublisher:
    """Atomically publish a run-scoped source archive and canonical plan tree."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def publish(
        self,
        project_input: ParsedProjectInput,
        plan: CanonicalPlan,
        holdouts: Sequence[HoldoutSuite] = (),
    ) -> Path:
        if (
            project_input.sha256 != plan.input_sha256
            or project_input.source_reference != plan.source_reference
        ):
            raise ValueError("canonical plan does not belong to the supplied input")
        if self._root.exists():
            if self._matches_existing(project_input, plan, holdouts):
                return self._root / "plans"
            raise PlanPublishConflictError("a different canonical plan is already published")

        self._root.parent.mkdir(parents=True, exist_ok=True)
        stage = self._root.parent / f".{self._root.name}-{uuid.uuid4().hex}.tmp"
        try:
            self._write_tree(stage, project_input, plan, holdouts)
            try:
                os.replace(stage, self._root)
            except OSError as exc:
                if self._root.exists() and self._matches_existing(project_input, plan, holdouts):
                    return self._root / "plans"
                if self._root.exists():
                    raise PlanPublishConflictError(
                        "a different canonical plan won concurrent publication"
                    ) from exc
                raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return self._root / "plans"

    def _matches_existing(
        self,
        project_input: ParsedProjectInput,
        plan: CanonicalPlan,
        holdouts: Sequence[HoldoutSuite],
    ) -> bool:
        expected = self._root.parent / f".{self._root.name}-{uuid.uuid4().hex}.verify"
        try:
            self._write_tree(expected, project_input, plan, holdouts)
            existing_entries = list(self._root.rglob("*"))
            if any(entry.is_symlink() for entry in existing_entries):
                return False
            expected_files = {
                path.relative_to(expected) for path in expected.rglob("*") if path.is_file()
            }
            existing_files = {
                path.relative_to(self._root) for path in existing_entries if path.is_file()
            }
            if existing_files != expected_files:
                return False
            return all(
                (self._root / relative).read_bytes() == (expected / relative).read_bytes()
                for relative in expected_files
            )
        finally:
            if expected.exists():
                shutil.rmtree(expected)

    def _write_tree(
        self,
        stage: Path,
        project_input: ParsedProjectInput,
        plan: CanonicalPlan,
        holdouts: Sequence[HoldoutSuite] = (),
    ) -> None:
        source = stage / "source" / "input.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(project_input.content.encode("utf-8"))
        plans_root = stage / "plans"
        batches_root = stage / "contracts" / "batches"
        holdouts_root = stage / "holdouts"
        batches_root.mkdir(parents=True, exist_ok=True)
        holdouts_root.mkdir(parents=True, exist_ok=True)
        for directory in (
            "requirements",
            "architecture",
            "specs",
            "implementation",
            "tests",
            "reports",
        ):
            (plans_root / directory).mkdir(parents=True, exist_ok=True)

        self._write_json(plans_root / "manifest.json", plan.model_dump(mode="json"))
        for package in plan.work_packages:
            self._write_json(
                batches_root / f"{package.batch_id}.json",
                package.batch.model_dump(mode="json"),
            )
        holdouts_by_batch = {holdout.batch_id: holdout for holdout in holdouts}
        for package in plan.work_packages:
            holdout = holdouts_by_batch.get(package.batch_id)
            if holdout is None:
                if package.holdout_digest is not None:
                    raise ValueError(f"holdout payload missing for {package.batch_id}")
                continue
            actual_digest = CanonicalPlanCompiler._holdout_digest(holdout)
            if package.holdout_digest != actual_digest:
                raise ValueError(f"holdout payload differs for {package.batch_id}")
            self._write_json(
                holdouts_root / f"{package.batch_id}.json",
                holdout.model_dump(mode="json"),
            )
        self._write_text(plans_root / "index.md", self._render_index(plan))
        self._write_text(
            plans_root / "requirements" / "product-goal.md",
            self._render_product_goal(project_input),
        )
        self._write_text(
            plans_root / "requirements" / "acceptance-criteria.md",
            self._render_acceptance(plan),
        )
        self._write_text(
            plans_root / "architecture" / "dependency-dag.md",
            self._render_dependency_dag(plan),
        )
        self._write_text(
            plans_root / "implementation" / "work-packages.md",
            self._render_work_packages(plan),
        )
        self._write_text(
            plans_root / "implementation" / "decisions.md",
            "# Decisions\n\n- The source input is immutable and identified by SHA-256.\n"
            "- Planning, execution, and review exchange versioned contracts only.\n",
        )
        self._write_text(
            plans_root / "specs" / "process-boundaries.md",
            "# Process boundaries\n\n"
            "Planning may publish contracts but may not execute or approve them.\n\n"
            "Execution requires a passed review decision for this exact plan ID.\n\n"
            "Review consumes immutable plan and execution evidence and may not build artifacts.\n",
        )
        self._write_text(
            plans_root / "tests" / "contract-tests.md",
            "# Contract tests\n\nEvery package must satisfy its versioned acceptance assertions.\n",
        )
        self._write_text(
            plans_root / "tests" / "holdout-cases.md",
            self._render_holdout_refs(plan),
        )
        self._write_text(
            plans_root / "reports" / "latest-build.md",
            "# Latest build\n\nStatus: not executed. Planning does not claim build evidence.\n",
        )
        self._write_text(
            plans_root / "reports" / "latest-evaluation.md",
            "# Latest evaluation\n\nStatus: not reviewed. Release remains blocked.\n",
        )

    @staticmethod
    def _render_index(plan: CanonicalPlan) -> str:
        lines = [
            "# Canonical Captain plan",
            "",
            f"- Plan ID: `{plan.plan_id}`",
            f"- Input SHA-256: `{plan.input_sha256}`",
            f"- Source: `{plan.source_reference}`",
            f"- Worker pool: {', '.join(plan.worker_pool)}",
            "",
            "| Work package | Target | Canonical status | Handoff | Dependencies |",
            "| --- | --- | --- | --- | --- |",
        ]
        for package in plan.work_packages:
            dependencies = ", ".join(package.depends_on) or "none"
            lines.append(
                f"| `{package.batch_id}` | `{package.batch.target}` | `{package.status.value}` | "
                f"{package.handoff} | {dependencies} |"
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_product_goal(project_input: ParsedProjectInput) -> str:
        preferred = next(
            (
                section
                for section in project_input.sections
                if "goal" in section.title.lower() or "objective" in section.title.lower()
            ),
            project_input.sections[0] if project_input.sections else None,
        )
        body = preferred.body if preferred is not None and preferred.body else project_input.content
        return (
            "# Product goal\n\n"
            f"Source: `{project_input.source_reference}`  \n"
            f"Input SHA-256: `{project_input.sha256}`\n\n"
            f"{body.strip()}\n"
        )

    @staticmethod
    def _render_acceptance(plan: CanonicalPlan) -> str:
        lines = ["# Acceptance criteria", ""]
        for package in plan.work_packages:
            lines.append(f"## {package.batch.title}")
            lines.append("")
            for assertion in package.batch.acceptance_criteria:
                lines.append(f"- `{assertion.assertion_id}`: `{assertion.kind.value}`")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _render_dependency_dag(plan: CanonicalPlan) -> str:
        lines = ["# Dependency DAG", "", "```mermaid", "graph TD"]
        for package in plan.work_packages:
            if not package.depends_on:
                lines.append(f'    {package.batch_id}["{package.batch_id}"]')
            for dependency in package.depends_on:
                lines.append(f'    {dependency}["{dependency}"] --> {package.batch_id}["{package.batch_id}"]')
        lines.extend(["```", ""])
        return "\n".join(lines)

    @staticmethod
    def _render_work_packages(plan: CanonicalPlan) -> str:
        lines = ["# Work packages", ""]
        for package in plan.work_packages:
            lines.extend(
                [
                    f"## {package.batch.title}",
                    "",
                    f"- Batch ID: `{package.batch_id}`",
                    f"- Status: `{package.status.value}`",
                    f"- Worker: `{package.worker_id}`",
                    f"- {package.handoff}",
                    f"- Goal: {package.batch.goal}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _render_holdout_refs(plan: CanonicalPlan) -> str:
        lines = [
            "# Holdout references",
            "",
            "Payloads are stored outside the builder-visible plan tree.",
            "",
        ]
        for package in plan.work_packages:
            digest = package.holdout_digest or "not-provided"
            lines.append(f"- `{package.batch_id}`: `{digest}`")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
