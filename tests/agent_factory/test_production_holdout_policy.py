from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import TextMessage
from pydantic import BaseModel

from agenten.agent_factory.capability_live_adapters import (
    ContentAddressedArtifactStore,
)
from agenten.agent_factory.input_compiler import FactoryInputCompiler
from agenten.agent_factory.input_document import parse_factory_input_bytes
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.production_holdout_policy import (
    CanonicalInputHoldoutPolicy,
    _observation_from_content,
)
from agenten.agent_factory.team_execution import build_private_holdout_task_envelope
from agenten.agent_runtime.contracts import ArtifactRef


NOW = datetime(2026, 7, 21, 18, 0, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parents[1] / "fixtures" / "agent_factory" / "TO_BE_BUILT.valid.md"


def _candidate_ref() -> ArtifactRef:
    digest = hashlib.sha256(b"sealed candidate").hexdigest()
    return ArtifactRef(
        uri=f"artifact://factory-candidate/{digest}",
        sha256=digest,
        media_type="application/zip",
    )


def _compiled() -> tuple[bytes, object, InMemoryPrivateHoldoutStore]:
    source = FIXTURE.read_bytes()
    document = parse_factory_input_bytes(source, "TO_BE_BUILT.md")
    holdouts = InMemoryPrivateHoldoutStore()
    compiled = FactoryInputCompiler(holdout_store=holdouts).compile(document, 3)
    return source, compiled, holdouts


def _result(payload: object, *, source: str = "quality_warden") -> TaskResult:
    return TaskResult(
        messages=[TextMessage(source=source, content=json.dumps(payload))],
        stop_reason="TERMINATE",
    )


def test_structured_terminal_observation_normalizes_assertion_list() -> None:
    class Assertion(BaseModel):
        assertion_id: str
        passed: bool
        observable: str

    class Recovery(BaseModel):
        stop_condition_sha256: str

    class Observation(BaseModel):
        schema: str
        assertions: list[Assertion]
        recovery: Recovery
        termination: str

    parsed = _observation_from_content(
        Observation(
            schema="captain.factory-observation.v1",
            assertions=[
                Assertion(
                    assertion_id="assert-123456789abc",
                    passed=True,
                    observable="exact observed output",
                )
            ],
            recovery=Recovery(stop_condition_sha256="a" * 64),
            termination="TERMINATE",
        )
    )

    assert parsed == {
        "schema": "captain.factory-observation.v1",
        "assertions": {
            "assert-123456789abc": {
                "passed": True,
                "observable": "exact observed output",
            }
        },
        "recovery": {"stop_condition_sha256": "a" * 64},
        "termination": "TERMINATE",
    }


@pytest.mark.asyncio
async def test_policy_reconstructs_holdout_and_accepts_only_observed_expected_values(
    tmp_path: Path,
) -> None:
    source, compiled, holdout_bodies = _compiled()
    artifacts = ContentAddressedArtifactStore(tmp_path / "cas")
    policy = CanonicalInputHoldoutPolicy(
        canonical_input=source,
        subject_version=3,
        candidate_ref=_candidate_ref(),
        artifacts=artifacts,
        clock=lambda: NOW,
    )
    reference = compiled.private_holdout_refs[0]
    holdout = json.loads(holdout_bodies.get(reference.uri))
    observations = {
        assertion.assertion_id: {
            "passed": True,
            "observable": assertion.observable_expected,
        }
        for assertion in compiled.assertions
    }
    receipt = await policy.evaluate(
        reference,
        _result(
            {
                "schema": "captain.factory-observation.v1",
                "assertions": observations,
                "recovery": {
                    "stop_condition_sha256": hashlib.sha256(
                        holdout["observable_expected"].encode("utf-8")
                    ).hexdigest()
                },
            }
        ),
        compiled.assertion_ids,
    )

    assert all(item.passed for item in receipt.decisions)
    assert receipt.candidate_ref == _candidate_ref()
    assert receipt.evaluator_id == "captain.observable_expected"
    assert receipt.evaluator_version.startswith("1+")
    assert await policy.read(reference) == holdout_bodies.get(reference.uri).encode()
    assert artifacts.binding(
        "holdout-policy", f"{compiled.source_ref.sha256}/v3/{compiled.compilation_digest}"
    ) is not None


@pytest.mark.asyncio
async def test_task_envelope_drives_a_real_task_result_with_private_assertion_contract(
    tmp_path: Path,
) -> None:
    source, compiled, holdout_bodies = _compiled()
    policy = CanonicalInputHoldoutPolicy(
        canonical_input=source,
        subject_version=3,
        candidate_ref=_candidate_ref(),
        artifacts=ContentAddressedArtifactStore(tmp_path / "cas"),
        clock=lambda: NOW,
    )
    reference = compiled.private_holdout_refs[0]
    body = holdout_bodies.get(reference.uri).encode()
    envelope = json.loads(
        build_private_holdout_task_envelope(body, compiled.assertion_ids)
    )
    required = envelope["required_final_output"]

    assert required["assertion_ids"] == list(compiled.assertion_ids)
    assert "observable_expected" not in required["assertions"]["item_shape"]
    assert required["assertions"]["format"] == "array"
    assert envelope["private_case"]["assertion_expectations"] == {
        assertion.assertion_id: assertion.observable_expected
        for assertion in compiled.assertions
    }
    assert "termination" not in required
    assert "copy observable byte-for-byte" in envelope["instructions"]
    assert "Captain-authorized observed evidence" in envelope["instructions"]
    observation = {
        "assertions": {
            assertion.assertion_id: {
                "passed": True,
                "observable": assertion.observable_expected,
            }
            for assertion in compiled.assertions
        },
        "recovery": required["recovery"],
        "termination": "TERMINATE",
    }
    task_result = TaskResult(
        messages=[
            TextMessage(source="user", content=json.dumps(envelope)),
            TextMessage(source="quality_warden", content=json.dumps(observation)),
        ],
        stop_reason="TERMINATE",
    )

    receipt = await policy.evaluate(
        reference,
        task_result,
        compiled.assertion_ids,
    )

    assert all(item.passed for item in receipt.decisions)


@pytest.mark.asyncio
async def test_policy_is_not_all_pass_and_ignores_user_echo(tmp_path: Path) -> None:
    source, compiled, holdout_bodies = _compiled()
    policy = CanonicalInputHoldoutPolicy(
        canonical_input=source,
        subject_version=3,
        candidate_ref=_candidate_ref(),
        artifacts=ContentAddressedArtifactStore(tmp_path / "cas"),
        clock=lambda: NOW,
    )
    reference = compiled.private_holdout_refs[0]
    holdout = json.loads(holdout_bodies.get(reference.uri))
    valid = {
        "schema": "captain.factory-observation.v1",
        "assertions": {
            assertion.assertion_id: {
                "passed": True,
                "observable": assertion.observable_expected,
            }
            for assertion in compiled.assertions
        },
        "recovery": {
            "stop_condition_sha256": hashlib.sha256(
                holdout["observable_expected"].encode()
            ).hexdigest()
        },
    }
    result = TaskResult(
        messages=[
            TextMessage(source="user", content=json.dumps(valid)),
            TextMessage(
                source="quality_warden",
                content=json.dumps(
                    {
                        **valid,
                        "assertions": {
                            **valid["assertions"],
                            compiled.assertion_ids[0]: {
                                "passed": True,
                                "observable": "unrelated output",
                            },
                        },
                    }
                ),
            ),
        ],
        stop_reason="TERMINATE",
    )

    receipt = await policy.evaluate(
        reference, result, compiled.assertion_ids
    )

    assert receipt.decisions[0].passed is False
    assert any(item.passed for item in receipt.decisions[1:])


@pytest.mark.asyncio
async def test_policy_fails_all_assertions_when_stop_condition_hash_is_absent(
    tmp_path: Path,
) -> None:
    source, compiled, _ = _compiled()
    policy = CanonicalInputHoldoutPolicy(
        canonical_input=source,
        subject_version=3,
        candidate_ref=_candidate_ref(),
        artifacts=ContentAddressedArtifactStore(tmp_path / "cas"),
        clock=lambda: NOW,
    )
    payload = {
        "schema": "captain.factory-observation.v1",
        "assertions": {
            assertion.assertion_id: {
                "passed": True,
                "observable": assertion.observable_expected,
            }
            for assertion in compiled.assertions
        },
        "recovery": {"stop_condition_sha256": "0" * 64},
    }

    receipt = await policy.evaluate(
        compiled.private_holdout_refs[0],
        _result(payload),
        compiled.assertion_ids,
    )

    assert not any(item.passed for item in receipt.decisions)


@pytest.mark.asyncio
async def test_policy_rejects_unknown_holdout_or_assertion_scope(tmp_path: Path) -> None:
    source, compiled, _ = _compiled()
    policy = CanonicalInputHoldoutPolicy(
        canonical_input=source,
        subject_version=3,
        candidate_ref=_candidate_ref(),
        artifacts=ContentAddressedArtifactStore(tmp_path / "cas"),
        clock=lambda: NOW,
    )
    reference = compiled.private_holdout_refs[0]

    with pytest.raises(ValueError, match="assertion scope"):
        await policy.evaluate(reference, _result({}), (compiled.assertion_ids[0],))
    with pytest.raises(ValueError, match="unknown private holdout"):
        await policy.read(reference.model_copy(update={"sha256": "f" * 64}))
