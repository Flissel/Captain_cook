"""Shared deterministic credential redaction for evaluation evidence."""

from __future__ import annotations

import re
from typing import TypeVar

from pydantic import BaseModel


CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?im)(?P<key>\b(?:[a-z][a-z0-9_]*_)?(?:api_key|token)|\bpassword)(?P<before>[ \t]*)=(?P<after>[ \t]*)(?P<value>[^\r\n]*)"
)
_Model = TypeVar("_Model", bound=BaseModel)


def redact_text(value: str) -> str:
    """Replace assignment values for supported credential keys, case-insensitively."""

    return CREDENTIAL_ASSIGNMENT.sub(
        lambda match: f"{match.group('key')}{match.group('before')}={match.group('after')}[REDACTED]",
        value,
    )


def redact_model(model: _Model) -> _Model:
    """Return the same frozen Pydantic contract with all text leaves redacted."""

    return type(model).model_validate(_redact_value(model.model_dump()))


def _redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value
