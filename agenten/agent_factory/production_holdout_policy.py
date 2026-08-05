"""Deterministic Captain policy for private Factory holdout evaluation.

The policy is reconstructed from the canonical ``TO_BE_BUILT.md`` bytes.  It
never asks the provider to judge itself: only structured observations emitted
by non-user AutoGen participants can satisfy an assertion.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from autogen_agentchat.base import TaskResult
from pydantic import BaseModel

from agenten.agent_factory.capability_live_adapters import (
    ContentAddressedArtifactStore,
)
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.input_compiler import (
    AcceptanceAssertion,
    FactoryInputCompiler,
)
from agenten.agent_factory.input_document import parse_factory_input_bytes
from agenten.agent_factory.team_execution import (
    FactoryHoldoutAssertionDecisionV1,
    FactoryHoldoutEvaluationReceiptV1,
)
from agenten.agent_runtime.contracts import ArtifactRef


_POLICY_ID = "captain.observable_expected"
_POLICY_VERSION = 1
_IGNORED_SOURCES = frozenset({"user", "factory_host"})


class CanonicalInputHoldoutPolicy:
    """Serve and evaluate one immutable canonical-input holdout policy."""

    def __init__(
        self,
        *,
        canonical_input: bytes,
        subject_version: int,
        candidate_ref: ArtifactRef,
        artifacts: ContentAddressedArtifactStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(canonical_input, bytes):
            raise TypeError("canonical_input must be bytes")
        if isinstance(subject_version, bool) or subject_version < 1:
            raise ValueError("subject_version must be a positive integer")
        self._candidate_ref = candidate_ref
        self._artifacts = artifacts
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        document = parse_factory_input_bytes(canonical_input, "TO_BE_BUILT.md")
        source_ref = artifacts.put(
            canonical_input,
            "text/markdown",
            namespace="factory-input",
        )
        if source_ref.sha256 != document.input_ref.sha256:
            raise ValueError("canonical input CAS digest changed")

        holdout_store = InMemoryPrivateHoldoutStore()
        compiled = FactoryInputCompiler(holdout_store=holdout_store).compile(
            document,
            subject_version,
        )
        self._assertion_ids = compiled.assertion_ids
        self._assertions = {
            assertion.assertion_id: assertion for assertion in compiled.assertions
        }
        self._holdouts = {
            reference: holdout_store.get(reference.uri).encode("utf-8")
            for reference in compiled.private_holdout_refs
        }
        policy_payload = {
            "schema": "captain.private-holdout-policy.v1",
            "policy_id": _POLICY_ID,
            "policy_version": _POLICY_VERSION,
            "source_sha256": document.input_ref.sha256,
            "subject_version": subject_version,
            "compiled_spec_sha256": compiled.compilation_digest,
            "assertions": [
                assertion.model_dump(mode="json")
                for assertion in compiled.assertions
            ],
            "holdout_refs": [
                reference.model_dump(mode="json")
                for reference in compiled.private_holdout_refs
            ],
        }
        policy_bytes = _canonical_json(policy_payload)
        self._policy_ref = artifacts.put(
            policy_bytes,
            "application/json",
            namespace="holdout-policy",
        )
        artifacts.bind(
            "holdout-policy",
            # The same source/version can be recompiled after Captain changes
            # its private-contract compiler.  Bind the immutable policy to the
            # compilation digest as well; an older policy must remain readable
            # rather than making a later live run overwrite it.
            f"{document.input_ref.sha256}/v{subject_version}/{compiled.compilation_digest}",
            self._policy_ref,
        )

    @property
    def policy_ref(self) -> ArtifactRef:
        return self._policy_ref

    @property
    def assertion_ids(self) -> tuple[str, ...]:
        return self._assertion_ids

    @property
    def holdout_refs(self) -> tuple[PrivateHoldoutRef, ...]:
        return tuple(self._holdouts)

    async def read(self, reference: PrivateHoldoutRef) -> bytes:
        try:
            body = self._holdouts[reference]
        except KeyError as exc:
            raise ValueError("unknown private holdout reference") from exc
        if hashlib.sha256(body).hexdigest() != reference.sha256:
            raise ValueError("private holdout body digest changed")
        return body

    async def evaluate(
        self,
        reference: PrivateHoldoutRef,
        result: Any,
        assertion_ids: tuple[str, ...],
    ) -> FactoryHoldoutEvaluationReceiptV1:
        if reference not in self._holdouts:
            raise ValueError("unknown private holdout reference")
        if assertion_ids != self._assertion_ids:
            raise ValueError("holdout assertion scope does not match canonical input")
        if not isinstance(result, TaskResult):
            raise ValueError("holdout evaluator requires an AutoGen TaskResult")

        observation = _last_agent_observation(result)
        assertions = observation.get("assertions") if observation else None
        recovery = observation.get("recovery") if observation else None
        holdout = json.loads(self._holdouts[reference])
        expected_stop_hash = hashlib.sha256(
            str(holdout["observable_expected"]).encode("utf-8")
        ).hexdigest()
        recovery_matches = (
            isinstance(recovery, Mapping)
            and recovery.get("stop_condition_sha256") == expected_stop_hash
        )
        decisions = tuple(
            self._decision(
                assertion=self._assertions[assertion_id],
                raw=(
                    assertions.get(assertion_id)
                    if isinstance(assertions, Mapping)
                    else None
                ),
                recovery_matches=recovery_matches,
            )
            for assertion_id in assertion_ids
        )
        evaluated_at = self._clock()
        if (
            evaluated_at.tzinfo is None
            or evaluated_at.utcoffset() != timezone.utc.utcoffset(evaluated_at)
        ):
            raise ValueError("holdout policy clock must be UTC")
        return FactoryHoldoutEvaluationReceiptV1(
            schema_name="captain.factory-holdout-evaluation-receipt.v1",
            holdout_ref=reference,
            candidate_ref=self._candidate_ref,
            assertion_ids=assertion_ids,
            decisions=decisions,
            evaluator_id=_POLICY_ID,
            evaluator_version=f"{_POLICY_VERSION}+{self._policy_ref.sha256[:12]}",
            evaluated_at=evaluated_at,
        )

    @staticmethod
    def _decision(
        *,
        assertion: AcceptanceAssertion,
        raw: object,
        recovery_matches: bool,
    ) -> FactoryHoldoutAssertionDecisionV1:
        passed = False
        if isinstance(raw, Mapping) and raw.get("passed") is True:
            observable = raw.get("observable")
            if isinstance(observable, str):
                expected = _normalize_observable(assertion.observable_expected)
                actual = _normalize_observable(observable)
                passed = bool(expected and expected in actual and recovery_matches)
        provenance = (
            "observable_expected_and_stop_hash"
            if passed
            else "observable_expected_or_stop_hash_mismatch"
        )
        return FactoryHoldoutAssertionDecisionV1(
            assertion_id=assertion.assertion_id,
            passed=passed,
            provenance_code=provenance,
        )


def _last_agent_observation(result: TaskResult) -> dict[str, object] | None:
    for message in reversed(result.messages):
        source = getattr(message, "source", "")
        if not isinstance(source, str) or source.casefold() in _IGNORED_SOURCES:
            continue
        content = getattr(message, "content", None)
        parsed = _observation_from_content(content)
        if parsed is not None:
            return parsed
    return None


def _observation_from_content(content: object) -> dict[str, object] | None:
    if isinstance(content, BaseModel):
        candidate = content.model_dump()
        if candidate.get("schema_name") == "captain.factory-observation.v1":
            candidate["schema"] = candidate["schema_name"]
    elif isinstance(content, Mapping):
        candidate = dict(content)
    elif isinstance(content, str):
        stripped = content.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
        if fenced is not None:
            stripped = fenced.group(1)
        try:
            raw = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(raw, dict):
            return None
        candidate = raw
    else:
        return None
    if candidate.get("schema") != "captain.factory-observation.v1":
        # The terminal model may omit the redundant schema discriminator even
        # when it emitted the full required observation body. Normalize only
        # this exact, otherwise complete compatibility shape before Captain
        # evaluates every assertion against its own private oracle.
        if (
            "schema" not in candidate
            and candidate.get("termination") == "TERMINATE"
            and isinstance(candidate.get("assertions"), Mapping)
            and isinstance(candidate.get("recovery"), Mapping)
        ):
            candidate["schema"] = "captain.factory-observation.v1"
        else:
            return None
    assertions = candidate.get("assertions")
    if isinstance(assertions, list):
        normalized: dict[str, dict[str, object]] = {}
        for item in assertions:
            if not isinstance(item, Mapping):
                return None
            assertion_id = item.get("assertion_id")
            passed = item.get("passed")
            observable = item.get("observable")
            if (
                not isinstance(assertion_id, str)
                or not assertion_id
                or assertion_id in normalized
                or not isinstance(passed, bool)
                or not isinstance(observable, str)
            ):
                return None
            normalized[assertion_id] = {
                "passed": passed,
                "observable": observable,
            }
        candidate["assertions"] = normalized
    if candidate.get("schema") != "captain.factory-observation.v1":
        return None
    return candidate


def _normalize_observable(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE))


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
