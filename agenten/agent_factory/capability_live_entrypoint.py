"""Thin, redacted entrypoint for the three-input capability live runner.

The module validates repository ownership and output evidence.  Concrete
Factory, runtime, Gateway, and Minibook effects remain in the injected
``CapabilityLiveRunner`` graph; this boundary never fabricates their receipts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID, uuid5

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveDemoEvidence,
    CapabilityLiveInput,
    validate_live_demo_evidence,
)
from agenten.agent_runtime.confined_files import ConfinedFileStore
from agenten.agent_runtime.captain_production_adapters import (
    ContentAddressedArtifactAdapter,
)


_RUN_NAMESPACE = UUID("ba04703e-9ff1-55ff-95a3-da23f5ba7c06")
_CAPABILITY_INPUTS_BY_PATTERN = {
    "sequential": "sequential-n8n",
    "selector_group_chat": "selector-review",
    "swarm": "swarm-api-gap",
}


class CapabilityLiveEntrypointError(RuntimeError):
    """The standalone command lacks a concrete current-authority binding."""


class CapabilityLiveRunnerPort(Protocol):
    async def run(
        self,
        *,
        run_id: UUID,
        inputs: tuple[CapabilityLiveInput, ...],
        maximum_cost_usd: Decimal,
    ) -> CapabilityLiveDemoEvidence: ...


RunnerFactory = Callable[[Path], CapabilityLiveRunnerPort]
ConversationPattern = Literal["sequential", "selector_group_chat", "swarm"]


def load_capability_live_inputs(
    *,
    repository_root: Path,
    input_paths: Sequence[Path],
    patterns: Sequence[str],
) -> tuple[CapabilityLiveInput, ...]:
    """Bind exact repository files to redacted, content-addressed input refs."""

    root = repository_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("capability live repository root is unavailable")
    if len(input_paths) != 3 or len(patterns) != 3:
        raise ValueError("capability live entrypoint requires exactly three inputs")

    resolved: list[Path] = []
    for path in input_paths:
        candidate = path.resolve(strict=True)
        if not candidate.is_file() or candidate.name != "TO_BE_BUILT.md":
            raise ValueError("canonical capability live input is unavailable")
        try:
            candidate.relative_to(root)
        except ValueError:
            raise ValueError("capability live inputs must be repository-owned") from None
        resolved.append(candidate)

    expected_patterns = tuple(_CAPABILITY_INPUTS_BY_PATTERN)
    if tuple(patterns) != expected_patterns:
        raise ValueError("capability live conversation patterns are not canonical")

    source_store = ContentAddressedArtifactAdapter(
        root / ".captain-cook" / "artifacts" / "sha256"
    )
    result: list[CapabilityLiveInput] = []
    for path, pattern in zip(
        resolved,
        patterns,
        strict=True,
    ):
        content = path.read_bytes()
        source_ref = source_store.put(content, "text/markdown")
        semantics = {
            "sequential": {
                "integration_intent": "n8n",
                "conflicting_input": False,
                "human_escalation_required": False,
                "required_tool_marker": None,
            },
            "selector_group_chat": {
                "integration_intent": "none",
                "conflicting_input": True,
                "human_escalation_required": True,
                "required_tool_marker": None,
            },
            "swarm": {
                "integration_intent": "none",
                "conflicting_input": False,
                "human_escalation_required": False,
                "required_tool_marker": "TODO_TOOL.v1",
            },
        }[pattern]
        result.append(
            CapabilityLiveInput(
                input_id=_CAPABILITY_INPUTS_BY_PATTERN[pattern],
                conversation_pattern=cast(ConversationPattern, pattern),
                source_ref=source_ref,
                **semantics,
            )
        )
    return tuple(result)


async def run_capability_live_demo(
    *,
    runner: CapabilityLiveRunnerPort,
    repository_root: Path,
    input_paths: Sequence[Path],
    patterns: Sequence[str],
    maximum_cost_usd: Decimal,
    evidence_path: Path,
    run_id: UUID | None = None,
) -> CapabilityLiveDemoEvidence:
    """Run one validated suite and write only its redacted final evidence."""

    root = repository_root.resolve(strict=True)
    inputs = load_capability_live_inputs(
        repository_root=root,
        input_paths=input_paths,
        patterns=patterns,
    )
    selected_run_id = run_id or uuid5(
        _RUN_NAMESPACE,
        "|".join(item.source_ref.sha256 for item in inputs),
    )
    evidence = validate_live_demo_evidence(
        await runner.run(
            run_id=selected_run_id,
            inputs=inputs,
            maximum_cost_usd=maximum_cost_usd,
        )
    )
    if evidence.run_id != selected_run_id:
        raise CapabilityLiveEntrypointError("live runner changed the run identity")
    _persist_final_evidence(root, evidence_path, evidence)
    return verify_capability_live_evidence(evidence_path)


def verify_capability_live_evidence(path: Path) -> CapabilityLiveDemoEvidence:
    """Reparse and re-run every final authority gate from persisted bytes."""

    try:
        evidence = CapabilityLiveDemoEvidence.model_validate_json(path.read_bytes())
    except (OSError, ValueError):
        raise CapabilityLiveEntrypointError(
            "capability live final evidence is missing or invalid"
        ) from None
    return validate_live_demo_evidence(evidence)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner_factory: RunnerFactory | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path)
    parser.add_argument("--input", action="append", type=Path, default=[])
    parser.add_argument("--pattern", action="append", default=[])
    parser.add_argument("--max-cost-usd")
    parser.add_argument("--evidence-path", required=True, type=Path)
    parser.add_argument("--run-id", type=UUID)
    parser.add_argument("--verify-only", action="store_true")
    arguments = parser.parse_args(argv)

    if arguments.verify_only:
        evidence = verify_capability_live_evidence(arguments.evidence_path)
    else:
        if (
            arguments.repository_root is None
            or len(arguments.input) != 3
            or len(arguments.pattern) != 3
            or arguments.max_cost_usd is None
        ):
            raise CapabilityLiveEntrypointError(
                "live execution requires repository, three inputs, patterns, and budget"
            )
        maximum = _positive_decimal(arguments.max_cost_usd)
        selected_factory = runner_factory or _compose_current_gateway_runner
        runner = selected_factory(arguments.repository_root.resolve(strict=True))
        evidence = asyncio.run(
            run_capability_live_demo(
                runner=runner,
                repository_root=arguments.repository_root,
                input_paths=arguments.input,
                patterns=arguments.pattern,
                maximum_cost_usd=maximum,
                evidence_path=arguments.evidence_path,
                run_id=arguments.run_id,
            )
        )
    print(
        json.dumps(
            {
                "schema": "captain.capability-live-entrypoint-result.v1",
                "status": evidence.status,
                "run_id": str(evidence.run_id),
                "booked_cost_usd": str(evidence.booked_cost_usd),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _persist_final_evidence(
    repository_root: Path,
    evidence_path: Path,
    evidence: CapabilityLiveDemoEvidence,
) -> None:
    state_root = (repository_root / ".captain-cook" / "live-demo").resolve()
    target = evidence_path.resolve()
    try:
        relative = target.relative_to(state_root)
    except ValueError:
        raise ValueError("capability live evidence must remain below .captain-cook") from None
    content = (
        json.dumps(
            evidence.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    ConfinedFileStore(state_root).write_once(
        relative,
        content,
        conflict="capability live final evidence already exists with different content",
    )


def _positive_decimal(value: object) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise CapabilityLiveEntrypointError("maximum cost must be a finite decimal") from None
    if not parsed.is_finite() or parsed <= 0:
        raise CapabilityLiveEntrypointError("maximum cost must be positive and finite")
    return parsed


def _compose_current_gateway_runner(repository_root: Path) -> CapabilityLiveRunnerPort:
    """Load the configured current-authority graph for the normal CLI path."""

    from gateway.capability_live_production_builder import (
        CapabilityLiveProductionConfigurationError,
        build_current_authority_capability_live_runner_from_environment,
    )

    try:
        return build_current_authority_capability_live_runner_from_environment(
            repository_root=repository_root,
            environment=os.environ,
        )
    except CapabilityLiveProductionConfigurationError as exc:
        raise CapabilityLiveEntrypointError(str(exc)) from exc


def _cli_main() -> int:
    try:
        return main()
    except CapabilityLiveEntrypointError:
        print(
            json.dumps(
                {
                    "schema": "captain.capability-live-entrypoint-error.v1",
                    "status": "configuration_blocked",
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli_main())


__all__ = [
    "CapabilityLiveEntrypointError",
    "CapabilityLiveRunnerPort",
    "load_capability_live_inputs",
    "main",
    "run_capability_live_demo",
    "verify_capability_live_evidence",
]
