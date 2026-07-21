from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agenten.agent_factory.forge_contracts import DocumentationEvidence, DocumentationQuery
from agenten.agent_factory.forge_documentation import DocumentationPolicyError, validate_documentation


def test_autogen_documentation_requires_matching_official_version() -> None:
    query = DocumentationQuery(
        ecosystem="autogen", package_id="autogen-agentchat", installed_version="0.7.5",
        query="official teams API", required=True,
    )
    evidence = DocumentationEvidence(
        query=query, query_sha256="a" * 64, retrieved_version="0.7.5",
        retrieved_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        source_refs=({"uri": "artifact://docs/autogen", "sha256": "b" * 64, "media_type": "text/html"},),
        content_sha256="c" * 64,
    )
    assert validate_documentation(evidence).retrieved_version == "0.7.5"
    with pytest.raises(DocumentationPolicyError):
        validate_documentation(evidence.model_copy(update={"retrieved_version": "0.6.4"}))


def test_n8n_documentation_is_not_required_without_declared_integration() -> None:
    queries = (
        DocumentationQuery(
            ecosystem="autogen", package_id="autogen-agentchat", installed_version="0.7.5",
            query="official teams API", required=True,
        ),
    )
    assert all(query.ecosystem != "n8n" for query in queries)
