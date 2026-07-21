"""Composition root for the opt-in Live Demo A2 one-shot command."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable

import httpx
from pydantic import ValidationError

from agenten.agent_factory.contracts import AgentFactoryJob, FactoryLease, FactoryRole
from agenten.agent_factory.hermes_cli import (
    FilesystemFactorySkillReplayStore,
    FilesystemReleasedFactorySkillCatalog,
    HermesCliFactory,
    HermesCliSettings,
)
from agenten.agent_factory.live_demo_one_shot import LiveDemoEvidenceSummary, LiveDemoOneShot
from agenten.agent_factory.live_demo_runtime_chain import (
    LiveDemoRuntimeChain,
    LiveDemoRuntimeRelease,
)
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.state_machine import FactoryAction
from agenten.agent_runtime.contracts import AgentRuntimeCommand
from agenten.agent_runtime.gateway_client import GatewayRuntimeClient
from agenten.agent_runtime.http_executor import AgentRuntimeHttpExecutor
from agenten.delivery.projection_feed_client import GatewayProjectionFeedClient


class LiveDemoConfigurationError(ValueError):
    """The opt-in command lacks a required, non-secret configuration value."""


def load_live_demo_release(path: Path) -> LiveDemoRuntimeRelease:
    """Load one strict Captain release envelope without inventing runtime state."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or set(document) != {"dispatch", "command"}:
            raise ValueError("release must contain only dispatch and command")
        dispatch = document["dispatch"]
        if not isinstance(dispatch, dict) or set(dispatch) != {
            "job",
            "action",
            "role",
            "lease",
        }:
            raise ValueError("release dispatch has an invalid shape")
        role_value = dispatch["role"]
        lease_value = dispatch["lease"]
        return LiveDemoRuntimeRelease(
            dispatch=FactoryDispatch(
                job=AgentFactoryJob.model_validate(dispatch["job"]),
                action=FactoryAction.model_validate(dispatch["action"]),
                role=FactoryRole(role_value) if role_value is not None else None,
                lease=FactoryLease.model_validate(lease_value)
                if lease_value is not None
                else None,
            ),
            command=AgentRuntimeCommand.model_validate(document["command"]),
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, ValidationError):
        raise LiveDemoConfigurationError("live demo release document is invalid") from None


def build_live_demo_hermes(
    *,
    dispatch: FactoryDispatch,
    hermes_executable: str,
    skill_root: Path,
    released_skill_root: Path,
    evidence_root: Path,
    clock: Callable[[], datetime],
) -> HermesCliFactory:
    """Build and preflight the production Hermes adapter without live effects."""

    settings = HermesCliSettings(
        executable=hermes_executable,
        skill_root=skill_root,
        evidence_root=evidence_root,
        released_skill_root=released_skill_root,
    )
    factory = HermesCliFactory(
        settings=settings,
        released_skill_catalog=FilesystemReleasedFactorySkillCatalog(
            released_skill_root
        ),
        replay_store=FilesystemFactorySkillReplayStore(
            evidence_root / "skill-replays"
        ),
        clock=clock,
    )
    try:
        factory.validate_dispatch_configuration(dispatch)
    except FactoryDispatchError as exc:
        raise LiveDemoConfigurationError(
            "released factory skills failed startup validation"
        ) from exc
    return factory


async def run_live_demo_a2(
    *,
    release_path: Path,
    output_path: Path,
    gateway_url: str,
    runtime_url: str,
    hermes_executable: str = "hermes",
    skill_root: Path = Path("agenten/agent_factory/skills"),
    released_skill_root: Path = Path("agenten/agent_factory/released-skills"),
    evidence_root: Path = Path("artifacts/agent-factory/evidence"),
) -> LiveDemoEvidenceSummary:
    """Run only with concrete external adapters and persist redacted JSON evidence."""

    gateway_token = os.environ.get("CAPTAIN_GATEWAY_TOKEN")
    runtime_token = os.environ.get("CAPTAIN_RUNTIME_TOKEN")
    if not gateway_token:
        raise LiveDemoConfigurationError("CAPTAIN_GATEWAY_TOKEN is required")
    if not runtime_token:
        raise LiveDemoConfigurationError("CAPTAIN_RUNTIME_TOKEN is required")
    release = load_live_demo_release(release_path)
    clock = lambda: datetime.now(timezone.utc)
    hermes = build_live_demo_hermes(
        dispatch=release.dispatch,
        hermes_executable=hermes_executable,
        skill_root=skill_root,
        released_skill_root=released_skill_root,
        evidence_root=evidence_root,
        clock=clock,
    )
    async with httpx.AsyncClient(timeout=30.0) as http:
        runner = LiveDemoOneShot(
            chain=LiveDemoRuntimeChain(
                hermes=hermes,
                runtime_service=AgentRuntimeHttpExecutor(runtime_url, runtime_token, http),
                clock=clock,
            ),
            runtime_state=GatewayRuntimeClient(gateway_url, gateway_token, http),
            projection_feed=GatewayProjectionFeedClient(
                gateway_url, gateway_token, http
            ),
        )
        summary = await runner.run(release)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        summary.model_dump_json(by_alias=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary
