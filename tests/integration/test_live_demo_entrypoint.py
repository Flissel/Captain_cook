from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest

from agenten.agent_factory.live_demo_entrypoint import (
    LiveDemoConfigurationError,
    build_live_demo_hermes,
    load_live_demo_release,
)
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from tests.integration.test_live_demo_runtime_chain import _release


def test_release_document_round_trips_into_factory_dispatch(tmp_path) -> None:
    expected = _release()
    document = {
        "dispatch": {
            "job": expected.dispatch.job.model_dump(mode="json", by_alias=True),
            "action": expected.dispatch.action.model_dump(mode="json"),
            "role": expected.dispatch.role.value if expected.dispatch.role else None,
            "lease": expected.dispatch.lease.model_dump(mode="json", by_alias=True)
            if expected.dispatch.lease
            else None,
        },
        "command": expected.command.model_dump(mode="json", by_alias=True),
    }
    release_path = tmp_path / "release.json"
    release_path.write_text(json.dumps(document), encoding="utf-8")

    observed = load_live_demo_release(release_path)

    assert observed == expected


def _directory_digest(path: Path) -> str:
    manifest = [
        {
            "path": item.relative_to(path).as_posix(),
            "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
            "size": item.stat().st_size,
        }
        for item in sorted(
            candidate for candidate in path.rglob("*") if candidate.is_file()
        )
    ]
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_live_demo_builds_hermes_with_concrete_released_catalog(tmp_path) -> None:
    release = _release()
    skill_root = tmp_path / "skills"
    skill_directory = skill_root / "captain-factory-brief-codex"
    skill_directory.mkdir(parents=True)
    (skill_directory / "SKILL.md").write_text("# Brief Codex\n", encoding="utf-8")
    digest = _directory_digest(skill_directory)
    released = ReleasedHermesSkill(
        schema_name="captain.released-hermes-skill.v1",
        skill_id="captain-factory-brief-codex",
        version=1,
        capability="factory_workflow",
        content_ref={
            "uri": "artifact://released-skills/captain-factory-brief-codex/v1",
            "sha256": digest,
            "media_type": "application/json",
        },
        content_sha256=digest,
        status="released",
        released_at=release.dispatch.lease.issued_at - timedelta(seconds=1),
        producer="captain",
    )
    catalog_path = (
        tmp_path
        / "catalog"
        / str(release.dispatch.job.job_id)
        / f"{FactorySkillStep.BRIEF_CODEX.value}.json"
    )
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(released.model_dump_json(by_alias=True), encoding="utf-8")

    factory = build_live_demo_hermes(
        dispatch=release.dispatch,
        hermes_executable="hermes",
        skill_root=skill_root,
        released_skill_root=tmp_path / "catalog",
        evidence_root=tmp_path / "evidence",
        clock=lambda: release.dispatch.lease.issued_at,
    )

    assert factory is not None


def test_live_demo_fails_startup_when_released_catalog_is_missing(tmp_path) -> None:
    release = _release()

    with pytest.raises(LiveDemoConfigurationError, match="released factory skills"):
        build_live_demo_hermes(
            dispatch=release.dispatch,
            hermes_executable="hermes",
            skill_root=tmp_path / "skills",
            released_skill_root=tmp_path / "catalog",
            evidence_root=tmp_path / "evidence",
            clock=lambda: release.dispatch.lease.issued_at,
        )
