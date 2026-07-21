"""Strict provenance checking for an injected Context7 documentation reader."""

from __future__ import annotations

import hashlib
import json

from agenten.agent_factory.codebase_discovery import DocumentationDiscoveryPort
from agenten.agent_factory.forge_contracts import DocumentationEvidence, DocumentationQuery


class VerifiedContext7DocumentationAdapter:
    """Accept only exact-query, installed-version Context7 evidence."""

    def __init__(self, delegate: DocumentationDiscoveryPort) -> None:
        if delegate is None:
            raise ValueError("Context7 documentation delegate is required")
        self._delegate = delegate

    def resolve(self, query: DocumentationQuery) -> DocumentationEvidence:
        evidence = self._delegate.resolve(query)
        if not isinstance(evidence, DocumentationEvidence):
            raise ValueError("Context7 returned untyped documentation evidence")
        if evidence.query != query or evidence.query_sha256 != _query_digest(query):
            raise ValueError("Context7 provenance does not match the requested query")
        if evidence.retrieved_version != query.installed_version:
            raise ValueError("Context7 provenance does not match the installed version")
        return evidence


def _query_digest(query: DocumentationQuery) -> str:
    payload = json.dumps(
        query.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
