from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "run_portal_bearer_live.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_portal_bearer_live", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_select_credential_requires_one_exact_name_and_project() -> None:
    runner = _load_module()
    expected = SimpleNamespace(
        credential_id="cred-1",
        credential_name="Captain Demo Bearer",
        credential_type="httpBearerAuth",
        project_id="project-1",
    )

    assert runner.select_credential((expected,), "Captain Demo Bearer") is expected
    with pytest.raises(RuntimeError, match="exactly one"):
        runner.select_credential((expected,), "Foreign")
    with pytest.raises(RuntimeError, match="project-bound"):
        runner.select_credential(
            (SimpleNamespace(**(expected.__dict__ | {"project_id": None})),),
            "Captain Demo Bearer",
        )


def test_build_evidence_contains_provider_proof_without_secret_material() -> None:
    runner = _load_module()
    completion = SimpleNamespace(
        integration_kind="bearer",
        trace_id=UUID("40000000-0000-4000-8000-000000000001"),
        provider_proof_sha256="a" * 64,
        provider_probe_id=UUID("50000000-0000-4000-8000-000000000003"),
        template_ref=SimpleNamespace(sha256="b" * 64),
        deployed_workflow_ref=SimpleNamespace(sha256="c" * 64),
        execution_ref=SimpleNamespace(sha256="d" * 64),
    )
    audit = SimpleNamespace(invocation_count=1, completion_count=1)

    evidence = runner.build_evidence(
        job_id=UUID("10000000-0000-4000-8000-000000000001"),
        correlation_id=UUID("20000000-0000-4000-8000-000000000001"),
        run_id="portal-bearer-live-1",
        credential_id="cred-1",
        completion=completion,
        audit=audit,
    )

    assert evidence["status"] == "passed"
    assert evidence["provider_trace_id"] == str(completion.trace_id)
    assert evidence["provider_proof_sha256"] == "a" * 64
    assert evidence["secrets_emitted"] is False
    rendered = str(evidence).lower()
    for forbidden in ("password", "access_token", "authorization", "client_secret"):
        assert forbidden not in rendered


def test_oauth_evidence_contains_only_provider_bound_exchange_metadata() -> None:
    runner = _load_module()
    completion = SimpleNamespace(
        integration_kind="oauth2",
        trace_id=UUID("40000000-0000-4000-8000-000000000002"),
        provider_proof_sha256="a" * 64,
        provider_probe_id=UUID("50000000-0000-4000-8000-000000000004"),
        template_ref=SimpleNamespace(sha256="b" * 64),
        deployed_workflow_ref=SimpleNamespace(sha256="c" * 64),
        execution_ref=SimpleNamespace(sha256="d" * 64),
        oauth_grant_type="client_credentials",
        oauth_exchange_id=UUID("60000000-0000-4000-8000-000000000004"),
        oauth_exchange_ref=SimpleNamespace(sha256="e" * 64),
    )
    audit = SimpleNamespace(invocation_count=1, completion_count=1)

    evidence = runner.build_evidence(
        job_id=UUID("10000000-0000-4000-8000-000000000001"),
        correlation_id=UUID("20000000-0000-4000-8000-000000000001"),
        run_id="portal-oauth2-live-1",
        credential_id="cred-oauth",
        completion=completion,
        audit=audit,
    )

    assert evidence["oauth_grant_type"] == "client_credentials"
    assert evidence["oauth_exchange_id"] == str(completion.oauth_exchange_id)
    assert evidence["oauth_exchange_sha256"] == "e" * 64
    rendered = str(evidence).lower()
    for forbidden in ("access_token", "client_secret", "authorization"):
        assert forbidden not in rendered
