from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from agenten.agent_runtime.contracts import AgentRuntimeCommand, CapabilityGrant


ROOT = Path(__file__).parents[2]
CAPTAIN_FIXTURES = ROOT / "tests" / "fixtures" / "contracts"
HERMES_FIXTURES = ROOT / "hermes-agent" / "tests" / "fixtures"
FIXTURE_NAMES = (
    "captain_work_package_released.v1.json",
    "hermes_work_result_submitted.v1.json",
)
FORBIDDEN_KEY = re.compile(
    r"(?i)(?:^|_)(?:api[_-]?key|authorization|credentials?|password|"
    r"private[_-]?key|secrets?|token|holdouts?)(?:$|_)"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")

WORK_PACKAGE_FIELDS = {
    "schema",
    "event_id",
    "correlation_id",
    "occurred_at",
    "producer",
    "subject_id",
    "subject_version",
    "command",
    "grant",
    "acceptance_assertion_ids",
}
WORK_RESULT_FIELDS = {
    "schema",
    "event_id",
    "command_id",
    "correlation_id",
    "occurred_at",
    "producer",
    "subject_id",
    "subject_version",
    "batch_id",
    "batch_version",
    "grant_id",
    "integration_intent",
    "status",
    "session_id",
    "before_commit",
    "after_commit",
    "changed_paths",
    "artifact_digest",
    "codex_evidence",
    "mcp_evidence",
    "error",
}
CODEX_EVIDENCE_FIELDS = {
    "session_id",
    "turn_id",
    "started_at",
    "ended_at",
    "before_commit",
    "after_commit",
    "changed_paths",
    "output_digest",
}
MCP_EVIDENCE_FIELDS = {
    "server_name",
    "tool_name",
    "call_id",
    "execution_id",
    "correlation_id",
    "started_at",
    "ended_at",
    "input_digest",
    "output_digest",
}


def _load(root: Path, name: str) -> dict[str, Any]:
    return json.loads((root / name).read_text(encoding="utf-8"))


def _reject_forbidden_fields(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if FORBIDDEN_KEY.search(str(key)):
                raise ValueError(f"forbidden secret or holdout field at {path}.{key}")
            _reject_forbidden_fields(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_forbidden_fields(nested, f"{path}[{index}]")


def _require_exact_fields(payload: dict[str, Any], required: set[str], path: str) -> None:
    missing = required - payload.keys()
    unknown = payload.keys() - required
    if missing or unknown:
        raise ValueError(
            f"{path} field mismatch: missing={sorted(missing)}, unknown={sorted(unknown)}"
        )


def _validate_fixture(name: str, payload: dict[str, Any]) -> None:
    _reject_forbidden_fields(payload)
    if name == FIXTURE_NAMES[0]:
        _require_exact_fields(payload, WORK_PACKAGE_FIELDS, "work package")
        command = AgentRuntimeCommand.model_validate(payload["command"])
        grant = CapabilityGrant.model_validate(payload["grant"])
        assert payload["schema"] == "captain.work-package-released.v1"
        assert str(command.event_id) == str(grant.command_id)
        assert str(command.correlation_id) == payload["correlation_id"]
        assert command.subject_id == grant.subtask_id == payload["subject_id"]
        assert command.subject_version == payload["subject_version"]
        assert command.payload.batch_id == grant.batch_id
        assert command.payload.workspace_ref == grant.workspace_ref
        assert payload["acceptance_assertion_ids"]
        return

    _require_exact_fields(payload, WORK_RESULT_FIELDS, "work result")
    _require_exact_fields(payload["codex_evidence"], CODEX_EVIDENCE_FIELDS, "codex evidence")
    for evidence in payload["mcp_evidence"]:
        _require_exact_fields(evidence, MCP_EVIDENCE_FIELDS, "mcp evidence")
        assert evidence["correlation_id"] == payload["correlation_id"]
        assert evidence["call_id"]
        assert evidence["execution_id"]
        assert SHA256.fullmatch(evidence["input_digest"])
        assert SHA256.fullmatch(evidence["output_digest"])
    assert payload["schema"] == "captain.hermes-work-result.v1"
    assert payload["session_id"] == payload["codex_evidence"]["session_id"]
    assert payload["before_commit"] == payload["codex_evidence"]["before_commit"]
    assert payload["after_commit"] == payload["codex_evidence"]["after_commit"]
    assert payload["changed_paths"] == payload["codex_evidence"]["changed_paths"]
    assert GIT_SHA.fullmatch(payload["before_commit"])
    assert GIT_SHA.fullmatch(payload["after_commit"])
    assert SHA256.fullmatch(payload["artifact_digest"])
    assert SHA256.fullmatch(payload["codex_evidence"]["output_digest"])


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_reviewed_contract_fixture_copy_matches_pinned_hermes(name: str) -> None:
    assert (CAPTAIN_FIXTURES / name).read_bytes() == (HERMES_FIXTURES / name).read_bytes()


@pytest.mark.parametrize("root", (CAPTAIN_FIXTURES, HERMES_FIXTURES), ids=("captain", "hermes"))
@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_captain_and_hermes_fixture_shapes_are_compatible(root: Path, name: str) -> None:
    _validate_fixture(name, _load(root, name))


@pytest.mark.parametrize("root", (CAPTAIN_FIXTURES, HERMES_FIXTURES), ids=("captain", "hermes"))
@pytest.mark.parametrize("forbidden", ("api_key", "credentials", "holdout_cases"))
def test_secret_and_holdout_fields_fail_both_contract_sides(
    root: Path, forbidden: str
) -> None:
    payload = copy.deepcopy(_load(root, FIXTURE_NAMES[0]))
    payload["command"]["payload"][forbidden] = {"value": "must-not-cross-boundary"}

    with pytest.raises(ValueError, match="forbidden secret or holdout"):
        _validate_fixture(FIXTURE_NAMES[0], payload)


@pytest.mark.parametrize("root", (CAPTAIN_FIXTURES, HERMES_FIXTURES), ids=("captain", "hermes"))
def test_unknown_fields_fail_both_contract_sides(root: Path) -> None:
    payload = copy.deepcopy(_load(root, FIXTURE_NAMES[1]))
    payload["unexpected_contract_field"] = True

    with pytest.raises(ValueError, match="unknown=.*unexpected_contract_field"):
        _validate_fixture(FIXTURE_NAMES[1], payload)
