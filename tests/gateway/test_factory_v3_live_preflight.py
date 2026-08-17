from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest

import gateway.agent_factory_live_operator as live_operator


ROOT = Path(__file__).parents[2]
INPUT = ROOT / "tests" / "fixtures" / "agent_factory" / "sales_pipeline_brief.v3.json"
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
RUN_ID = UUID("a1000000-0000-0000-0000-000000000001")


class _ReadyReadOnlyProbe:
    def endpoint_ready(self, url: str, *, token: str) -> bool:
        assert url.startswith("https://")
        assert token
        return True

    def released_skill_active(
        self,
        *,
        skill_name: str,
        skill_sha256: str,
    ) -> bool:
        assert skill_name == "captain-factory-brief-codex"
        assert len(skill_sha256) == 64
        return True


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
    hermes_root = tmp_path / "hermes-agent"
    (hermes_root / "hermes_cli").mkdir(parents=True)
    (hermes_root / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (hermes_root / "concurrent_log_handler.py").write_text("", encoding="utf-8")
    return {
        "CAPTAIN_FACTORY_V3_LIVE": "1",
        "CAPTAIN_FACTORY_V3_GATEWAY_HEALTH_URL": "https://gateway.example/healthz",
        "CAPTAIN_FACTORY_V3_MINIBOOK_HEALTH_URL": "https://minibook.example/healthz",
        "CAPTAIN_FACTORY_V3_RELEASED_SKILL_NAME": "captain-factory-brief-codex",
        "CAPTAIN_FACTORY_V3_RELEASED_SKILL_SHA256": _skill_digest(skill),
        "CAPTAIN_FACTORY_V3_RELEASED_SKILL_ROOT": str(skill_root),
        "CAPTAIN_FACTORY_V3_WORKER_ID": "hermes-live-worker",
        "CAPTAIN_FACTORY_V3_WORKER_TOKEN": "worker-secret-canary",
        "CAPTAIN_FACTORY_V3_CREATION_API_KEY": "creation-secret-canary",
        "CAPTAIN_FACTORY_V3_CAPTAIN_TOKEN": "captain-secret-canary",
        "CAPTAIN_FACTORY_V3_PORTAL_URL": "https://portal.example/",
        "CAPTAIN_FACTORY_V3_CREDENTIAL_REF": "credential://tenant-a/crm-readonly",
        "CAPTAIN_FACTORY_V3_HERMES_PYTHON": sys.executable,
        "CAPTAIN_FACTORY_V3_HERMES_MODULE_ROOT": str(hermes_root),
        "CAPTAIN_CODEX_EXECUTABLE": sys.executable,
        "CAPTAIN_PWSH_EXECUTABLE": sys.executable,
        "CAPTAIN_CODEX_HOME": str(codex_home),
        "CAPTAIN_FACTORY_V3_MAX_WALL_CLOCK_SECONDS": "600",
        "CAPTAIN_FACTORY_V3_MAX_FILES": "50",
        "CAPTAIN_FACTORY_V3_MAX_PROVIDER_COST_USD": "1.50",
        "CAPTAIN_FACTORY_V3_DEADLINE_AT": (NOW + timedelta(minutes=10)).isoformat(),
        "CAPTAIN_FACTORY_V3_NETWORK_ALLOWED": "false",
    }


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "run" / "workspace"
    cas = tmp_path / "run" / "cas"
    state = tmp_path / "run" / "state"
    for path in (workspace, cas, state):
        path.mkdir(parents=True)
    return workspace, cas, state


def test_live_preflight_reports_missing_requirements_without_secrets_or_paths(
    tmp_path: Path,
) -> None:
    workspace, cas, state = _roots(tmp_path)

    report = live_operator.preflight_factory_v3_live(
        environment={},
        repository_root=ROOT,
        input_path=INPUT,
        workspace_root=workspace,
        cas_root=cas,
        state_root=state,
        run_id=RUN_ID,
        clock=lambda: NOW,
    )

    assert report.status == "unmet_preflight"
    assert report.ready is False
    assert {check.code for check in report.checks if check.status == "unmet"} == {
        "codex_session",
        "credential_separation",
        "execution_bounds",
        "gateway_reachability",
        "hermes_runtime",
        "live_opt_in",
        "minibook_reachability",
        "portal_prerequisites",
        "released_skill_authority",
        "runtime_enforcement",
    }
    serialized = json.dumps(report.as_dict(), sort_keys=True)
    assert str(tmp_path) not in serialized
    assert "secret" not in serialized.casefold()


def test_fully_shaped_live_preflight_rejects_the_unsealed_powershell_runner(
    tmp_path: Path,
) -> None:
    workspace, cas, state = _roots(tmp_path)
    environment = _configured_environment(tmp_path)

    report = live_operator.preflight_factory_v3_live(
        environment=environment,
        repository_root=ROOT,
        input_path=INPUT,
        workspace_root=workspace,
        cas_root=cas,
        state_root=state,
        run_id=RUN_ID,
        clock=lambda: NOW,
        _probe=_ReadyReadOnlyProbe(),
    )

    assert report.status == "unmet_preflight"
    assert tuple(
        check.code for check in report.checks if check.status == "unmet"
    ) == ("runtime_enforcement",)
    serialized = json.dumps(report.as_dict(), sort_keys=True)
    for forbidden in (
        "worker-secret-canary",
        "creation-secret-canary",
        "captain-secret-canary",
        str(workspace),
        str(cas),
        str(state),
    ):
        assert forbidden not in serialized


def test_action_inbox_metadata_is_safe_but_cannot_bypass_runtime_preflight(
    tmp_path: Path,
) -> None:
    workspace, cas, state = _roots(tmp_path)
    environment = _configured_environment(tmp_path)
    environment.pop("CAPTAIN_FACTORY_V3_CREDENTIAL_REF")
    environment.update(
        {
            "CAPTAIN_FACTORY_V3_ACTION_INBOX_URL": (
                "https://portal.example/factory-actions/"
                "a2000000-0000-0000-0000-000000000002"
            ),
            "CAPTAIN_FACTORY_V3_ACTION_REQUEST_ID": (
                "a2000000-0000-0000-0000-000000000002"
            ),
            "CAPTAIN_FACTORY_V3_SAFE_CHECKPOINT_SHA256": "b" * 64,
        }
    )

    report = live_operator.preflight_factory_v3_live(
        environment=environment,
        repository_root=ROOT,
        input_path=INPUT,
        workspace_root=workspace,
        cas_root=cas,
        state_root=state,
        run_id=RUN_ID,
        clock=lambda: NOW,
        _probe=_ReadyReadOnlyProbe(),
    )

    assert report.status == "unmet_preflight"
    assert report.action_inbox is not None
    assert report.action_inbox.status == "pending"
    assert report.action_inbox.url == environment["CAPTAIN_FACTORY_V3_ACTION_INBOX_URL"]
    assert report.action_inbox.request_id == UUID(
        environment["CAPTAIN_FACTORY_V3_ACTION_REQUEST_ID"]
    )
    assert report.action_inbox.safe_checkpoint_sha256 == "b" * 64
    assert "CAPTAIN_FACTORY_V3_CREDENTIAL_REF" not in json.dumps(
        report.as_dict(), sort_keys=True
    )


def test_preflight_cli_writes_sanitized_unmet_report_and_returns_nonzero(
    tmp_path: Path,
    capsys,
) -> None:
    workspace, cas, state = _roots(tmp_path)
    output = tmp_path / "run" / "preflight.json"

    exit_code = live_operator.factory_v3_live_preflight_main(
        [
            "--repository-root",
            str(ROOT),
            "--input",
            str(INPUT),
            "--workspace-root",
            str(workspace),
            "--cas-root",
            str(cas),
            "--state-root",
            str(state),
            "--run-id",
            str(RUN_ID),
            "--output",
            str(output),
        ],
        environment={},
        clock=lambda: NOW,
    )

    assert exit_code == 3
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == (
        "unmet_preflight"
    )
    stdout = json.loads(capsys.readouterr().out)
    assert stdout["status"] == "unmet_preflight"
    assert stdout["run_id"] == str(RUN_ID)
    assert "output" not in stdout
    assert str(tmp_path) not in json.dumps(stdout)


def test_live_operator_refuses_v3_composition_before_any_external_port() -> None:
    with pytest.raises(ValueError, match="runtime enforcement is unavailable"):
        live_operator.compose_business_demo_factory_operator(
            SimpleNamespace(factory_v3_enabled=True),  # type: ignore[arg-type]
            environment={},
            n8n_client=object(),  # type: ignore[arg-type]
            minibook_client=object(),  # type: ignore[arg-type]
        )
