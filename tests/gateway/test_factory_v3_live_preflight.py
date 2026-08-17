from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest

from agenten.agent_factory.forge_contracts import FactoryActionRequestV1
import gateway.agent_factory_live_operator as live_operator


ROOT = Path(__file__).parents[2]
INPUT = ROOT / "tests" / "fixtures" / "agent_factory" / "sales_pipeline_brief.v3.json"
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
RUN_ID = UUID("a1000000-0000-0000-0000-000000000001")
CORRELATION_ID = UUID("a1000000-0000-0000-0000-000000000002")
FACTORY_JOB_ID = UUID("a1000000-0000-0000-0000-000000000003")
CREATION_JOB_ID = UUID("a1000000-0000-0000-0000-000000000004")
REQUEST_ID = UUID("a1000000-0000-0000-0000-000000000005")


class _NoExternalCallsProbe:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def service_health(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        self.calls.append("service_health")
        raise AssertionError("local preflight failure must suppress endpoint reads")

    def released_skill_active(self, **kwargs: object) -> bool:
        del kwargs
        self.calls.append("released_skill_active")
        raise AssertionError("local preflight failure must suppress authority reads")

    def action_resume_metadata(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        self.calls.append("action_resume_metadata")
        raise AssertionError("local preflight failure must suppress Action Inbox reads")


class _TypedReadyProbe:
    def __init__(self, resume_metadata: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.resume_metadata = resume_metadata

    def service_health(
        self, *, service: str, origin: str, token: str
    ) -> dict[str, object]:
        assert token
        self.calls.append(("service_health", (service, origin)))
        capabilities = {
            "captain-gateway": [
                "factory-v3-live-authority",
                "action-inbox-read",
            ],
            "minibook-creation": ["creation-jobs-v3", "cas-readback-v1"],
        }[service]
        return {
            "schema": "captain.factory-v3-service-health.v1",
            "service": service,
            "version": "v3",
            "origin": origin,
            "capabilities": capabilities,
            "ready": True,
        }

    def released_skill_active(
        self,
        *,
        gateway_origin: str,
        token: str,
        skill_name: str,
        skill_sha256: str,
    ) -> bool:
        assert gateway_origin == "https://gateway.example"
        assert token and skill_name == "captain-factory-brief-codex"
        assert len(skill_sha256) == 64
        self.calls.append(("released_skill_active", skill_sha256))
        return True

    def action_resume_metadata(
        self, *, gateway_origin: str, token: str, run_id: UUID
    ) -> dict[str, object] | None:
        assert gateway_origin == "https://gateway.example"
        assert token and run_id == RUN_ID
        self.calls.append(("action_resume_metadata", run_id))
        return self.resume_metadata


def _skill_digest(directory: Path) -> str:
    manifest = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            content = path.read_bytes()
            manifest.append(
                {
                    "path": path.relative_to(directory).as_posix(),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
            )
    encoded = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _configured_environment(tmp_path: Path) -> dict[str, str]:
    skill_root = tmp_path / "released-skills"
    skill = skill_root / "captain-factory-brief-codex"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# released skill\n", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    return {
        "CAPTAIN_FACTORY_V3_LIVE": "1",
        "CAPTAIN_FACTORY_V3_GATEWAY_ORIGIN": "https://gateway.example",
        "CAPTAIN_FACTORY_V3_MINIBOOK_ORIGIN": "https://minibook.example",
        "CAPTAIN_FACTORY_V3_RELEASED_SKILL_NAME": "captain-factory-brief-codex",
        "CAPTAIN_FACTORY_V3_RELEASED_SKILL_SHA256": _skill_digest(skill),
        "CAPTAIN_FACTORY_V3_RELEASED_SKILL_ROOT": str(skill_root),
        "CAPTAIN_FACTORY_V3_WORKER_ID": "hermes-live-worker",
        "CAPTAIN_FACTORY_V3_WORKER_TOKEN": "worker-secret-canary",
        "CAPTAIN_FACTORY_V3_CREATION_API_KEY": "creation-secret-canary",
        "CAPTAIN_FACTORY_V3_CAPTAIN_TOKEN": "captain-secret-canary",
        "CAPTAIN_FACTORY_V3_CREDENTIAL_REF": "credential://tenant-a/crm-readonly",
        "CAPTAIN_CODEX_EXECUTABLE": str(tmp_path / "codex.exe"),
        "CAPTAIN_PWSH_EXECUTABLE": str(tmp_path / "pwsh.exe"),
        "CAPTAIN_CODEX_HOME": str(codex_home),
        "CAPTAIN_FACTORY_V3_HERMES_PYTHON": str(tmp_path / "python.exe"),
        "CAPTAIN_FACTORY_V3_MAX_WALL_CLOCK_SECONDS": "300",
        "CAPTAIN_FACTORY_V3_MAX_FILES": "4096",
        "CAPTAIN_FACTORY_V3_MAX_PROVIDER_COST_USD": "1.50",
        "CAPTAIN_FACTORY_V3_DEADLINE_AT": (NOW + timedelta(minutes=5)).isoformat(),
        "CAPTAIN_FACTORY_V3_NETWORK_ALLOWED": "false",
    }


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "run" / "workspace"
    cas = tmp_path / "run" / "cas"
    state = tmp_path / "run" / "state"
    for path in (workspace, cas, state):
        path.mkdir(parents=True)
    return workspace, cas, state


def _action_request() -> FactoryActionRequestV1:
    checkpoint = "b" * 64
    return FactoryActionRequestV1.model_validate(
        {
            "request_id": str(REQUEST_ID),
            "factory_job_id": str(FACTORY_JOB_ID),
            "creation_job_id": str(CREATION_JOB_ID),
            "correlation_id": str(CORRELATION_ID),
            "expected_subject_version": 7,
            "request_type": "credential_required",
            "safe_checkpoint_ref": {
                "uri": f"artifact://factory-checkpoint/{checkpoint}",
                "sha256": checkpoint,
                "media_type": "application/json",
            },
            "safe_checkpoint_sha256": checkpoint,
            "required_scope": "Read the approved account snapshot",
            "allowed_decisions": ["approve", "deny"],
            "message": "A read-only account credential is required.",
            "deadline_at": (NOW + timedelta(minutes=4)).isoformat(),
            "status": "pending",
        }
    )


def _signed_resume_metadata(
    key: str = "resume-verification-key",
    *,
    status: str = "pending",
) -> dict[str, object]:
    request = _action_request()
    payload: dict[str, object] = {
        "schema": "captain.factory-v3-resume-metadata.v1",
        "run_id": str(RUN_ID),
        "correlation_id": str(request.correlation_id),
        "factory_job_id": str(request.factory_job_id),
        "creation_job_id": str(request.creation_job_id),
        "request_id": str(request.request_id),
        "expected_subject_version": request.expected_subject_version,
        "safe_checkpoint_ref": request.safe_checkpoint_ref.model_dump(mode="json"),
        "safe_checkpoint_sha256": request.safe_checkpoint_sha256,
        "deadline_at": request.deadline_at.isoformat().replace("+00:00", "Z"),
        "tenant_id": "tenant-a",
        "action_url": f"https://gateway.example/portal/factory-actions/{REQUEST_ID}",
        "gateway_origin": "https://gateway.example",
        "issued_at": NOW.isoformat().replace("+00:00", "Z"),
        "status": status,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["metadata_sha256"] = hashlib.sha256(canonical).hexdigest()
    payload["captain_signature"] = hmac.new(
        key.encode(), canonical, hashlib.sha256
    ).hexdigest()
    return payload


def test_live_preflight_reports_missing_requirements_without_external_calls_or_secrets(
    tmp_path: Path,
) -> None:
    workspace, cas, state = _roots(tmp_path)
    probe = _NoExternalCallsProbe()

    report = live_operator.preflight_factory_v3_live(
        environment={}, repository_root=ROOT, input_path=INPUT,
        workspace_root=workspace, cas_root=cas, state_root=state,
        run_id=RUN_ID, clock=lambda: NOW, _probe=probe,
    )

    assert report.status == "unmet_preflight"
    assert probe.calls == []
    serialized = json.dumps(report.as_dict(), sort_keys=True)
    assert str(tmp_path) not in serialized
    assert "secret" not in serialized.casefold()


def test_unavailable_runtime_enforcement_suppresses_all_external_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, cas, state = _roots(tmp_path)
    environment = _configured_environment(tmp_path)
    probe = _NoExternalCallsProbe()
    monkeypatch.setattr(live_operator, "_factory_v3_codex_session_ready", lambda *_: True)
    monkeypatch.setattr(live_operator, "_factory_v3_hermes_runtime_ready", lambda *_: True)

    report = live_operator.preflight_factory_v3_live(
        environment=environment, repository_root=ROOT, input_path=INPUT,
        workspace_root=workspace, cas_root=cas, state_root=state,
        run_id=RUN_ID, clock=lambda: NOW, _probe=probe,
    )

    assert probe.calls == []
    assert "runtime_enforcement" in {
        check.code for check in report.checks if check.status == "unmet"
    }


def test_fully_met_local_stage_reads_only_bound_typed_authorities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, cas, state = _roots(tmp_path)
    environment = _configured_environment(tmp_path)
    probe = _TypedReadyProbe()
    monkeypatch.setattr(live_operator, "_factory_v3_live_runtime_enforcement_ready", lambda: True)
    monkeypatch.setattr(live_operator, "_factory_v3_codex_session_ready", lambda *_: True)
    monkeypatch.setattr(live_operator, "_factory_v3_hermes_runtime_ready", lambda *_: True)

    report = live_operator.preflight_factory_v3_live(
        environment=environment, repository_root=ROOT, input_path=INPUT,
        workspace_root=workspace, cas_root=cas, state_root=state,
        run_id=RUN_ID, clock=lambda: NOW, _probe=probe,
    )

    assert report.status == "ready"
    assert probe.calls[:2] == [
        ("service_health", ("captain-gateway", "https://gateway.example")),
        ("service_health", ("minibook-creation", "https://minibook.example")),
    ]
    assert probe.calls[2][0] == "released_skill_active"
    assert all(check.status == "met" for check in report.checks)


def test_legacy_health_url_override_is_rejected_before_token_can_leave_process(
    tmp_path: Path,
) -> None:
    workspace, cas, state = _roots(tmp_path)
    environment = _configured_environment(tmp_path)
    environment["CAPTAIN_FACTORY_V3_GATEWAY_HEALTH_URL"] = "https://attacker.example/collect"
    probe = _NoExternalCallsProbe()

    report = live_operator.preflight_factory_v3_live(
        environment=environment, repository_root=ROOT, input_path=INPUT,
        workspace_root=workspace, cas_root=cas, state_root=state,
        run_id=RUN_ID, clock=lambda: NOW, _probe=probe,
    )

    assert probe.calls == []
    assert next(check for check in report.checks if check.code == "authority_origins").status == "unmet"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": "ok"},
        {"schema": "captain.factory-v3-service-health.v1", "service": "captain-gateway", "version": "v3", "origin": "https://gateway.example", "capabilities": 1, "ready": True},
        {"schema": "captain.factory-v3-service-health.v1", "service": "captain-gateway", "version": "v3", "origin": "https://gateway.example", "capabilities": ["factory-v3-live-authority", "action-inbox-read"], "ready": False},
        {"schema": "captain.factory-v3-service-health.v1", "service": "wrong-service", "version": "v3", "origin": "https://gateway.example", "capabilities": ["factory-v3-live-authority", "action-inbox-read"], "ready": True},
    ],
)
def test_service_health_requires_typed_identity_capability_version_and_readiness(payload: dict[str, object]) -> None:
    assert not live_operator._factory_v3_service_health_payload_ready(payload, service="captain-gateway", origin="https://gateway.example")


def test_service_health_accepts_only_exact_typed_authority_payload() -> None:
    assert live_operator._factory_v3_service_health_payload_ready(
        {"schema": "captain.factory-v3-service-health.v1", "service": "captain-gateway", "version": "v3", "origin": "https://gateway.example", "capabilities": ["factory-v3-live-authority", "action-inbox-read"], "ready": True},
        service="captain-gateway", origin="https://gateway.example",
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://gateway.example",
        "https://gateway.example/healthz",
        "https://gateway.example?next=evil",
        "https://user@gateway.example",
        "https://gateway.example/%2e%2e/evil",
        "https://gateway.example\\evil",
    ],
)
def test_authority_origin_rejects_non_tls_remote_or_caller_controlled_paths(origin: str) -> None:
    assert live_operator._canonical_service_origin(origin) is None


def test_credentials_are_pairwise_distinct_under_constant_time_comparison(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environment = _configured_environment(tmp_path)
    comparisons: list[tuple[bytes, bytes]] = []
    original = hmac.compare_digest

    def compare(left: bytes, right: bytes) -> bool:
        comparisons.append((left, right))
        return original(left, right)

    monkeypatch.setattr(live_operator.hmac, "compare_digest", compare)
    assert live_operator._factory_v3_credentials_ready(environment)
    assert len(comparisons) == 3
    environment["CAPTAIN_FACTORY_V3_WORKER_TOKEN"] = environment["CAPTAIN_FACTORY_V3_CAPTAIN_TOKEN"]
    assert not live_operator._factory_v3_credentials_ready(environment)


def test_action_inbox_resume_metadata_is_gateway_read_signed_and_exactly_bound(tmp_path: Path) -> None:
    metadata = _signed_resume_metadata()
    path = tmp_path / "resume-metadata.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    current = _signed_resume_metadata(status="answered")
    probe = _TypedReadyProbe(resume_metadata=current)

    resolved = live_operator._factory_v3_authoritative_resume_metadata(
        environment={"CAPTAIN_FACTORY_V3_CAPTAIN_TOKEN": "captain-token", "CAPTAIN_FACTORY_V3_RESUME_VERIFY_KEY": "resume-verification-key"},
        gateway_origin="https://gateway.example", run_id=RUN_ID, now=NOW,
        probe=probe, persisted_path=path,
    )

    assert resolved is not None
    assert (resolved.run_id, resolved.correlation_id, resolved.factory_job_id, resolved.creation_job_id, resolved.request_id) == (RUN_ID, CORRELATION_ID, FACTORY_JOB_ID, CREATION_JOB_ID, REQUEST_ID)
    assert resolved.expected_subject_version == 7
    assert resolved.tenant_id == "tenant-a"
    assert resolved.safe_checkpoint_sha256 == "b" * 64
    assert resolved.status == "answered"
    assert ("action_resume_metadata", RUN_ID) in probe.calls


def test_answered_authoritative_action_resumes_without_reemitting_pending_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, cas, state = _roots(tmp_path)
    environment = _configured_environment(tmp_path)
    environment.pop("CAPTAIN_FACTORY_V3_CREDENTIAL_REF")
    environment["CAPTAIN_FACTORY_V3_RESUME_VERIFY_KEY"] = "resume-verification-key"
    persisted = _signed_resume_metadata()
    path = tmp_path / "resume-metadata.json"
    path.write_text(json.dumps(persisted), encoding="utf-8")
    probe = _TypedReadyProbe(
        resume_metadata=_signed_resume_metadata(status="answered")
    )
    monkeypatch.setattr(live_operator, "_factory_v3_live_runtime_enforcement_ready", lambda: True)
    monkeypatch.setattr(live_operator, "_factory_v3_codex_session_ready", lambda *_: True)
    monkeypatch.setattr(live_operator, "_factory_v3_hermes_runtime_ready", lambda *_: True)

    report = live_operator.preflight_factory_v3_live(
        environment=environment, repository_root=ROOT, input_path=INPUT,
        workspace_root=workspace, cas_root=cas, state_root=state,
        run_id=RUN_ID, resume_metadata_path=path, clock=lambda: NOW, _probe=probe,
    )

    assert report.status == "ready"
    assert report.action_inbox is None
    assert report.resume_metadata is not None
    assert report.resume_metadata.status == "answered"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("correlation_id", "a1000000-0000-0000-0000-000000000099"),
        ("factory_job_id", "a1000000-0000-0000-0000-000000000099"),
        ("creation_job_id", "a1000000-0000-0000-0000-000000000099"),
        ("request_id", "a1000000-0000-0000-0000-000000000099"),
        ("expected_subject_version", 8),
        ("tenant_id", "tenant-b"),
        ("safe_checkpoint_sha256", "c" * 64),
    ],
)
def test_resume_rejects_stale_foreign_or_spoofed_authoritative_row(tmp_path: Path, field: str, replacement: object) -> None:
    persisted = _signed_resume_metadata()
    current = _signed_resume_metadata(status="answered")
    current[field] = replacement
    unsigned = {k: v for k, v in current.items() if k not in {"metadata_sha256", "captain_signature"}}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    current["metadata_sha256"] = hashlib.sha256(canonical).hexdigest()
    current["captain_signature"] = hmac.new(b"resume-verification-key", canonical, hashlib.sha256).hexdigest()
    path = tmp_path / "resume-metadata.json"
    path.write_text(json.dumps(persisted), encoding="utf-8")

    assert live_operator._factory_v3_authoritative_resume_metadata(
        environment={"CAPTAIN_FACTORY_V3_CAPTAIN_TOKEN": "captain-token", "CAPTAIN_FACTORY_V3_RESUME_VERIFY_KEY": "resume-verification-key"},
        gateway_origin="https://gateway.example", run_id=RUN_ID, now=NOW,
        probe=_TypedReadyProbe(resume_metadata=current), persisted_path=path,
    ) is None


def test_action_inbox_environment_values_never_become_resume_authority() -> None:
    assert live_operator._factory_v3_authoritative_resume_metadata(
        environment={"CAPTAIN_FACTORY_V3_CAPTAIN_TOKEN": "captain-token", "CAPTAIN_FACTORY_V3_RESUME_VERIFY_KEY": "resume-verification-key", "CAPTAIN_FACTORY_V3_ACTION_REQUEST_ID": str(REQUEST_ID), "CAPTAIN_FACTORY_V3_ACTION_INBOX_URL": "https://gateway.example/fake", "CAPTAIN_FACTORY_V3_SAFE_CHECKPOINT_SHA256": "b" * 64},
        gateway_origin="https://gateway.example", run_id=RUN_ID, now=NOW,
        probe=_TypedReadyProbe(resume_metadata=None), persisted_path=None,
    ) is None


def test_codex_session_requires_canonical_executables_and_pinned_launcher(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts" / "codex-session.ps1", scripts / "codex-session.ps1")
    codex = tmp_path / "codex.exe"
    pwsh = tmp_path / "pwsh.exe"
    codex.write_bytes(b"codex")
    pwsh.write_bytes(b"pwsh")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    environment = {"CAPTAIN_CODEX_EXECUTABLE": str(codex), "CAPTAIN_PWSH_EXECUTABLE": str(pwsh), "CAPTAIN_CODEX_HOME": str(codex_home)}

    assert live_operator._factory_v3_codex_session_ready(environment, repository)
    (scripts / "codex-session.ps1").write_text("# fabricated launcher\n", encoding="utf-8")
    assert not live_operator._factory_v3_codex_session_ready(environment, repository)
    environment["CAPTAIN_CODEX_EXECUTABLE"] = sys.executable
    assert not live_operator._factory_v3_codex_session_ready(environment, ROOT)


def test_hermes_runtime_rejects_arbitrary_python_and_fabricated_module_tree(tmp_path: Path) -> None:
    fabricated = tmp_path / "hermes-agent"
    (fabricated / "hermes_cli").mkdir(parents=True)
    (fabricated / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (fabricated / "concurrent_log_handler.py").write_text("", encoding="utf-8")
    assert not live_operator._factory_v3_hermes_runtime_ready(
        {"CAPTAIN_FACTORY_V3_HERMES_PYTHON": sys.executable, "CAPTAIN_FACTORY_V3_HERMES_MODULE_ROOT": str(fabricated)}, ROOT,
    )


def test_frozen_input_is_bound_to_exact_sha_and_full_authority_manifest() -> None:
    assert live_operator._factory_v3_frozen_input_sha256(ROOT, INPUT) == "81e76f52fdc99f64aeea9e5008c00d6eba25972014ef7563d4fc0851cf85864f"
    manifest = live_operator._FACTORY_V3_AUTHORITY_MANIFEST
    assert manifest["fixture_expected"] == json.loads(INPUT.read_text(encoding="utf-8"))["expected"]
    assert manifest["build_authority"] == {
        "allowed_output_roots": ["src", "tests", "evidence"],
        "allowed_commands": [["python", "-m", "compileall", "src"], ["python", "-m", "pytest"]],
        "limits": {"max_wall_clock_seconds": 300, "max_provider_cost_usd": "1.50", "max_retries": 0, "max_files": 4096},
        "network_allowed": False,
    }


def test_preflight_cli_writes_sanitized_unmet_report_and_returns_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace, cas, state = _roots(tmp_path)
    output = tmp_path / "run" / "preflight.json"
    exit_code = live_operator.factory_v3_live_preflight_main(
        ["--repository-root", str(ROOT), "--input", str(INPUT), "--workspace-root", str(workspace), "--cas-root", str(cas), "--state-root", str(state), "--run-id", str(RUN_ID), "--output", str(output)],
        environment={}, clock=lambda: NOW,
    )
    assert exit_code == 3
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "unmet_preflight"
    assert report["authority_manifest_sha256"] == live_operator._FACTORY_V3_AUTHORITY_MANIFEST_SHA256
    stdout = json.loads(capsys.readouterr().out)
    assert stdout["status"] == "unmet_preflight"
    assert stdout["run_id"] == str(RUN_ID)
    assert "output" not in stdout
    assert str(tmp_path) not in json.dumps(stdout)


def test_live_operator_refuses_v3_composition_before_any_external_port() -> None:
    with pytest.raises(ValueError, match="runtime enforcement is unavailable"):
        live_operator.compose_business_demo_factory_operator(
            SimpleNamespace(factory_v3_enabled=True),  # type: ignore[arg-type]
            environment={}, n8n_client=object(), minibook_client=object(),  # type: ignore[arg-type]
        )
