"""Captain-side validation of documentation evidence returned by Hermes."""
from __future__ import annotations

from typing import Protocol

from .forge_contracts import DocumentationEvidence, DocumentationQuery


class DocumentationPolicyError(RuntimeError):
    pass


class FactoryDocumentationPort(Protocol):
    async def resolve(
        self, queries: tuple[DocumentationQuery, ...]
    ) -> tuple[DocumentationEvidence, ...]: ...


def validate_documentation(evidence: DocumentationEvidence) -> DocumentationEvidence:
    expected = evidence.query.installed_version.split(".")[:2]
    retrieved = evidence.retrieved_version.split(".")[:2]
    if len(expected) != 2 or len(retrieved) != 2 or expected != retrieved:
        raise DocumentationPolicyError("documentation version does not match installed package")
    if not evidence.source_refs:
        raise DocumentationPolicyError("documentation evidence requires sources")
    return evidence
