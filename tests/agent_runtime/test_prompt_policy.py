from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agenten.agent_runtime.capabilities import derive_grant
from agenten.agent_runtime.contracts import AgentRuntimeCommand
from agenten.agent_runtime.prompt_policy import PromptPolicyDenied, render_codex_prompt
from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    ExampleCase,
    WorkBatch,
)


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "contracts"
    / "agent_runtime_command.v1.json"
)


def command_for(*, n8n: bool) -> AgentRuntimeCommand:
    value: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["payload"]["capability_profile"] = (
        "n8n-builder" if n8n else "code-builder"
    )
    value["payload"]["integration_intent"] = "n8n" if n8n else "none"
    return AgentRuntimeCommand.model_validate(value)


def batch_for(*, n8n: bool, goal: str = "Implement the released change.") -> WorkBatch:
    return WorkBatch(
        batch_id="batch-1",
        title="Implement one bounded work package",
        goal=goal,
        subtask_ids=["subtask-1"],
        target="python",
        capability_tags=["n8n-builder" if n8n else "code-builder"],
        constraints=["Do not modify files outside the authorized workspace."],
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="focused-tests-pass",
                kind=AssertionKind.STATUS_EQUALS,
                path="status",
                expected="passed",
                description="The focused tests pass.",
            )
        ],
        golden_cases=[
            ExampleCase(
                case_id="visible-example",
                input={"request": "bounded"},
                expected_observations={"status": "passed"},
            )
        ],
    )


def render(*, n8n: bool, batch: WorkBatch | None = None):
    command = command_for(n8n=n8n)
    released = batch or batch_for(n8n=n8n)
    grant = derive_grant(command, released, NOW)
    return render_codex_prompt(command, released, grant)


def test_plain_builder_has_no_n8n_instructions() -> None:
    rendered = render(n8n=False)

    assert "n8n" not in rendered.text.lower()
    assert rendered.overlay == "base"


def test_n8n_builder_requires_discover_validate_test_evidence_order() -> None:
    rendered = render(n8n=True)
    lowered = rendered.text.lower()
    positions = [
        lowered.index(term)
        for term in (
            "discover mcp tools",
            "prefer native n8n nodes",
            "validate",
            "test",
            "evidence",
        )
    ]

    assert positions == sorted(positions)
    assert "do not start, stop, recreate, or adopt docker containers" in lowered
    assert "do not create, delete, or migrate docker volumes" in lowered
    assert rendered.overlay == "n8n"


def test_rendered_prompt_is_content_addressed_and_has_no_redactions() -> None:
    rendered = render(n8n=False)

    assert rendered.sha256 == hashlib.sha256(rendered.text.encode("utf-8")).hexdigest()
    assert rendered.media_type == "text/markdown"
    assert rendered.redactions == ()
    assert "captain.agent-runtime-result.v1" in rendered.text
    assert "workspace://authorized/project-1/subtask-1" in rendered.text


@pytest.mark.parametrize(
    "goal",
    [
        "Use api_key=should-never-enter-a-prompt",
        "Send Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
        "Read C:\\Users\\Other\\private.txt before implementing.",
        "Read /home/operator/private.txt before implementing.",
    ],
)
def test_secret_values_and_absolute_paths_are_rejected(goal: str) -> None:
    batch = batch_for(n8n=False, goal=goal)

    with pytest.raises(PromptPolicyDenied):
        render(n8n=False, batch=batch)


def test_holdout_like_keys_cannot_enter_visible_golden_cases() -> None:
    batch = batch_for(n8n=False).model_copy(
        update={
            "golden_cases": [
                ExampleCase(
                    case_id="visible-example",
                    input={"private_holdout": {"expected": "hidden"}},
                )
            ]
        }
    )

    with pytest.raises(PromptPolicyDenied, match="holdout"):
        render(n8n=False, batch=batch)


def test_wrong_grant_profile_cannot_select_an_overlay() -> None:
    plain_command = command_for(n8n=False)
    plain_batch = batch_for(n8n=False)
    plain_grant = derive_grant(plain_command, plain_batch, NOW)

    with pytest.raises(PromptPolicyDenied, match="grant"):
        render_codex_prompt(command_for(n8n=True), batch_for(n8n=True), plain_grant)
