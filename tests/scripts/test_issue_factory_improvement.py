from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "issue-factory-improvement.py"


def _module():
    spec = spec_from_file_location("issue_factory_improvement", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_eligible_only_issues_exact_retryable_subset(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    module = _module()
    claims_id = UUID("71000000-0000-0000-0000-000000000001")
    renewal_id = UUID("71000000-0000-0000-0000-000000000002")
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        module,
        "resolve_current_factory_actions",
        lambda dsn, job_ids: {
            claims_id: "dispatch_quality_warden",
            renewal_id: "append_improvement_requested",
        },
    )

    ref = SimpleNamespace(
        sha256="a" * 64,
        model_dump=lambda **_: {
            "uri": f"artifact://factory/{'a' * 64}",
            "sha256": "a" * 64,
            "media_type": "application/json",
        },
    )
    authorization = SimpleNamespace(
        request_block=SimpleNamespace(
            job_id=renewal_id,
            attempt=1,
            event_id=UUID("73000000-0000-0000-0000-000000000001"),
        ),
        authorized_attempt=2,
        failed_evaluation=SimpleNamespace(artifact_ref=ref),
        authorization_ref=ref,
    )

    def issue(**kwargs):
        captured.update(kwargs)
        return (authorization,)

    monkeypatch.setattr(module, "issue_captain_technical_improvements", issue)
    authority_root = (
        tmp_path / ".captain-cook" / "private" / "business-benchmarks"
    )
    authority_root.mkdir(parents=True)
    monkeypatch.setenv(
        "TEST_MARIADB_DSN",
        "mariadb://captain:test@127.0.0.1:33316/captain_test",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            str(SCRIPT),
            "--workspace-root",
            str(tmp_path),
            "--authority-root",
            str(authority_root),
            "--job-id",
            str(claims_id),
            "--job-id",
            str(renewal_id),
            "--issued-at",
            "2026-08-02T12:00:00Z",
            "--eligible-only",
        ],
    )

    assert module.main() == 0
    assert captured["job_ids"] == (renewal_id,)
    output = json.loads(capsys.readouterr().out)
    assert output["requested_job_ids"] == [str(claims_id), str(renewal_id)]
    assert output["skipped_job_ids"] == [str(claims_id)]
    assert [item["job_id"] for item in output["authorizations"]] == [
        str(renewal_id)
    ]
