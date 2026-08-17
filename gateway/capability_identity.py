"""Canonical identities shared by generic promotion, execution, and projection."""

from __future__ import annotations

from uuid import UUID


def generic_capability_subject_id(job_id: UUID) -> str:
    """Return the Factory job UUID without an opaque identity remapping."""

    if type(job_id) is not UUID:
        raise TypeError("generic capability job identity is invalid")
    return f"subject:{job_id}"


__all__ = ["generic_capability_subject_id"]
