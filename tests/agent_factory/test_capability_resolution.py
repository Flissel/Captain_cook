from __future__ import annotations

import json
from pathlib import Path

from agenten.agent_factory.capability_resolution import CapabilityResolver
from agenten.agent_factory.contracts import AgentFactoryJobV2, PromotedCapability


FIXTURES = Path(__file__).parents[1] / "fixtures" / "agent_factory"


def job() -> AgentFactoryJobV2:
    return AgentFactoryJobV2.model_validate_json((FIXTURES / "agent_factory_job.v2.json").read_text(encoding="utf-8"))


def artifact(name: str, digest: str) -> dict[str, str]:
    return {"uri": f"artifact://capability/{name}/{digest}", "sha256": digest, "media_type": "application/json"}


def capability() -> PromotedCapability:
    return PromotedCapability.model_validate({
        "capability_id": "customer_support_triage", "version": 1, "status": "ready_to_use",
        "blueprint_ref": artifact("blueprint", "1" * 64), "code_ref": artifact("code", "2" * 64),
        "tool_refs": [], "promotion_block_ref": artifact("promotion", "3" * 64),
    })


class Catalog:
    def __init__(self, result: PromotedCapability | None) -> None:
        self.result = result
        self.calls = 0

    def compatible_capability(self, requested: AgentFactoryJobV2) -> PromotedCapability | None:
        self.calls += 1
        return self.result


def test_compatible_catalog_hit_reuses_content_addressed_capability() -> None:
    catalog = Catalog(capability())
    result = CapabilityResolver(catalog).resolve(job())
    assert result.kind == "reuse"
    assert result.capability == capability()
    assert result.creation_key is None
    assert catalog.calls == 1


def test_catalog_miss_creates_digest_bound_idempotent_request() -> None:
    catalog = Catalog(None)
    resolver = CapabilityResolver(catalog)
    first = resolver.resolve(job())
    second = resolver.resolve(job())
    assert first == second
    assert first.kind == "create"
    assert first.capability is None
    assert first.creation_key is not None
    assert first.creation_key.startswith("factory-create-")
    assert len(first.creation_key) == len("factory-create-") + 64


def test_incompatible_or_stale_catalog_result_is_a_miss() -> None:
    incompatible = capability().model_copy(update={"capability_id": "other_capability"})
    stale = capability().model_copy(update={"version": 0})
    for candidate in (incompatible, stale):
        result = CapabilityResolver(Catalog(candidate)).resolve(job())
        assert result.kind == "create"
