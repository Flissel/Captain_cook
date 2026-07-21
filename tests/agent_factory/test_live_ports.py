from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.execution_budget import InMemoryFactoryBudgetLedger
from agenten.agent_factory.hermes_cli import InMemoryFactorySkillReplayStore
from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_runtime.contracts import IntegrationIntent
from agenten.agent_runtime.capabilities import derive_grant
from agenten.agent_runtime.contracts import AgentRuntimeResult, RuntimeStatus
from agenten.agent_factory.forge_contracts import (
    ArtifactRef as ForgeArtifactRef,
    DocumentationEvidence,
    DocumentationQuery,
)
from agenten.delivery.minibook_events import MinibookProjectionEvent
from tests.agent_runtime.test_service import artifact, batch_for, command_for
from tests.agent_factory.test_team_execution import (
    NOW,
    _PricingAuthority,
    _invocation,
    _job_v3,
    _pricing_quote,
    _released_skill_fixture,
)


def test_pricing_adapter_rejects_unknown_and_cross_job_quotes() -> None:
    from agenten.agent_factory.live_pricing import CaptainPricingAuthorityAdapter

    job = _job_v3()

    class Source:
        quote = None

        def resolve_quote(self, **_: object):
            return self.quote

    source = Source()
    authority = CaptainPricingAuthorityAdapter(source)
    invocation = _invocation(job)
    with pytest.raises(ValueError, match="unknown"):
        authority.resolve(
            job=job,
            invocation=invocation,
            provider="provider",
            model="approved-model-id",
            now=NOW,
        )

    source.quote = _pricing_quote(job).model_copy(
        update={"job_id": job.job_id.int.to_bytes(16, "big").hex()}
    )
    with pytest.raises(ValueError, match="job|bound"):
        authority.resolve(
            job=job,
            invocation=invocation,
            provider="deterministic-replay",
            model="approved-model-id",
            now=NOW,
        )

    source.quote = _pricing_quote(job)
    assert authority.resolve(
        job=job,
        invocation=invocation,
        provider="deterministic-replay",
        model="approved-model-id",
        now=NOW,
    ) == source.quote


@pytest.mark.asyncio
async def test_private_holdout_adapter_selects_only_captain_refs_and_checks_digest() -> None:
    from agenten.agent_factory.live_holdouts import (
        CaptainPrivateHoldoutAdapter,
        CaptainPrivateHoldoutResolver,
        CaptainPrivateHoldoutSelector,
    )
    from agenten.agent_factory.team_execution import (
        FactoryHoldoutAssertionDecisionV1,
        FactoryHoldoutEvaluationReceiptV1,
    )

    body = b"Captain private case"
    job = _job_v3(holdout_body=body)
    selected = CaptainPrivateHoldoutSelector(
        job=job,
        holdout_id=job.private_holdout_refs[0].holdout_id,
    )
    assert selected(job) == job.private_holdout_refs[0]
    with pytest.raises(ValueError, match="different job"):
        selected(job.model_copy(update={"subject_version": 2}))
    with pytest.raises(ValueError, match="unknown"):
        CaptainPrivateHoldoutSelector(job=job, holdout_id="holdout-999999999999")

    class Source:
        current = body

        async def read(self, _reference):
            return self.current

    source = Source()
    resolver = CaptainPrivateHoldoutResolver(job=job, source=source)
    resolved = await resolver.resolve(job.private_holdout_refs[0])
    assert resolved.body == body
    source.current = b"substituted"
    with pytest.raises(ValueError, match="digest"):
        await resolver.resolve(job.private_holdout_refs[0])

    source.current = body
    candidate_ref = job.input_ref

    class Evaluator:
        async def evaluate(self, reference, _result, assertion_ids):
            return FactoryHoldoutEvaluationReceiptV1(
                schema_name="captain.factory-holdout-evaluation-receipt.v1",
                holdout_ref=reference,
                candidate_ref=candidate_ref,
                assertion_ids=assertion_ids,
                decisions=tuple(
                    FactoryHoldoutAssertionDecisionV1(
                        assertion_id=assertion_id,
                        passed=True,
                        provenance_code="captain_private_rule",
                    )
                    for assertion_id in assertion_ids
                ),
                evaluator_id="captain_private_evaluator",
                evaluator_version="1",
                evaluated_at=NOW,
            )

    adapter = CaptainPrivateHoldoutAdapter(
        job=job,
        source=source,
        evaluator=Evaluator(),  # type: ignore[arg-type]
    )
    assert (await adapter.resolve(job.private_holdout_refs[0])).body == body
    receipt = await adapter.evaluate(
        job.private_holdout_refs[0],
        object(),
        job.acceptance_assertion_ids,
    )
    assert receipt.assertion_ids == job.acceptance_assertion_ids
    with pytest.raises(ValueError, match="exactly"):
        await adapter.evaluate(
            job.private_holdout_refs[0],
            object(),
            (job.acceptance_assertion_ids[0],),
        )


def test_scoped_n8n_adapter_requires_declared_intent_and_typed_evidence() -> None:
    from agenten.agent_factory.live_n8n import ScopedCaptainN8nMcpAdapter

    class Delegate:
        def tool(self, name: str) -> object:
            return f"tool:{name}"

        def authorization(self, name: str) -> object:
            return f"authorization:{name}"

        def observed_evidence(self) -> tuple[object, ...]:
            return (object(),)

    job = _job_v3()
    ordinary_lease = issue_factory_lease(
        job=job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/n8n",
        now=NOW,
    )
    with pytest.raises(ValueError, match="integration_intent=n8n"):
        ScopedCaptainN8nMcpAdapter(
            lease=ordinary_lease,
            delegate=Delegate(),  # type: ignore[arg-type]
        )
    n8n_lease = issue_factory_lease(
        job=job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/n8n",
        now=NOW,
        integration_intent=IntegrationIntent.N8N,
    )
    adapter = ScopedCaptainN8nMcpAdapter(
        lease=n8n_lease,
        delegate=Delegate(),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="typed"):
        adapter.observed_evidence()


def test_live_composition_passes_only_explicit_authoritative_team_ports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenten.agent_factory import live_composition
    from agenten.agent_factory.live_composition import (
        FactoryLiveRuntimePorts,
        compose_live_factory_runtime,
    )

    job = _job_v3()
    released, skill_root = _released_skill_fixture(tmp_path)

    class Catalog:
        def released_for(self, *_: object):
            return released

    sentinel_adapter = object()
    captured: dict[str, object] = {}

    def compose(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel_adapter

    monkeypatch.setattr(live_composition, "_compose_live_team_execution", compose)
    model_client_for = lambda *_: object()
    budget = InMemoryFactoryBudgetLedger()
    pricing = _PricingAuthority(_pricing_quote(job))
    replay = InMemoryFactorySkillReplayStore()
    holdouts = object()
    n8n_adapter = object()
    n8n_authority = object()
    clock = lambda: NOW
    ports = FactoryLiveRuntimePorts(
        hermes=object(),  # type: ignore[arg-type]
        codex=object(),  # type: ignore[arg-type]
        context7=None,
        candidate_provider=object(),  # type: ignore[arg-type]
        minibook=None,
        model_client_for=model_client_for,  # type: ignore[arg-type]
        budget=budget,
        pricing_authority=pricing,
        replay_store=replay,
        holdouts=holdouts,  # type: ignore[arg-type]
        n8n_adapter=n8n_adapter,  # type: ignore[arg-type]
        n8n_authority=n8n_authority,  # type: ignore[arg-type]
        released_skill_catalog=Catalog(),  # type: ignore[arg-type]
        skill_root=skill_root,
        tools={},
        provider="deterministic-replay",
        model="approved-model-id",
        max_cost_per_call=Decimal("0.50"),
        clock=clock,
    )
    evidence_store = FilesystemFactoryEvidenceStore(tmp_path / "evidence")
    components = compose_live_factory_runtime(
        job=job,
        evidence_store=evidence_store,
        ports=ports,
        holdout_id=job.private_holdout_refs[0].holdout_id,
    )

    assert components.team_execution is sentinel_adapter
    assert components.hermes is ports.hermes
    assert components.codex is ports.codex
    assert components.candidate_provider is ports.candidate_provider
    assert captured["job"] is job
    assert captured["evidence_store"] is evidence_store
    team_ports = captured["ports"]
    assert team_ports.model_client_for is model_client_for
    assert team_ports.budget is budget
    assert team_ports.pricing_authority is pricing
    assert team_ports.replay_store is replay
    assert team_ports.holdouts is holdouts
    assert team_ports.n8n_adapter is n8n_adapter
    assert team_ports.n8n_authority is n8n_authority
    assert team_ports.clock is clock
    assert captured["holdout_selector"](job) == job.private_holdout_refs[0]

    with pytest.raises(ValueError, match="authoritative port"):
        compose_live_factory_runtime(
            job=job,
            evidence_store=evidence_store,
            ports=replace(ports, codex=None),  # type: ignore[arg-type]
            holdout_id=job.private_holdout_refs[0].holdout_id,
        )


@pytest.mark.asyncio
async def test_codex_adapter_requires_bound_session_and_content_evidence() -> None:
    from agenten.agent_factory.live_codex import BoundCodexBuildAdapter

    command = command_for()
    grant = derive_grant(command, batch_for(command), NOW)
    evidence = artifact("codex-evidence", "b")

    class Runtime:
        result = AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=uuid4(),
            command_id=command.event_id,
            correlation_id=command.correlation_id,
            occurred_at=NOW,
            producer="agent-runtime",
            subject_id=command.subject_id,
            subject_version=command.subject_version,
            grant_id=grant.grant_id,
            operation=command.payload.operation,
            status=RuntimeStatus.SUCCEEDED,
            artifact_refs=(evidence,),
            evidence_refs=(evidence,),
        )

        async def start(self, *_: object, **__: object) -> AgentRuntimeResult:
            return self.result

        async def resume(self, *_: object, **__: object) -> AgentRuntimeResult:
            return self.result

    runtime = Runtime()
    adapter = BoundCodexBuildAdapter(runtime=runtime, clock=lambda: NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="session"):
        await adapter.execute(command, grant)

    runtime.result = runtime.result.model_copy(update={"session_id": "codex-session-1"})
    assert await adapter.execute(command, grant) == runtime.result


def test_context7_adapter_rejects_mismatched_or_unversioned_provenance() -> None:
    from agenten.agent_factory.live_context7 import VerifiedContext7DocumentationAdapter

    query = DocumentationQuery(
        ecosystem="autogen",
        package_id="autogen-agentchat",
        installed_version="0.7.5",
        query="Swarm handoff semantics",
        required=True,
    )
    encoded = json.dumps(
        query.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    ref = ForgeArtifactRef(
        uri="artifact://context7/autogen-agentchat",
        sha256="c" * 64,
        media_type="text/markdown",
    )

    class Delegate:
        evidence = DocumentationEvidence(
            query=query,
            query_sha256=hashlib.sha256(encoded).hexdigest(),
            retrieved_version="0.7.5",
            retrieved_at=NOW,
            source_refs=(ref,),
            content_sha256="d" * 64,
        )

        def resolve(self, _query: DocumentationQuery) -> DocumentationEvidence:
            return self.evidence

    delegate = Delegate()
    adapter = VerifiedContext7DocumentationAdapter(delegate)  # type: ignore[arg-type]
    assert adapter.resolve(query) == delegate.evidence
    delegate.evidence = delegate.evidence.model_copy(
        update={"retrieved_version": "unknown"}
    )
    with pytest.raises(ValueError, match="version"):
        adapter.resolve(query)


def test_forge_candidate_adapter_requires_sealed_archive_digest(tmp_path: Path) -> None:
    from agenten.agent_factory.live_forge import SealedForgeCandidateProvider

    from tests.agent_factory.test_team_execution import _candidate

    job = _job_v3()
    candidate = _candidate(tmp_path)

    class Provider:
        def candidate_for(self, _job):
            return candidate

    provider = SealedForgeCandidateProvider(Provider())  # type: ignore[arg-type]
    assert provider.candidate_for(job) == candidate
    candidate.source_archive.write_bytes(b"substituted candidate")
    with pytest.raises(ValueError, match="digest"):
        provider.candidate_for(job)


@pytest.mark.asyncio
async def test_minibook_projection_adapter_is_correlated_read_only() -> None:
    from agenten.agent_factory.live_minibook import ReadOnlyMinibookProjectionAdapter

    correlation_id = uuid4()
    event = MinibookProjectionEvent.model_validate(
        {
            "schema": "captain.minibook-projection.v2",
            "event_id": str(uuid4()),
            "correlation_id": str(correlation_id),
            "causation_id": str(uuid4()),
            "occurred_at": datetime.now(timezone.utc),
            "producer": "captain-gateway",
            "subject_id": f"subject:{uuid4()}",
            "subject_version": 1,
            "event_type": "codex.result",
            "payload": {
                "view": "build",
                "template_id": "runtime_build_recorded",
                "status_id": "built",
                "actor_role_id": "captain_gateway",
            },
        }
    )

    class Feed:
        events: tuple[object, ...] = (event,)

        async def events_for_correlation(self, _correlation_id):
            return self.events

    feed = Feed()
    reader = ReadOnlyMinibookProjectionAdapter(feed)  # type: ignore[arg-type]
    assert await reader.events_for_correlation(correlation_id) == (event,)
    feed.events = ()
    with pytest.raises(ValueError, match="no correlated"):
        await reader.events_for_correlation(correlation_id)
