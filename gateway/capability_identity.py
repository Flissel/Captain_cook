"""Canonical identities shared by generic promotion, execution, and projection."""

from __future__ import annotations

import hashlib
from uuid import UUID


def generic_capability_subject_id(job_id: UUID) -> str:
    """Return the sole deterministic Minibook subject for a Factory job."""

    material = bytearray(
        hashlib.sha256(f"generic-capability-subject:{job_id}".encode()).digest()[:16]
    )
    material[6] = (material[6] & 0x0F) | 0x40
    material[8] = (material[8] & 0x3F) | 0x80
    return f"subject:{UUID(bytes=bytes(material))}"


__all__ = ["generic_capability_subject_id"]
