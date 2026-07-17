"""Fail-closed prompt rendering for bounded Codex runtime sessions."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_runtime.capabilities import CapabilityDenied, validate_grant
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    CapabilityGrant,
    CapabilityProfile,
)
from agenten.validation.contracts import WorkBatch


class PromptPolicyDenied(RuntimeError):
    """Build input is unsafe or does not match the authoritative grant."""


class RenderedPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: Literal["text/markdown"] = "text/markdown"
    overlay: Literal["base", "n8n"]
    redactions: tuple[str, ...] = ()


_PROMPT_DIRECTORY = Path(__file__).with_name("prompts")
_SECRET_KEY = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|authorization|credentials?|password|private[_-]?key|secrets?|tokens?)(?:$|[_-])"
)
_HOLDOUT_KEY = re.compile(r"(?i)(?:holdout|hidden[_-]?case|private[_-]?(?:case|test))")
_SECRET_VALUE = re.compile(
    r"(?i)(?:api[_-]?key|authorization|bearer|password|secret|token)\s*(?::|=)\s*\S+"
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:^|\s)[a-z]:\\")
_USER_ABSOLUTE_PATH = re.compile(r"(?:^|\s)/(?:home|Users)/")


def render_codex_prompt(
    command: AgentRuntimeCommand,
    batch: WorkBatch,
    grant: CapabilityGrant,
) -> RenderedPrompt:
    """Render only validated, build-visible input into a content-addressed prompt."""

    try:
        validate_grant(grant, command, grant.issued_at)
    except CapabilityDenied as exc:
        raise PromptPolicyDenied(f"grant does not authorize prompt rendering: {exc}") from exc

    payload = command.payload
    if payload.batch_id != batch.batch_id:
        raise PromptPolicyDenied("command does not match the released batch")
    if payload.subtask_id is None or payload.subtask_id not in batch.subtask_ids:
        raise PromptPolicyDenied("command subtask is not part of the released batch")
    if grant.profile.value not in batch.capability_tags:
        raise PromptPolicyDenied("grant profile is not released by the batch")

    visible_contract = batch.model_dump(mode="json")
    _assert_prompt_safe(visible_contract)

    base = _load_template("codex_base.md").format(
        workspace_ref=grant.workspace_ref,
        batch_id=batch.batch_id,
        subtask_id=payload.subtask_id,
        title=batch.title,
        goal=batch.goal,
        target=batch.target,
        runtime=batch.runtime,
        runtime_version=batch.runtime_version,
        interface_schema=batch.interface_schema,
        constraints_json=_canonical_json(batch.constraints),
        capabilities_json=_canonical_json(grant.capabilities),
        acceptance_json=_canonical_json(
            [item.model_dump(mode="json") for item in batch.acceptance_criteria]
        ),
        golden_cases_json=_canonical_json(
            [item.model_dump(mode="json") for item in batch.golden_cases]
        ),
        wall_seconds=payload.limits.wall_seconds,
        max_iterations=payload.limits.max_iterations,
    ).strip()

    if grant.profile is CapabilityProfile.N8N_BUILDER:
        text = f"{_load_template('codex_n8n_overlay.md').strip()}\n\n{base}\n"
        overlay: Literal["base", "n8n"] = "n8n"
    else:
        text = f"{base}\n"
        overlay = "base"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return RenderedPrompt(
        text=text,
        sha256=digest,
        overlay=overlay,
        redactions=(),
    )


def _assert_prompt_safe(value: Any, *, path: str = "batch") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key)
            if _SECRET_KEY.search(key_text):
                raise PromptPolicyDenied(f"secret-bearing field is forbidden at {path}.{key_text}")
            if _HOLDOUT_KEY.search(key_text):
                raise PromptPolicyDenied(f"holdout field is forbidden at {path}.{key_text}")
            _assert_prompt_safe(nested, path=f"{path}.{key_text}")
        return
    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _assert_prompt_safe(nested, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        if _SECRET_VALUE.search(value):
            raise PromptPolicyDenied(f"secret-like value is forbidden at {path}")
        if _WINDOWS_ABSOLUTE_PATH.search(value) or _USER_ABSOLUTE_PATH.search(value):
            raise PromptPolicyDenied(f"absolute user path is forbidden at {path}")


def _load_template(name: str) -> str:
    return (_PROMPT_DIRECTORY / name).read_text(encoding="utf-8")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
