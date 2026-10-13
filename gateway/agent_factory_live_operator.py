"""Deployable composition for resuming the isolated business-demo Factory jobs."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from blockchain.mariadb_storage import MariaDBStorage
from agenten.agent_factory.business_benchmark_bootstrap import (
    CaptainCanonicalSuiteAuthority,
    CaptainCanonicalSuiteRepository,
)
from agenten.agent_factory.business_benchmark_demo_provisioning import (
    assert_local_captain_test_dsn,
)
from agenten.agent_factory.business_benchmark_dispatch import (
    BusinessBenchmarkDispatchInputs,
)
from agenten.agent_factory.business_benchmark_live import (
    LiveBusinessBenchmarkSettings,
)
from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkContentAddressedArtifactStore,
)
from agenten.agent_factory.business_benchmark_store import (
    FilesystemBusinessBenchmarkEvidenceStore,
)
from agenten.agent_factory.candidate_evaluation import (
    GatewayForgeCandidateProvider,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.codex_build_execution import (
    CaptainCodexAssignmentBuilder,
    CaptainCodexBuildSealer,
    CaptainFactoryCodexResumeAuthorizer,
    CaptainV3CodexRequestMapper,
    CodexCliFactoryBuildExecutor,
    CodexCliFactoryBuildSettings,
    GitDetachedFactoryWorkspacePreparer,
    PowerShellFactoryCodexProcessInspector,
)
from agenten.agent_factory.codex_build_recovery import (
    FilesystemFactoryCodexBuildCheckpointStore,
    FilesystemFactoryCodexScaffoldManifestStore,
    FilesystemFactoryCodexSealedEvidenceStore,
)
from agenten.agent_factory.codex_build_provenance import (
    CaptainCodexBuildReceiptIssuer,
    CodexBuildArtifactCas,
)
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.hermes_cli import HermesCliSettings
from agenten.agent_factory.hermes_factory_worker import (
    AuthenticatedMinibookV3WorkerClient,
    FilesystemHermesFactoryWorkerCheckpointStore,
    HermesFactoryWorker,
)
from agenten.agent_factory.forge_contracts import (
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
)
from agenten.agent_factory.minibook_forge import (
    CaptainCreationJobMapper,
    CaptainCreationJobV3Authority,
    CaptainCreationJobV3Mapper,
    FilesystemCaptainCreationJobV3Store,
    MinibookForgeHttpClient,
    MinibookForgeHttpSettings,
    MinibookForgeSettings,
    MinibookSwarmForge,
)
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.technical_revalidation import (
    FilesystemFactoryTechnicalRevalidationAuthority,
    TECHNICAL_REVALIDATION_EVALUATOR_PATHS,
    TECHNICAL_REVALIDATION_RUNTIME_PATHS,
)
from agenten.agent_factory.state_machine import FactoryActionKind
from agenten.agent_factory.capability_resolution import CapabilityResolution
from agenten.agent_factory.production_dispatch_runner import (
    ProductionFactoryDispatchResult,
)
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.execution.codex_policy import CodexExecutionPolicy
from agenten.execution.codex_supervisor import PowerShellCodexRunner
from gateway.agent_factory_live_composition import (
    AgentFactoryLiveComposition,
    GatewayTechnicalTeamExecutionPortsProvider,
    HermesFactoryWorkerRuntime,
    compose_agent_factory_live,
    select_technical_business_holdout,
)
from gateway.business_benchmark_live_composition import (
    GatewayForgeBusinessBenchmarkCandidateAuthority,
    GatewayBusinessBenchmarkLiveCompositionLoader,
)
from gateway.factory_repository import (
    GatewayFactoryBudgetLedger,
    GatewayFactoryLeases,
    GatewayFactoryRepository,
)
from gateway.factory_forge_evidence import CaptainForgeEvidenceBridge
from gateway.factory_runtime_retry_authority import (
    FilesystemFactoryRuntimeRetryAuthority,
)
from gateway.factory_improvement_authority import (
    FilesystemFactoryImprovementAuthority,
)
from gateway.factory_hermes_retry_authority import (
    FilesystemFactoryHermesRetryAuthority,
)
from gateway.factory_v3_live_authority import (
    FactoryV3LiveAuthorityBinding,
    FactoryV3ServiceBinding,
    load_captain_factory_v3_live_authority,
    verify_factory_v3_local_runtime,
)
from gateway.minibook_creation_artifacts import GatewayMinibookCreationArtifactStore
from gateway.store import GatewayStore


class _DispatchInputComposition(Protocol):
    def dispatch_inputs(
        self,
        settings: LiveBusinessBenchmarkSettings,
        request: FactoryDispatch,
    ) -> BusinessBenchmarkDispatchInputs: ...


class _BenchmarkCodexPromptArtifactStore:
    def __init__(self, artifacts: BusinessBenchmarkContentAddressedArtifactStore) -> None:
        self._artifacts = artifacts

    def persist(self, job_id: UUID, content: bytes) -> ArtifactRef:
        del job_id
        return self._artifacts.put(
            content,
            "application/json",
            namespace="codex-brief-prompt",
        )


class _FactoryCodexArtifactReader:
    """Route immutable inputs to their owning CAS and reject foreign refs."""

    def __init__(
        self,
        *,
        benchmark_artifacts: BusinessBenchmarkContentAddressedArtifactStore,
        minibook_creation_artifacts: GatewayMinibookCreationArtifactStore,
    ) -> None:
        self._benchmark_artifacts = benchmark_artifacts
        self._minibook_creation_artifacts = minibook_creation_artifacts

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        if reference.uri.startswith("artifact://business-benchmark-production/"):
            return self._benchmark_artifacts.read_bytes(reference)
        if reference.uri.startswith("artifact://minibook-creation/"):
            return self._minibook_creation_artifacts.read_bytes(reference)
        raise ValueError("artifact reference has no recognized Factory owner")


def validate_factory_total_cost_envelope(
    *,
    environment: Mapping[str, str],
    benchmark_maximum_usd_per_team: tuple[Decimal, ...],
    benchmark_profiles: tuple[str, ...] = ("claims", "renewal"),
) -> None:
    """Reject any demo composition that could exceed its total team reserve."""

    if environment.get("CAPTAIN_CODEX_AUTH_MODE", "").strip() != (
        "chatgpt_subscription"
    ):
        raise ValueError("Factory Codex must use ChatGPT subscription authentication")
    if (
        environment.get("CAPTAIN_FACTORY_HERMES_PROVIDER", "").strip()
        != "openai-api"
        or environment.get("CAPTAIN_FACTORY_HERMES_MODEL", "").strip()
        != "gpt-5.6-terra"
        or environment.get(
            "CAPTAIN_FACTORY_HERMES_REASONING_EFFORT", ""
        ).strip()
        != "high"
    ):
        raise ValueError("Factory cloud Hermes route is invalid")
    try:
        user_maximum_eur = Decimal(
            _required(environment, "CAPTAIN_FACTORY_USER_MAX_EUR_PER_TEAM")
        )
        budget_eur_per_usd = Decimal(
            _required(environment, "CAPTAIN_FACTORY_BUDGET_EUR_PER_USD")
        )
        total_maximum_usd = Decimal(
            _required(
                environment,
                "CAPTAIN_FACTORY_MAX_TOTAL_COST_USD_PER_TEAM",
            )
        )
        codex_metered_usd = Decimal(
            _required(
                environment,
                "CAPTAIN_FACTORY_CODEX_METERED_USD_PER_TEAM",
            )
        )
        hermes_incremental_usd = Decimal(
            _required(
                environment,
                "CAPTAIN_FACTORY_HERMES_INCREMENTAL_MAX_USD",
            )
        )
        unresolved_hermes_effect_reserve_usd = Decimal(
            _required(
                environment,
                "CAPTAIN_FACTORY_HERMES_UNRESOLVED_EFFECT_RESERVE_USD",
            )
        )
        prior_actual_usd_by_profile = {
            "claims": Decimal(
                _required(
                    environment,
                    "CAPTAIN_FACTORY_PRIOR_ACTUAL_USD_CLAIMS",
                )
            ),
            "renewal": Decimal(
                _required(
                    environment,
                    "CAPTAIN_FACTORY_PRIOR_ACTUAL_USD_RENEWAL",
                )
            ),
        }
    except ArithmeticError as exc:
        raise ValueError("Factory total cost envelope is invalid") from exc
    values = (
        user_maximum_eur,
        budget_eur_per_usd,
        total_maximum_usd,
        codex_metered_usd,
        hermes_incremental_usd,
        unresolved_hermes_effect_reserve_usd,
        *prior_actual_usd_by_profile.values(),
        *benchmark_maximum_usd_per_team,
    )
    selected_prior_actual_usd = tuple(
        prior_actual_usd_by_profile.get(profile, Decimal("NaN"))
        for profile in benchmark_profiles
    )
    if (
        any(not value.is_finite() or value < 0 for value in values)
        or user_maximum_eur != Decimal("6.00")
        or budget_eur_per_usd < Decimal("1.00")
        or total_maximum_usd <= 0
        or total_maximum_usd > Decimal("4.80")
        or total_maximum_usd * budget_eur_per_usd > user_maximum_eur
        or codex_metered_usd != 0
        or hermes_incremental_usd != Decimal("1.50")
        or unresolved_hermes_effect_reserve_usd != Decimal("0.25")
        or len(benchmark_maximum_usd_per_team) not in {1, 2}
        or len(benchmark_profiles) != len(benchmark_maximum_usd_per_team)
        or len(set(benchmark_profiles)) != len(benchmark_profiles)
        or any(profile not in prior_actual_usd_by_profile for profile in benchmark_profiles)
        or any(
            benchmark_usd
            + hermes_incremental_usd
            + prior_usd
            + codex_metered_usd
            > total_maximum_usd
            for benchmark_usd, prior_usd in zip(
                benchmark_maximum_usd_per_team,
                selected_prior_actual_usd,
                strict=True,
            )
        )
    ):
        raise ValueError("Factory total cost envelope is invalid")


_FACTORY_V3_LIVE_SCHEMA = "captain.factory-v3-live-preflight.v1"
_FACTORY_V3_LIVE_SUMMARY_SCHEMA = "captain.factory-v3-live-preflight-summary.v1"
_FACTORY_V3_LIVE_INPUT = Path(
    "tests/fixtures/agent_factory/sales_pipeline_brief.v3.json"
)
_FACTORY_V3_FROZEN_INPUT_SHA256 = (
    "81e76f52fdc99f64aeea9e5008c00d6eba25972014ef7563d4fc0851cf85864f"
)
_FACTORY_V3_AGENT_IDS = (
    "account_researcher",
    "opportunity_qualifier",
    "deal_strategist",
    "executive_brief_writer",
)
_FACTORY_V3_AUTHORITY_MANIFEST: dict[str, object] = {
    "fixture_expected": {
        "required_agent_ids": list(_FACTORY_V3_AGENT_IDS),
        "ordered_handoffs": [
            {
                "from_agent_id": "account_researcher",
                "to_agent_id": "opportunity_qualifier",
            },
            {
                "from_agent_id": "opportunity_qualifier",
                "to_agent_id": "deal_strategist",
            },
            {
                "from_agent_id": "deal_strategist",
                "to_agent_id": "executive_brief_writer",
            },
        ],
        "tool_requirements": [
            {"tool_id": "account_snapshot_reader", "required": True},
            {"tool_id": "opportunity_scoring_rules", "required": True},
            {"tool_id": "industry_signal_reader", "required": False},
        ],
        "allowed_optional_tool_gaps": ["industry_signal_reader"],
        "public_assertion_ids": [
            "assert-2b1d7c6a3666",
            "assert-7f77e654f907",
            "assert-b84ce004c78b",
            "assert-cc0a96c5fc02",
            "assert-9e6cdab86303",
            "assert-9a5995ebfcb8",
            "assert-3e8de9d245ae",
            "assert-3153de4c492a",
        ],
    },
    "build_authority": {
        "allowed_output_roots": ["src", "tests", "evidence"],
        "allowed_commands": [
            ["python", "-m", "compileall", "src"],
            ["python", "-m", "pytest"],
        ],
        "limits": {
            "max_wall_clock_seconds": 300,
            "max_provider_cost_usd": "1.50",
            "max_retries": 0,
            "max_files": 4096,
        },
        "network_allowed": False,
    },
}
_FACTORY_V3_AUTHORITY_MANIFEST_SHA256 = (
    "21fa9a028a9a1b2c53635dcf345d2321f675a7fa2012083c6581dc8ef092a382"
)
_SAFE_CREDENTIAL_REF = re.compile(
    r"^credential://[A-Za-z0-9._~-]+/[A-Za-z0-9._~/-]+$"
)
_SAFE_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class FactoryV3LivePreflightCheck:
    code: str
    status: Literal["met", "unmet"]

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{1,63}", self.code) is None:
            raise ValueError("Factory V3 live preflight check code is invalid")

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "status": self.status}


@dataclass(frozen=True)
class FactoryV3LiveActionInboxStatus:
    status: Literal["pending"]
    url: str
    request_id: UUID
    safe_checkpoint_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "status": self.status,
            "url": self.url,
            "request_id": str(self.request_id),
            "safe_checkpoint_sha256": self.safe_checkpoint_sha256,
        }


@dataclass(frozen=True)
class FactoryV3LiveResumeMetadata:
    run_id: UUID
    correlation_id: UUID
    factory_job_id: UUID
    creation_job_id: UUID
    request_id: UUID
    expected_subject_version: int
    safe_checkpoint_ref: ArtifactRef
    safe_checkpoint_sha256: str
    deadline_at: datetime
    tenant_id: str
    action_url: str
    gateway_origin: str
    issued_at: datetime
    metadata_sha256: str
    captain_signature: str
    status: Literal["pending", "answered"] = "pending"

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema": "captain.factory-v3-resume-metadata.v1",
            "run_id": str(self.run_id),
            "correlation_id": str(self.correlation_id),
            "factory_job_id": str(self.factory_job_id),
            "creation_job_id": str(self.creation_job_id),
            "request_id": str(self.request_id),
            "expected_subject_version": self.expected_subject_version,
            "safe_checkpoint_ref": self.safe_checkpoint_ref.model_dump(mode="json"),
            "safe_checkpoint_sha256": self.safe_checkpoint_sha256,
            "deadline_at": self.deadline_at.isoformat().replace("+00:00", "Z"),
            "tenant_id": self.tenant_id,
            "action_url": self.action_url,
            "gateway_origin": self.gateway_origin,
            "issued_at": self.issued_at.isoformat().replace("+00:00", "Z"),
            "status": self.status,
        }

    def as_dict(self) -> dict[str, object]:
        return self.unsigned_dict() | {
            "metadata_sha256": self.metadata_sha256,
            "captain_signature": self.captain_signature,
        }

    def immutable_binding(self) -> tuple[object, ...]:
        return (
            self.run_id,
            self.correlation_id,
            self.factory_job_id,
            self.creation_job_id,
            self.request_id,
            self.expected_subject_version,
            self.safe_checkpoint_ref.uri,
            self.safe_checkpoint_ref.sha256,
            self.safe_checkpoint_ref.media_type,
            self.safe_checkpoint_sha256,
            self.deadline_at,
            self.tenant_id,
            self.action_url,
            self.gateway_origin,
        )


@dataclass(frozen=True)
class FactoryV3LivePreflightReport:
    run_id: UUID
    checked_at: datetime
    status: Literal["ready", "awaiting_user_input", "unmet_preflight"]
    input_sha256: str
    authority_binding_sha256: str
    active_released_skill_sha256: str
    checks: tuple[FactoryV3LivePreflightCheck, ...]
    action_inbox: FactoryV3LiveActionInboxStatus | None = None
    resume_metadata: FactoryV3LiveResumeMetadata | None = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": _FACTORY_V3_LIVE_SCHEMA,
            "run_id": str(self.run_id),
            "checked_at": self.checked_at.isoformat().replace("+00:00", "Z"),
            "status": self.status,
            "input_sha256": self.input_sha256,
            "authority_manifest_sha256": _FACTORY_V3_AUTHORITY_MANIFEST_SHA256,
            "authority_binding_sha256": self.authority_binding_sha256,
            "active_released_skill_sha256": self.active_released_skill_sha256,
            "checks": [check.as_dict() for check in self.checks],
        }
        if self.action_inbox is not None:
            payload["action_inbox"] = self.action_inbox.as_dict()
        return payload


class _FactoryV3LiveReadOnlyProbe(Protocol):
    def service_health(
        self,
        *,
        service: str,
        origin: str,
        token: str,
    ) -> Mapping[str, object]: ...

    def released_skill_active(
        self,
        *,
        gateway_origin: str,
        token: str,
        skill_name: str,
        skill_sha256: str,
    ) -> Mapping[str, object]: ...

    def action_resume_metadata(
        self,
        *,
        gateway_origin: str,
        token: str,
        run_id: UUID,
    ) -> Mapping[str, object] | None: ...


class _DefaultFactoryV3LiveReadOnlyProbe:
    """Perform bounded health reads and refuse unimplemented authority reads."""

    def _read_json(self, url: str, *, token: str) -> Mapping[str, object] | None:
        try:
            with httpx.Client(follow_redirects=False, timeout=5.0) as client:
                with client.stream(
                    "GET",
                    url,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {token}",
                    },
                ) as response:
                    content = b""
                    for chunk in response.iter_bytes():
                        content += chunk
                        if len(content) > 16_384:
                            return None
                    if response.status_code != 200:
                        return None
                    payload = json.loads(content)
                    return payload if isinstance(payload, dict) else None
        except (httpx.HTTPError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def service_health(
        self,
        *,
        service: str,
        origin: str,
        token: str,
    ) -> Mapping[str, object]:
        del service
        return self._read_json(f"{origin}/healthz", token=token) or {}

    def released_skill_active(
        self,
        *,
        gateway_origin: str,
        token: str,
        skill_name: str,
        skill_sha256: str,
    ) -> Mapping[str, object]:
        del gateway_origin, token, skill_name, skill_sha256
        # The current Gateway has no read-only, correlation-bound active-skill
        # preflight. Local bytes or an environment value are not Captain proof.
        return {}

    def action_resume_metadata(
        self,
        *,
        gateway_origin: str,
        token: str,
        run_id: UUID,
    ) -> Mapping[str, object] | None:
        return self._read_json(
            f"{gateway_origin}/v1/factory/live-runs/{run_id}/resume-metadata",
            token=token,
        )


def _factory_v3_check(code: str, condition: bool) -> FactoryV3LivePreflightCheck:
    return FactoryV3LivePreflightCheck(
        code=code,
        status="met" if condition else "unmet",
    )


def _canonical_service_origin(value: str) -> str | None:
    if not value or value != value.strip() or "\\" in value or "%" in value:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname
    if (
        hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.scheme not in {"https", "http"}
        or (parsed.scheme == "http" and hostname not in {"127.0.0.1", "::1", "localhost"})
        or hostname.endswith(".")
    ):
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname.casefold()
    authority = f"{parsed.scheme}://{host}"
    if port is not None:
        authority = f"{authority}:{port}"
    return authority if value.rstrip("/").casefold() == authority.casefold() else None


def _factory_v3_authority_origins(
    environment: Mapping[str, str],
    binding: FactoryV3LiveAuthorityBinding | None,
) -> tuple[str, str] | None:
    if binding is None:
        return None
    if any(
        environment.get(name, "").strip()
        for name in (
            "CAPTAIN_FACTORY_V3_GATEWAY_HEALTH_URL",
            "CAPTAIN_FACTORY_V3_MINIBOOK_HEALTH_URL",
        )
    ):
        return None
    gateway_claim = environment.get("CAPTAIN_FACTORY_V3_GATEWAY_ORIGIN", "").strip()
    minibook_claim = environment.get("CAPTAIN_FACTORY_V3_MINIBOOK_ORIGIN", "").strip()
    if (
        gateway_claim and gateway_claim != binding.gateway.origin
    ) or (
        minibook_claim and minibook_claim != binding.minibook.origin
    ):
        return None
    return binding.gateway.origin, binding.minibook.origin


def _factory_v3_service_health_payload_ready(
    payload: Mapping[str, object],
    *,
    binding: FactoryV3ServiceBinding,
) -> bool:
    capabilities = payload.get("capabilities")
    return bool(
        set(payload) == {
            "schema",
            "service",
            "version",
            "origin",
            "capabilities",
            "credential_audience",
            "ready",
        }
        and payload.get("schema") == "captain.factory-v3-service-health.v1"
        and payload.get("service") == binding.service
        and payload.get("version") == binding.version
        and payload.get("origin") == binding.origin
        and isinstance(capabilities, (list, tuple))
        and tuple(capabilities) == binding.capabilities
        and payload.get("credential_audience") == binding.credential_audience
        and payload.get("ready") is True
    )


def _bounded_service_health_ready(
    probe: _FactoryV3LiveReadOnlyProbe,
    *,
    binding: FactoryV3ServiceBinding,
    token: str,
) -> bool:
    if not token or _canonical_service_origin(binding.origin) != binding.origin:
        return False
    try:
        payload = probe.service_health(
            service=binding.service,
            origin=binding.origin,
            token=token,
        )
    except Exception:
        return False
    return _factory_v3_service_health_payload_ready(
        payload,
        binding=binding,
    )


def _isolated_factory_v3_roots_ready(*roots: Path) -> bool:
    resolved: list[Path] = []
    try:
        for root in roots:
            if root.is_symlink() or not root.is_dir():
                return False
            resolved.append(root.resolve(strict=True))
    except OSError:
        return False
    if len(set(resolved)) != len(resolved):
        return False
    return not any(
        first in second.parents or second in first.parents
        for index, first in enumerate(resolved)
        for second in resolved[index + 1 :]
    )


def _factory_v3_released_skill_identity(
    environment: Mapping[str, str],
    binding: FactoryV3LiveAuthorityBinding,
) -> tuple[str, str] | None:
    name_claim = environment.get(
        "CAPTAIN_FACTORY_V3_RELEASED_SKILL_NAME", ""
    ).strip()
    digest_claim = environment.get(
        "CAPTAIN_FACTORY_V3_RELEASED_SKILL_SHA256", ""
    ).strip()
    if (
        name_claim and name_claim != binding.released_skill.name
    ) or (
        digest_claim and digest_claim != binding.released_skill.sha256
    ):
        return None
    return binding.released_skill.name, binding.released_skill.sha256


def _factory_v3_released_skill_ready(
    environment: Mapping[str, str],
    probe: _FactoryV3LiveReadOnlyProbe,
    *,
    gateway_origin: str,
    binding: FactoryV3LiveAuthorityBinding,
) -> bool:
    identity = _factory_v3_released_skill_identity(environment, binding)
    token = environment.get("CAPTAIN_FACTORY_V3_CAPTAIN_TOKEN", "").strip()
    if identity is None or not token:
        return False
    name, digest = identity
    try:
        payload = probe.released_skill_active(
            gateway_origin=gateway_origin,
            token=token,
            skill_name=name,
            skill_sha256=digest,
        )
    except Exception:
        return False
    return bool(
        set(payload) == {
            "schema",
            "authority",
            "gateway_origin",
            "skill_name",
            "skill_sha256",
            "authority_binding_sha256",
            "status",
        }
        and payload.get("schema")
        == "captain.factory-v3-released-skill-authority.v1"
        and payload.get("authority") == "captain-gateway"
        and payload.get("gateway_origin") == binding.gateway.origin
        and payload.get("skill_name") == binding.released_skill.name
        and payload.get("skill_sha256") == binding.released_skill.sha256
        and payload.get("authority_binding_sha256") == binding.sha256
        and payload.get("status") == "active"
    )


def _factory_v3_local_runtime_ready(
    environment: Mapping[str, str],
    repository_root: Path,
    binding: FactoryV3LiveAuthorityBinding,
) -> bool:
    values = tuple(
        environment.get(name, "").strip()
        for name in (
            "CAPTAIN_CODEX_EXECUTABLE",
            "CAPTAIN_PWSH_EXECUTABLE",
            "CAPTAIN_CODEX_HOME",
            "CAPTAIN_FACTORY_V3_HERMES_PYTHON",
            "CAPTAIN_FACTORY_V3_HERMES_MODULE_ROOT",
            "CAPTAIN_FACTORY_V3_RELEASED_SKILL_ROOT",
        )
    )
    if not all(values):
        return False
    return verify_factory_v3_local_runtime(
        binding=binding,
        repository_root=repository_root,
        codex_path=Path(values[0]),
        pwsh_path=Path(values[1]),
        codex_home=Path(values[2]),
        hermes_python_path=Path(values[3]),
        hermes_runtime_root=Path(values[4]),
        released_skill_root=Path(values[5]),
    )


def _factory_v3_execution_bounds_ready(
    environment: Mapping[str, str],
    now: datetime,
) -> bool:
    try:
        maximum_runtime = int(
            environment.get("CAPTAIN_FACTORY_V3_MAX_WALL_CLOCK_SECONDS", "")
        )
        maximum_files = int(environment.get("CAPTAIN_FACTORY_V3_MAX_FILES", ""))
        maximum_cost = Decimal(
            environment.get("CAPTAIN_FACTORY_V3_MAX_PROVIDER_COST_USD", "")
        )
        deadline = datetime.fromisoformat(
            environment.get("CAPTAIN_FACTORY_V3_DEADLINE_AT", "").replace(
                "Z", "+00:00"
            )
        )
    except (ArithmeticError, ValueError):
        return False
    if deadline.tzinfo is None or deadline.utcoffset() != timezone.utc.utcoffset(deadline):
        return False
    remaining_seconds = (deadline - now).total_seconds()
    return bool(
        maximum_runtime == 300
        and maximum_files == 4_096
        and maximum_cost.is_finite()
        and maximum_cost == Decimal("1.50")
        and 1 <= remaining_seconds <= maximum_runtime
        and environment.get("CAPTAIN_FACTORY_V3_NETWORK_ALLOWED", "") == "false"
    )


def _factory_v3_credentials_ready(environment: Mapping[str, str]) -> bool:
    values = tuple(
        environment.get(name, "").strip()
        for name in (
            "CAPTAIN_FACTORY_V3_CAPTAIN_TOKEN",
            "CAPTAIN_FACTORY_V3_WORKER_TOKEN",
            "CAPTAIN_FACTORY_V3_CREATION_API_KEY",
        )
    )
    resume_verification_key = environment.get(
        "CAPTAIN_FACTORY_V3_RESUME_VERIFY_KEY", ""
    ).strip()
    if resume_verification_key:
        values = (*values, resume_verification_key)
    if not all(values):
        return False
    encoded = tuple(value.encode("utf-8") for value in values)
    return not any(
        hmac.compare_digest(left, right)
        for index, left in enumerate(encoded)
        for right in encoded[index + 1 :]
    )


def _factory_v3_resume_canonical_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _factory_v3_parse_resume_metadata(
    payload: Mapping[str, object],
    *,
    gateway_origin: str,
    run_id: UUID,
    now: datetime,
    verification_key: str,
) -> FactoryV3LiveResumeMetadata | None:
    expected_fields = {
        "schema",
        "run_id",
        "correlation_id",
        "factory_job_id",
        "creation_job_id",
        "request_id",
        "expected_subject_version",
        "safe_checkpoint_ref",
        "safe_checkpoint_sha256",
        "deadline_at",
        "tenant_id",
        "action_url",
        "gateway_origin",
        "issued_at",
        "status",
        "metadata_sha256",
        "captain_signature",
    }
    if set(payload) != expected_fields or len(verification_key) < 16:
        return None
    unsigned = {
        key: value
        for key, value in payload.items()
        if key not in {"metadata_sha256", "captain_signature"}
    }
    try:
        canonical = _factory_v3_resume_canonical_bytes(unsigned)
        metadata_sha256 = str(payload["metadata_sha256"])
        captain_signature = str(payload["captain_signature"])
        expected_digest = hashlib.sha256(canonical).hexdigest()
        expected_signature = hmac.new(
            verification_key.encode("utf-8"),
            canonical,
            hashlib.sha256,
        ).hexdigest()
        if (
            _SAFE_DIGEST.fullmatch(metadata_sha256) is None
            or _SAFE_DIGEST.fullmatch(captain_signature) is None
            or not hmac.compare_digest(metadata_sha256, expected_digest)
            or not hmac.compare_digest(captain_signature, expected_signature)
        ):
            return None
        parsed_run_id = UUID(str(payload["run_id"]))
        correlation_id = UUID(str(payload["correlation_id"]))
        factory_job_id = UUID(str(payload["factory_job_id"]))
        creation_job_id = UUID(str(payload["creation_job_id"]))
        request_id = UUID(str(payload["request_id"]))
        expected_subject_version = payload["expected_subject_version"]
        if (
            not isinstance(expected_subject_version, int)
            or isinstance(expected_subject_version, bool)
            or expected_subject_version < 1
        ):
            return None
        checkpoint_ref = ArtifactRef.model_validate(payload["safe_checkpoint_ref"])
        checkpoint_sha256 = str(payload["safe_checkpoint_sha256"])
        deadline_text = str(payload["deadline_at"])
        issued_text = str(payload["issued_at"])
        if not deadline_text.endswith("Z") or not issued_text.endswith("Z"):
            return None
        deadline = datetime.fromisoformat(deadline_text.replace("Z", "+00:00"))
        issued_at = datetime.fromisoformat(issued_text.replace("Z", "+00:00"))
        tenant_id = str(payload["tenant_id"])
        action_url = str(payload["action_url"])
        payload_origin = str(payload["gateway_origin"])
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
    expected_action_url = f"{gateway_origin}/portal/factory-actions/{request_id}"
    if (
        payload.get("schema") != "captain.factory-v3-resume-metadata.v1"
        or payload.get("status") not in {"pending", "answered"}
        or parsed_run_id != run_id
        or payload_origin != gateway_origin
        or action_url != expected_action_url
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tenant_id) is None
        or checkpoint_sha256 != checkpoint_ref.sha256
        or _SAFE_DIGEST.fullmatch(checkpoint_sha256) is None
        or checkpoint_ref.media_type != "application/json"
        or deadline.tzinfo is None
        or deadline.utcoffset() != timezone.utc.utcoffset(deadline)
        or issued_at.tzinfo is None
        or issued_at.utcoffset() != timezone.utc.utcoffset(issued_at)
        or issued_at > now
        or deadline <= now
    ):
        return None
    return FactoryV3LiveResumeMetadata(
        run_id=parsed_run_id,
        correlation_id=correlation_id,
        factory_job_id=factory_job_id,
        creation_job_id=creation_job_id,
        request_id=request_id,
        expected_subject_version=expected_subject_version,
        safe_checkpoint_ref=checkpoint_ref,
        safe_checkpoint_sha256=checkpoint_sha256,
        deadline_at=deadline,
        tenant_id=tenant_id,
        action_url=action_url,
        gateway_origin=gateway_origin,
        issued_at=issued_at,
        metadata_sha256=metadata_sha256,
        captain_signature=captain_signature,
        status=cast(Literal["pending", "answered"], payload["status"]),
    )


def _factory_v3_load_resume_metadata(
    path: Path,
    *,
    gateway_origin: str,
    run_id: UUID,
    now: datetime,
    verification_key: str,
) -> FactoryV3LiveResumeMetadata | None:
    try:
        if path.is_symlink() or path.name != "resume-metadata.json":
            return None
        content = path.read_bytes()
        if len(content) > 32_768:
            return None
        payload = json.loads(content)
        if not isinstance(payload, dict):
            return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _factory_v3_parse_resume_metadata(
        payload,
        gateway_origin=gateway_origin,
        run_id=run_id,
        now=now,
        verification_key=verification_key,
    )


def _factory_v3_authoritative_resume_metadata(
    *,
    environment: Mapping[str, str],
    gateway_origin: str,
    run_id: UUID,
    now: datetime,
    probe: _FactoryV3LiveReadOnlyProbe,
    persisted_path: Path | None,
) -> FactoryV3LiveResumeMetadata | None:
    token = environment.get("CAPTAIN_FACTORY_V3_CAPTAIN_TOKEN", "").strip()
    verification_key = environment.get(
        "CAPTAIN_FACTORY_V3_RESUME_VERIFY_KEY", ""
    ).strip()
    if not token or len(verification_key) < 16:
        return None
    persisted = None
    if persisted_path is not None:
        persisted = _factory_v3_load_resume_metadata(
            persisted_path,
            gateway_origin=gateway_origin,
            run_id=run_id,
            now=now,
            verification_key=verification_key,
        )
        if persisted is None:
            return None
    try:
        payload = probe.action_resume_metadata(
            gateway_origin=gateway_origin,
            token=token,
            run_id=run_id,
        )
    except Exception:
        return None
    if payload is None:
        return None
    current = _factory_v3_parse_resume_metadata(
        payload,
        gateway_origin=gateway_origin,
        run_id=run_id,
        now=now,
        verification_key=verification_key,
    )
    if current is None:
        return None
    if persisted is None:
        return current if current.status == "pending" else None
    if (
        persisted.status != "pending"
        or current.status != "answered"
        or current.immutable_binding() != persisted.immutable_binding()
    ):
        return None
    return current


def _factory_v3_portal_configuration_ready(
    environment: Mapping[str, str],
) -> bool:
    if any(
        environment.get(name, "").strip()
        for name in (
            "CAPTAIN_FACTORY_V3_ACTION_INBOX_URL",
            "CAPTAIN_FACTORY_V3_ACTION_REQUEST_ID",
            "CAPTAIN_FACTORY_V3_SAFE_CHECKPOINT_SHA256",
        )
    ):
        return False
    credential_ref = environment.get(
        "CAPTAIN_FACTORY_V3_CREDENTIAL_REF", ""
    ).strip()
    resume_key = environment.get(
        "CAPTAIN_FACTORY_V3_RESUME_VERIFY_KEY", ""
    ).strip()
    return bool(
        _SAFE_CREDENTIAL_REF.fullmatch(credential_ref) is not None
        or len(resume_key) >= 16
    )


def _factory_v3_frozen_input_sha256(
    repository_root: Path,
    input_path: Path,
) -> str | None:
    try:
        manifest_digest = hashlib.sha256(
            json.dumps(
                _FACTORY_V3_AUTHORITY_MANIFEST,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(
            manifest_digest,
            _FACTORY_V3_AUTHORITY_MANIFEST_SHA256,
        ):
            return None
        root = repository_root.resolve(strict=True)
        expected_path = (root / _FACTORY_V3_LIVE_INPUT).resolve(strict=True)
        actual_path = input_path.resolve(strict=True)
        if actual_path != expected_path or input_path.is_symlink():
            return None
        content = actual_path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(digest, _FACTORY_V3_FROZEN_INPUT_SHA256):
            return None
        payload = json.loads(content)
        if (
            not isinstance(payload, dict)
            or payload.get("expected")
            != _FACTORY_V3_AUTHORITY_MANIFEST["fixture_expected"]
        ):
            return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return digest


def _factory_v3_live_runtime_enforcement_ready() -> bool:
    # The current live composition returns PowerShellCodexRunner. Its Codex
    # sandbox and post-run receipts do not enforce exact writable roots, exact
    # build argv, and the provider-cost cap. Environment claims are not proof.
    return False


def preflight_factory_v3_live(
    *,
    environment: Mapping[str, str],
    repository_root: Path,
    input_path: Path,
    workspace_root: Path,
    cas_root: Path,
    state_root: Path,
    run_id: UUID,
    resume_metadata_path: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    _probe: _FactoryV3LiveReadOnlyProbe | None = None,
) -> FactoryV3LivePreflightReport:
    """Read live prerequisites without starting a worker or mutating services."""

    current_time = (clock or (lambda: datetime.now(timezone.utc)))()
    if (
        current_time.tzinfo is None
        or current_time.utcoffset() != timezone.utc.utcoffset(current_time)
    ):
        raise ValueError("Factory V3 live preflight clock must be UTC")
    probe = _probe or _DefaultFactoryV3LiveReadOnlyProbe()
    input_sha256 = _factory_v3_frozen_input_sha256(repository_root, input_path)
    authority_binding = load_captain_factory_v3_live_authority(repository_root)
    origins = _factory_v3_authority_origins(environment, authority_binding)
    live_opt_in = environment.get("CAPTAIN_FACTORY_V3_LIVE", "") == "1"
    isolated_roots = _isolated_factory_v3_roots_ready(
        workspace_root,
        cas_root,
        state_root,
    )
    credentials = bool(
        authority_binding is not None
        and _factory_v3_credentials_ready(environment)
    )
    execution_bounds = _factory_v3_execution_bounds_ready(environment, current_time)
    portal_configuration = _factory_v3_portal_configuration_ready(environment)
    runtime_enforcement = _factory_v3_live_runtime_enforcement_ready()

    # Stage 1 is entirely local. In particular, no token-bearing read is
    # attempted while opt-in, origin, bounds, isolation, credentials, Portal,
    # or the Captain-owned runtime enforcement gate is absent.
    stage_one_ready = all(
        (
            input_sha256 is not None,
            authority_binding is not None,
            live_opt_in,
            origins is not None,
            isolated_roots,
            credentials,
            execution_bounds,
            portal_configuration,
            runtime_enforcement,
        )
    )
    codex_session = False
    hermes_runtime = False
    released_skill_identity = False
    if stage_one_ready and authority_binding is not None:
        local_runtime = _factory_v3_local_runtime_ready(
            environment,
            repository_root,
            authority_binding,
        )
        codex_session = local_runtime
        hermes_runtime = local_runtime
        released_skill_identity = (
            _factory_v3_released_skill_identity(
                environment,
                authority_binding,
            )
            is not None
        )

    stage_two_ready = bool(
        stage_one_ready
        and codex_session
        and hermes_runtime
        and released_skill_identity
    )
    gateway_reachable = False
    minibook_reachable = False
    released_skill_authority = False
    resume_metadata: FactoryV3LiveResumeMetadata | None = None
    action_inbox: FactoryV3LiveActionInboxStatus | None = None
    credential_ref = environment.get(
        "CAPTAIN_FACTORY_V3_CREDENTIAL_REF", ""
    ).strip()
    portal_prerequisites = bool(
        portal_configuration
        and _SAFE_CREDENTIAL_REF.fullmatch(credential_ref) is not None
        and resume_metadata_path is None
    )

    # Stage 3 is the only token-bearing stage. Origins are canonical authorities
    # and health paths are derived by the probe; callers cannot inject a URL.
    if (
        stage_two_ready
        and origins is not None
        and authority_binding is not None
    ):
        gateway_origin, minibook_origin = origins
        gateway_reachable = _bounded_service_health_ready(
            probe,
            binding=authority_binding.gateway,
            token=environment.get("CAPTAIN_FACTORY_V3_CAPTAIN_TOKEN", "").strip(),
        )
        minibook_reachable = _bounded_service_health_ready(
            probe,
            binding=authority_binding.minibook,
            token=environment.get("CAPTAIN_FACTORY_V3_WORKER_TOKEN", "").strip(),
        )
        if gateway_reachable and minibook_reachable:
            released_skill_authority = _factory_v3_released_skill_ready(
                environment,
                probe,
                gateway_origin=gateway_origin,
                binding=authority_binding,
            )
        if released_skill_authority and (
            resume_metadata_path is not None
            or _SAFE_CREDENTIAL_REF.fullmatch(credential_ref) is None
        ):
            resume_metadata = _factory_v3_authoritative_resume_metadata(
                environment=environment,
                gateway_origin=gateway_origin,
                run_id=run_id,
                now=current_time,
                probe=probe,
                persisted_path=resume_metadata_path,
            )
            if resume_metadata is not None:
                if resume_metadata.status == "pending":
                    action_inbox = FactoryV3LiveActionInboxStatus(
                        status="pending",
                        url=resume_metadata.action_url,
                        request_id=resume_metadata.request_id,
                        safe_checkpoint_sha256=resume_metadata.safe_checkpoint_sha256,
                    )
                portal_prerequisites = True

    checks = (
        _factory_v3_check("frozen_input", input_sha256 is not None),
        _factory_v3_check(
            "signed_authority_binding",
            authority_binding is not None,
        ),
        _factory_v3_check("live_opt_in", live_opt_in),
        _factory_v3_check("authority_origins", origins is not None),
        _factory_v3_check("gateway_reachability", gateway_reachable),
        _factory_v3_check("minibook_reachability", minibook_reachable),
        _factory_v3_check("released_skill_authority", released_skill_authority),
        _factory_v3_check("hermes_runtime", hermes_runtime),
        _factory_v3_check("codex_session", codex_session),
        _factory_v3_check("isolated_roots", isolated_roots),
        _factory_v3_check("credential_separation", credentials),
        _factory_v3_check("execution_bounds", execution_bounds),
        _factory_v3_check("portal_prerequisites", portal_prerequisites),
        _factory_v3_check("runtime_enforcement", runtime_enforcement),
    )
    unmet = tuple(check for check in checks if check.status == "unmet")
    status: Literal["ready", "awaiting_user_input", "unmet_preflight"]
    if unmet:
        status = "unmet_preflight"
    elif action_inbox is not None:
        status = "awaiting_user_input"
    else:
        status = "ready"
    return FactoryV3LivePreflightReport(
        run_id=run_id,
        checked_at=current_time,
        status=status,
        input_sha256=input_sha256 or "0" * 64,
        authority_binding_sha256=(
            authority_binding.sha256 if authority_binding is not None else "0" * 64
        ),
        active_released_skill_sha256=(
            authority_binding.released_skill.sha256
            if authority_binding is not None and released_skill_authority
            else "0" * 64
        ),
        checks=checks,
        action_inbox=action_inbox,
        resume_metadata=resume_metadata,
    )


def _factory_v3_live_preflight_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the read-only Captain Factory V3 live preflight."
    )
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--cas-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--run-id", type=UUID, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume-metadata", type=Path)
    return parser


def factory_v3_live_preflight_main(
    argv: list[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    args = _factory_v3_live_preflight_parser().parse_args(argv)
    report = preflight_factory_v3_live(
        environment=os.environ if environment is None else environment,
        repository_root=args.repository_root,
        input_path=args.input,
        workspace_root=args.workspace_root,
        cas_root=args.cas_root,
        state_root=args.state_root,
        run_id=args.run_id,
        resume_metadata_path=args.resume_metadata,
        clock=clock,
    )
    output = args.output.resolve()
    run_root = args.state_root.resolve().parent
    try:
        output.relative_to(run_root)
    except ValueError as exc:
        raise ValueError("Factory V3 preflight report escaped its run root") from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    if report.resume_metadata is not None and args.resume_metadata is None:
        resume_output = run_root / "resume-metadata.json"
        encoded_resume = (
            json.dumps(
                report.resume_metadata.as_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        )
        if resume_output.exists():
            if resume_output.is_symlink() or resume_output.read_text(
                encoding="utf-8"
            ) != encoded_resume:
                raise ValueError("Factory V3 resume metadata changed")
        else:
            resume_output.write_text(encoded_resume, encoding="utf-8")
    summary: dict[str, object] = {
        "schema": _FACTORY_V3_LIVE_SUMMARY_SCHEMA,
        "run_id": str(report.run_id),
        "status": report.status,
        "unmet_checks": [
            check.code for check in report.checks if check.status == "unmet"
        ],
    }
    if report.action_inbox is not None:
        summary["action_inbox"] = report.action_inbox.as_dict()
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    if report.ready:
        return 0
    return 4 if report.status == "awaiting_user_input" else 3


class _CaptainV3CreationAuthorityBuilder:
    """Reconstruct exact V3 mapping inputs from Captain ledger and private CAS."""

    def __init__(
        self,
        *,
        repository: GatewayFactoryRepository,
        artifacts: BusinessBenchmarkContentAddressedArtifactStore,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts

    def __call__(self, request: FactoryDispatch) -> CaptainCreationJobV3Authority:
        if not isinstance(request.job, AgentFactoryJobV3) or request.lease is None:
            raise FactoryDispatchError("V3 authority requires an active Captain dispatch")
        job = request.job
        attempt = request.action.attempt
        blocks = tuple(
            block
            for block in self._repository.blocks(job.job_id)
            if block.job_id == job.job_id
            and block.correlation_id == job.correlation_id
            and block.subject_version == job.subject_version
            and block.attempt == attempt
            and block.status.value == "succeeded"
        )
        if not any(block.phase.value == "forge_requested" for block in blocks):
            raise FactoryDispatchError("V3 authority is missing the durable capability miss")

        def unique_ref(phase: str) -> ArtifactRef:
            candidates = tuple(
                block.artifact_refs[0]
                for block in blocks
                if block.phase.value == phase and len(block.artifact_refs) == 1
            )
            if len(candidates) != 1 or candidates[0].media_type != "application/json":
                raise FactoryDispatchError(f"V3 authority requires one durable {phase} artifact")
            return candidates[0]

        blueprint_ref = unique_ref("blueprint_created")
        tool_plan_ref = unique_ref("tool_candidate_tested")
        try:
            compiled_bytes = self._artifacts.read_bytes(job.compiled_spec_ref)
            compiled = json.loads(compiled_bytes)
            if not isinstance(compiled, dict):
                raise ValueError
            required_agent_ids = tuple(compiled["required_agent_ids"])
            ordered_handoffs = tuple(compiled["ordered_handoffs"])
            tool_requirements = tuple(compiled["tool_requirements"])
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FactoryDispatchError("V3 compiled topology authority is unavailable") from exc
        lease_bytes = json.dumps(
            request.lease.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        lease_ref = self._artifacts.put(
            lease_bytes, "application/json", namespace="factory-v3-lease"
        )
        workspace_policy_ref = self._artifacts.put(
            json.dumps(
                {
                    "schema": "captain.factory-v3-workspace-policy.v1",
                    "workspace_ref": request.lease.workspace_ref,
                    "execution_policy": job.execution_policy.model_dump(mode="json", by_alias=True),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            "application/json",
            namespace="factory-v3-workspace-policy",
        )
        creation_material = "\x1f".join(
            (
                job.required_capability,
                str(job.subject_version),
                job.input_ref.sha256,
                job.compiled_spec_ref.sha256,
                job.dependency_graph_ref.sha256,
                *job.acceptance_assertion_ids,
            )
        )
        return CaptainCreationJobV3Authority(
            command_id=job.event_id,
            capability_resolution=CapabilityResolution(
                kind="create",
                creation_key="factory-create-"
                + hashlib.sha256(creation_material.encode("utf-8")).hexdigest(),
            ),
            compiled_spec_ref=job.compiled_spec_ref,
            lease_ref=lease_ref,
            blueprint_ref=blueprint_ref,
            tool_plan_ref=tool_plan_ref,
            required_agent_ids=required_agent_ids,
            ordered_handoffs=ordered_handoffs,
            tool_requirements=tool_requirements,
            workspace_policy_ref=workspace_policy_ref,
            clarification_policy={
                "allowed_request_types": (
                    "clarification",
                    "credential_required",
                    "external_api_approval",
                    "n8n_approval",
                ),
                "max_requests": 2,
            },
        )


class _CaptainDurableV3WorkerClient:
    """Record Minibook assignment/effect authority before restart-sensitive use."""

    def __init__(
        self,
        *,
        delegate: AuthenticatedMinibookV3WorkerClient,
        authority: FilesystemCaptainCreationJobV3Store,
    ) -> None:
        self._delegate = delegate
        self._authority = authority

    async def next_assignment(self, scope=None) -> FactoryBuildAssignmentV2 | None:
        assignment = await self._delegate.next_assignment(scope)
        if assignment is not None:
            self._authority.record_assignment(assignment)
        return assignment

    async def claim(
        self, assignment_id: UUID, worker_id: str
    ) -> FactoryBuildAssignmentV2:
        assignment = await self._delegate.claim(assignment_id, worker_id)
        return self._authority.record_assignment(assignment)

    async def accepted_effect(
        self, assignment_id: UUID
    ) -> FactoryBuildEffectV2 | None:
        effect = await self._delegate.accepted_effect(assignment_id)
        if effect is not None:
            self._authority.record_effect(effect)
        return effect

    async def submit_effect(self, effect: FactoryBuildEffectV2) -> FactoryBuildEffectV2:
        accepted = await self._delegate.submit_effect(effect)
        return self._authority.record_effect(accepted)


@dataclass(frozen=True)
class FactoryLiveOperatorSettings:
    workspace_root: Path
    python_executable: Path
    hermes_python_executable: Path
    test_mariadb_dsn: str
    job_ids: tuple[UUID, ...]
    hermes_provider: str
    hermes_model: str
    hermes_maximum_total_cost_usd: Decimal
    hermes_reasoning_effort: str = "high"
    maximum_dispatches: int = 12
    stop_before_quality_warden: bool = False
    factory_v3_enabled: bool = False

    def __post_init__(self) -> None:
        assert_local_captain_test_dsn(self.test_mariadb_dsn)
        workspace = self.workspace_root.resolve()
        python = self.python_executable.resolve()
        hermes_python = self.hermes_python_executable.resolve()
        if (
            not workspace.is_dir()
            or not python.is_file()
            or not hermes_python.is_file()
        ):
            raise ValueError("Factory live operator paths are unavailable")
        _assert_hermes_python_runtime(
            hermes_python,
            module_root=workspace / "hermes-agent",
        )
        if len(self.job_ids) not in {1, 2}:
            raise ValueError("Factory live operator requires one or two distinct jobs")
        if len(set(self.job_ids)) != len(self.job_ids):
            raise ValueError("Factory live operator requires distinct jobs")
        if (
            self.hermes_provider not in {"openai-api", "custom"}
            or not self.hermes_model.strip()
            or self.hermes_reasoning_effort != "high"
            or (
                self.hermes_provider == "custom"
                and self.hermes_model != "captain-hermes:8b"
            )
            or not self.hermes_maximum_total_cost_usd.is_finite()
            or self.hermes_maximum_total_cost_usd <= 0
            or self.hermes_maximum_total_cost_usd > Decimal("1.50")
        ):
            raise ValueError("Factory Hermes pin or cost allocation is invalid")
        if self.maximum_dispatches < 1 or self.maximum_dispatches > 24:
            raise ValueError("Factory live dispatch bound is invalid")


class _FactoryInputMaterializer:
    """Route only Captain-owned artifacts to their exact verified CAS."""

    def __init__(
        self,
        *,
        benchmark_artifacts: BusinessBenchmarkContentAddressedArtifactStore,
        codex_build_artifacts: CodexBuildArtifactCas,
    ) -> None:
        self._benchmark_artifacts = benchmark_artifacts
        self._codex_build_artifacts = codex_build_artifacts

    def materialize(self, reference: ArtifactRef) -> Path:
        if reference.uri.startswith("artifact://business-benchmark-production/"):
            self._benchmark_artifacts.read_bytes(reference)
            return self._benchmark_artifacts.local_path(reference)
        if reference.uri.startswith("artifact://captain-codex-build/"):
            self._codex_build_artifacts.read_bytes(reference)
            return self._codex_build_artifacts.local_path(reference)
        raise FactoryDispatchError("factory input artifact owner is unsupported")


class _LazyProductionBenchmarkInputs:
    """Create the provider/n8n graph only when Quality Warden is dispatched."""

    def __init__(
        self,
        *,
        settings: Mapping[UUID, LiveBusinessBenchmarkSettings],
        loader: GatewayBusinessBenchmarkLiveCompositionLoader,
    ) -> None:
        self._settings = dict(settings)
        self._loader = loader
        self._compositions: dict[UUID, _DispatchInputComposition] = {}

    def resolve(self, request: FactoryDispatch) -> BusinessBenchmarkDispatchInputs | None:
        selected = self._settings.get(request.job.job_id)
        if selected is None:
            return None
        composition = self._compositions.get(request.job.job_id)
        if composition is None:
            raw = self._loader(selected)
            dispatch_inputs = getattr(raw, "dispatch_inputs", None)
            if not callable(dispatch_inputs):
                raise ValueError(
                    "production benchmark composition lacks dispatch inputs"
                )
            composition = raw  # type: ignore[assignment]
            self._compositions[request.job.job_id] = composition
        return composition.dispatch_inputs(selected, request)


def _canonical_business_benchmark_repository(
    *,
    authority_root: Path,
    seed_version_id: str,
) -> CaptainCanonicalSuiteRepository:
    return CaptainCanonicalSuiteRepository(
        CaptainCanonicalSuiteAuthority(
            root=authority_root / "suites",
            seed_version_id=seed_version_id,
        ),
        FilesystemBusinessBenchmarkEvidenceStore(
            authority_root / "runtime-state" / "benchmark-receipts"
        ),
    )


def _suite_scoped_hermes_evidence_root(
    *,
    authority_root: Path,
    seed_version_id: str,
) -> Path:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", seed_version_id) is None:
        raise ValueError("Hermes evidence scope requires a safe suite identifier")
    return (
        authority_root.resolve()
        / "runtime-state"
        / f"hermes-evidence-{seed_version_id}"
    )


def compose_business_demo_factory_operator(
    settings: FactoryLiveOperatorSettings,
    *,
    environment: Mapping[str, str],
    n8n_client: httpx.AsyncClient,
    clock: Callable[[], datetime] | None = None,
    hermes_factory_worker: HermesFactoryWorker | None = None,
    minibook_client: httpx.AsyncClient | None = None,
) -> AgentFactoryLiveComposition:
    """Compose every real port without dispatching Hermes, Forge, n8n, or OpenAI."""

    if settings.factory_v3_enabled and not _factory_v3_live_runtime_enforcement_ready():
        raise ValueError("Factory V3 live runtime enforcement is unavailable")
    current_time = clock or (lambda: datetime.now(timezone.utc))
    if (settings.factory_v3_enabled and minibook_client is None) or (
        not settings.factory_v3_enabled
        and (hermes_factory_worker is not None or minibook_client is not None)
    ):
        raise ValueError("Factory V3 worker configuration is incomplete")
    workspace = settings.workspace_root.resolve()
    authority_root = (
        workspace / ".captain-cook" / "private" / "business-benchmarks"
    ).resolve()
    configured_root = Path(
        _required(environment, "CAPTAIN_BENCHMARK_AUTHORITY_ROOT")
    ).resolve()
    if configured_root != authority_root:
        raise ValueError("Factory operator benchmark authority root does not match")
    aggregate = LiveBusinessBenchmarkSettings.from_environment(
        environment,
        repository_root=workspace,
    )
    if {item.job_id for item in aggregate.selections} != set(settings.job_ids):
        raise ValueError("Factory operator selections do not match the requested jobs")
    if (
        environment.get("CAPTAIN_FACTORY_HERMES_PROVIDER")
        != settings.hermes_provider
        or environment.get("CAPTAIN_FACTORY_HERMES_MODEL")
        != settings.hermes_model
    ):
        raise ValueError("Factory Hermes route does not match operator settings")
    validate_factory_total_cost_envelope(
        environment=environment,
        benchmark_maximum_usd_per_team=tuple(
            item.maximum_usd for item in aggregate.selections
        ),
        benchmark_profiles=tuple(item.profile for item in aggregate.selections),
    )
    if not environment.get("OPENAI_API_KEY", "").strip():
        raise ValueError("Factory provider secret is not present in the process")

    store = GatewayStore(MariaDBStorage(settings.test_mariadb_dsn))
    budget_ledger = GatewayFactoryBudgetLedger(store)
    codex_state_root = (authority_root / "runtime-state" / "codex").resolve()
    runtime_retry_authority = FilesystemFactoryRuntimeRetryAuthority(
        authority_root=(
            authority_root / "runtime-state" / "runtime-retry-authorizations"
        ),
        checkpoint_root=codex_state_root / "checkpoints",
    )
    improvement_authority = FilesystemFactoryImprovementAuthority(
        authority_root / "runtime-state" / "improvement-authorizations"
    )
    hermes_retry_authority = FilesystemFactoryHermesRetryAuthority(
        authority_root / "runtime-state" / "hermes-retry-authorizations"
    )
    repository = GatewayFactoryRepository(
        store,
        runtime_retries=runtime_retry_authority,
    )
    creation_artifact_root = (
        workspace / ".captain-cook" / "minibook-creation-cas"
    ).resolve()
    creation_artifacts = GatewayMinibookCreationArtifactStore(
        creation_artifact_root
    )
    v3_authority_store = (
        FilesystemCaptainCreationJobV3Store(
            authority_root / "runtime-state" / "factory-v3-authority"
        )
        if settings.factory_v3_enabled
        else None
    )
    candidate_bindings = GatewayForgeCandidateProvider(
        repository=repository,
        artifacts=creation_artifacts,
        v3_jobs=v3_authority_store,
    )
    technical_revalidation_authority = (
        FilesystemFactoryTechnicalRevalidationAuthority(
            authority_root
            / "runtime-state"
            / "technical-revalidation-authorizations",
            repository_root=workspace,
            runtime_paths=TECHNICAL_REVALIDATION_RUNTIME_PATHS,
            evaluator_paths=TECHNICAL_REVALIDATION_EVALUATOR_PATHS,
        )
    )
    def validate_technical_revalidation(request: FactoryDispatch) -> None:
        if not isinstance(request.job, AgentFactoryJobV3):
            raise FactoryDispatchError("technical revalidation requires a V3 job")
        resolved = candidate_bindings.candidate_for(request.job)
        if not isinstance(resolved, ResolvedFactoryCandidate):
            raise FactoryDispatchError("technical revalidation candidate is unavailable")
        technical_revalidation_authority.active(
            job=request.job,
            action=request.action,
            budget=budget_ledger.projection(request.job.job_id),
            now=current_time(),
            code_revision=_git_revision(workspace),
            candidate_ref=resolved.candidate.source_archive_ref,
            holdout_ref=select_technical_business_holdout(request.job),
        )
    factory_evidence_root = (
        authority_root / "runtime-state" / "factory-evidence"
    ).resolve()
    benchmark_cas = BusinessBenchmarkContentAddressedArtifactStore(
        authority_root / "cas"
    )
    legacy_mapper = CaptainCreationJobMapper(
        evidence=CaptainForgeEvidenceBridge(
            repository=repository,
            evidence_store=FilesystemFactoryEvidenceStore(factory_evidence_root),
        )
    )
    mapper = (
        CaptainCreationJobV3Mapper(
            authority=_CaptainV3CreationAuthorityBuilder(
                repository=repository,
                artifacts=benchmark_cas,
            ),
            released_skill=lambda job, step: repository.released_for(job, step),
            store=v3_authority_store,
        )
        if v3_authority_store is not None
        else legacy_mapper
    )
    codex_workspace_root = (
        workspace / ".captain-cook" / "private" / "codex-workspaces"
    ).resolve()
    codex_executable = Path(
        _required(environment, "CAPTAIN_CODEX_EXECUTABLE")
    ).resolve(strict=True)
    pwsh_executable = Path(
        _required(environment, "CAPTAIN_PWSH_EXECUTABLE")
    ).resolve(strict=True)
    codex_home = Path(_required(environment, "CAPTAIN_CODEX_HOME")).resolve(
        strict=True
    )
    codex_session_script = (workspace / "scripts" / "codex-session.ps1").resolve(
        strict=True
    )
    codex_authorizer = CodexExecutionPolicy(
        workspace_root=codex_workspace_root,
        environment=environment,
    )

    def codex_runner_factory(
        *,
        session_id: str,
        state_path: Path,
        journal_path: Path,
        maximum_runtime_seconds: int,
        deadline_at: datetime,
    ) -> PowerShellCodexRunner:
        return PowerShellCodexRunner(
            pwsh_path=pwsh_executable,
            script_path=codex_session_script,
            codex_path=codex_executable,
            session_id=session_id,
            state_path=state_path,
            journal_path=journal_path,
            artifact_references=(),
            codex_home=codex_home,
            deadline_at=deadline_at,
            timeout_seconds=maximum_runtime_seconds,
        )

    codex_build_cas = CodexBuildArtifactCas(
        workspace / ".captain-cook" / "private" / "codex-build-cas"
    )
    codex_cli_settings = CodexCliFactoryBuildSettings(
        state_root=codex_state_root,
        maximum_runtime_seconds=900,
        maximum_files=4_096,
        maximum_provider_cost_usd=float(
            settings.hermes_maximum_total_cost_usd
        ),
        supported_output_roots=("output",),
        supported_commands=(
            ("python", "-m", "pytest"),
            ("pytest",),
            ("python", "-m", "compileall", "output"),
        ),
    )
    codex_receipt_issuer = CaptainCodexBuildReceiptIssuer(codex_build_cas)
    def codex_sealer_for(
        execution_settings: CodexCliFactoryBuildSettings,
    ) -> CaptainCodexBuildSealer:
        return CaptainCodexBuildSealer(
            executor=CodexCliFactoryBuildExecutor(
                settings=execution_settings,
                workspace_preparer=GitDetachedFactoryWorkspacePreparer(
                    repository_root=workspace,
                    workspaces_root=codex_workspace_root,
                ),
                artifact_reader=_FactoryCodexArtifactReader(
                    benchmark_artifacts=benchmark_cas,
                    minibook_creation_artifacts=creation_artifacts,
                ),
                authorizer=codex_authorizer,
                runner_factory=codex_runner_factory,
                checkpoint_store=FilesystemFactoryCodexBuildCheckpointStore(
                    codex_state_root / "checkpoints"
                ),
                scaffold_manifest_store=FilesystemFactoryCodexScaffoldManifestStore(
                    codex_state_root / "scaffold-manifests"
                ),
                sealed_evidence_store=FilesystemFactoryCodexSealedEvidenceStore(
                    codex_state_root / "sealed-evidence"
                ),
                resume_authorizer=CaptainFactoryCodexResumeAuthorizer(
                    clock=current_time
                ),
                process_inspector=PowerShellFactoryCodexProcessInspector(
                    pwsh_path=pwsh_executable,
                    script_path=codex_session_script,
                ),
                clock=current_time,
            ),
            issuer=codex_receipt_issuer,
        )

    codex_sealer = codex_sealer_for(codex_cli_settings)
    if settings.factory_v3_enabled:
        assert minibook_client is not None
        forge = MinibookForgeHttpClient(
            mapper=mapper,
            settings=MinibookForgeHttpSettings(
                base_url=_required(environment, "CAPTAIN_FACTORY_V3_MINIBOOK_URL").rstrip("/"),
                api_key=_required(environment, "CAPTAIN_FACTORY_V3_CREATION_API_KEY"),
            ),
            client=minibook_client,
        )
    else:
        forge = MinibookSwarmForge(
            materializer=_FactoryInputMaterializer(
                benchmark_artifacts=benchmark_cas,
                codex_build_artifacts=codex_build_cas,
            ),
            mapper=legacy_mapper,
            skill_receipts=legacy_mapper,
            settings=MinibookForgeSettings(
                python_executable=str(settings.python_executable.resolve()),
                swarm_script=workspace / "minibook" / "autogen_swarm.py",
                working_directory=workspace,
                max_runtime_seconds=1800,
                artifact_root=creation_artifact_root,
            ),
        )
    technical_ports = GatewayTechnicalTeamExecutionPortsProvider.from_environment(
        environment=environment,
        store=store,
        candidate_bindings=candidate_bindings,
        authority_root=authority_root,
        skill_root=workspace / "agenten" / "agent_factory" / "skills",
        clock=current_time,
    )
    benchmark_loader = GatewayBusinessBenchmarkLiveCompositionLoader(
        environment=environment,
        n8n_client=n8n_client,
        clock=current_time,
        candidate_authority=GatewayForgeBusinessBenchmarkCandidateAuthority(
            candidate_bindings
        ),
    )
    benchmark_settings = {
        item.job_id: aggregate.for_selection(item)
        for item in aggregate.selections
    }
    benchmark_inputs = _LazyProductionBenchmarkInputs(
        settings=benchmark_settings,
        loader=benchmark_loader,
    )
    seed_version_id = _required(
        environment,
        "CAPTAIN_BENCHMARK_SEED_VERSION_ID",
    )
    suite_repository = _canonical_business_benchmark_repository(
        authority_root=authority_root,
        seed_version_id=seed_version_id,
    )
    n8n_work_batches = {
        item.job_id: _required(
            environment,
            "CAPTAIN_BENCHMARK_RENEWAL_BATCH_ID",
        )
        for item in aggregate.selections
        if item.profile == "renewal"
    }
    codex_prompt_store = _BenchmarkCodexPromptArtifactStore(benchmark_cas)
    worker_runtime: HermesFactoryWorkerRuntime | None = None
    if settings.factory_v3_enabled and hermes_factory_worker is None:
        assert minibook_client is not None
        configured_minibook_url = _required(
            environment,
            "CAPTAIN_FACTORY_V3_MINIBOOK_URL",
        ).rstrip("/")
        if str(minibook_client.base_url).rstrip("/") != configured_minibook_url:
            raise ValueError("Factory V3 Minibook client URL does not match")
        worker_id = _required(environment, "CAPTAIN_FACTORY_V3_WORKER_ID")
        runtime_skill_name = _required(
            environment,
            "CAPTAIN_FACTORY_V3_RELEASED_SKILL_NAME",
        )
        runtime_skill_sha256 = _required(
            environment,
            "CAPTAIN_FACTORY_V3_RELEASED_SKILL_SHA256",
        )
        checkpoint_store = FilesystemHermesFactoryWorkerCheckpointStore(
            codex_state_root / "hermes-worker-checkpoints"
        )
        minibook = _CaptainDurableV3WorkerClient(
            delegate=AuthenticatedMinibookV3WorkerClient(
                client=minibook_client,
                worker_token=_required(
                    environment,
                    "CAPTAIN_FACTORY_V3_WORKER_TOKEN",
                ),
                worker_id=worker_id,
                checkpoint_store=checkpoint_store,
            ),
            authority=v3_authority_store,
        )
        leases = GatewayFactoryLeases(store)
        assignment_mapper = CaptainV3CodexRequestMapper(
            job_for=lambda job_id: repository.job(job_id),
            lease_for=lambda job, attempt, now: leases.active(
                job,
                FactoryRole.TOOL_INTEGRATOR,
                attempt,
                now,
            ),
            released_skill_for=lambda job, step: repository.released_for(
                job,
                step,
            ),
            prompt_store=codex_prompt_store,
            artifact_cas=codex_build_cas,
            worker_id=worker_id,
            runtime_skill_sha256=runtime_skill_sha256,
            clock=current_time,
            runtime_retry_for=lambda job, action, now: (
                runtime_retry_authority.active(
                    job,
                    action,
                    store.factory_job(job.job_id),  # type: ignore[arg-type]
                    now,
                )
            ),
        )
        codex_builder = CaptainCodexAssignmentBuilder(
            sealer_for=codex_sealer_for,
            mapper=assignment_mapper,
            settings=codex_cli_settings,
        )
        worker_runtime = HermesFactoryWorkerRuntime(
            minibook=minibook,
            codex=codex_builder,
            checkpoint_store=checkpoint_store,
            codex_receipts=codex_receipt_issuer,
            worker_id=worker_id,
            released_skill_name=runtime_skill_name,
            released_skill_sha256=runtime_skill_sha256,
            retry_authority=codex_builder,
        )
    return compose_agent_factory_live(
        store=store,
        gateway_repository=repository,
        forge=forge,
        candidate_bindings=candidate_bindings,
        team_execution_ports_for=technical_ports,
        business_benchmark_repository=suite_repository,
        business_benchmark_inputs=benchmark_inputs,
        workspace_namespace="business-benchmark-factory-v3",
        evidence_root=factory_evidence_root,
        hermes_settings=HermesCliSettings(
            executable=str(settings.hermes_python_executable.resolve()),
            skill_root=workspace / "agenten" / "agent_factory" / "skills",
            timeout_seconds=900,
            evidence_root=_suite_scoped_hermes_evidence_root(
                authority_root=authority_root,
                seed_version_id=seed_version_id,
            ),
            released_skill_root=(
                workspace / "agenten" / "agent_factory" / "released-skills"
            ),
            module_root=workspace / "hermes-agent",
            working_directory=workspace,
            provider=settings.hermes_provider,
            model=settings.hermes_model,
            reasoning_effort=settings.hermes_reasoning_effort,
            maximum_total_cost_usd=settings.hermes_maximum_total_cost_usd,
            unresolved_effect_reserve_usd=Decimal(
                _required(
                    environment,
                    "CAPTAIN_FACTORY_HERMES_UNRESOLVED_EFFECT_RESERVE_USD",
                )
            ),
        ),
        clock=current_time,
        n8n_work_batches=n8n_work_batches,
        holdout_selector=select_technical_business_holdout,
        codex_build_sealer=codex_sealer,
        codex_prompt_artifact_store=codex_prompt_store,
        improvements=improvement_authority,
        runtime_retries=runtime_retry_authority,
        hermes_retry_authority=hermes_retry_authority,
        technical_revalidation_validator=validate_technical_revalidation,
        factory_v3_enabled=settings.factory_v3_enabled,
        hermes_factory_worker=hermes_factory_worker,
        hermes_factory_worker_runtime=worker_runtime,
    )


async def run_business_demo_factory_jobs(
    settings: FactoryLiveOperatorSettings,
    *,
    environment: Mapping[str, str],
) -> tuple[ProductionFactoryDispatchResult, ...]:
    stop_before_action = (
        FactoryActionKind.DISPATCH_QUALITY_WARDEN
        if settings.stop_before_quality_warden
        else None
    )
    async with AsyncExitStack() as stack:
        n8n_client = await stack.enter_async_context(
            httpx.AsyncClient(timeout=30.0)
        )
        minibook_client = None
        if getattr(settings, "factory_v3_enabled", False):
            minibook_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    timeout=30.0,
                    base_url=_required(
                        environment,
                        "CAPTAIN_FACTORY_V3_MINIBOOK_URL",
                    ),
                )
            )
        composition = compose_business_demo_factory_operator(
            settings,
            environment=environment,
            n8n_client=n8n_client,
            minibook_client=minibook_client,
        )
        results = tuple(
            [
                await composition.run(
                    job_id,
                    maximum_dispatches=settings.maximum_dispatches,
                    stop_before_action=stop_before_action,
                )
                for job_id in settings.job_ids
            ]
        )
    return results


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"required Factory operator setting is missing: {name}")
    return value


def _git_revision(workspace: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    revision = completed.stdout.strip().lower()
    if completed.returncode != 0 or len(revision) != 40 or any(
        character not in "0123456789abcdef" for character in revision
    ):
        raise ValueError("Factory operator Git revision is unavailable")
    return revision


def _assert_hermes_python_runtime(
    python_executable: Path,
    *,
    module_root: Path,
) -> None:
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment["PYTHONPATH"] = str(module_root.resolve())
    environment["CAPTAIN_EXPECTED_HERMES_MODULE_ROOT"] = str(
        module_root.resolve()
    )
    probe = subprocess.run(
        [
            str(python_executable),
            "-c",
            (
                "import os; from pathlib import Path; "
                "import concurrent_log_handler, hermes_cli; "
                "Path(hermes_cli.__file__).resolve().relative_to("
                "Path(os.environ['CAPTAIN_EXPECTED_HERMES_MODULE_ROOT']).resolve())"
            ),
        ],
        cwd=module_root.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        raise ValueError("dedicated Hermes Python runtime is incomplete")


__all__ = [
    "FactoryV3LiveActionInboxStatus",
    "FactoryV3LivePreflightCheck",
    "FactoryV3LivePreflightReport",
    "FactoryV3LiveResumeMetadata",
    "FactoryLiveOperatorSettings",
    "compose_business_demo_factory_operator",
    "factory_v3_live_preflight_main",
    "preflight_factory_v3_live",
    "run_business_demo_factory_jobs",
    "validate_factory_total_cost_envelope",
]


if __name__ == "__main__":
    raise SystemExit(factory_v3_live_preflight_main(sys.argv[1:]))
