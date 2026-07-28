"""Fail-closed production ports for provider-backed business benchmarks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from autogen_core.models import ChatCompletionClient
from autogen_ext.models.openai import OpenAIChatCompletionClient
from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.candidate_evaluation import (
    FactoryCandidateManifest,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.execution_policy import FactoryLiveCapability
from agenten.agent_factory.skill_workflow_contracts import FactorySkillInvocationV1
from agenten.agent_factory.team_execution import FactoryPricingQuoteV1
from agenten.agent_runtime.contracts import ArtifactRef


_SAFE_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+\-/]{0,127}$")
_ARTIFACT_PREFIX = "artifact://business-benchmark-production/"


class BusinessBenchmarkProductionPortError(ValueError):
    """A production port input or immutable binding is not trustworthy."""


class BusinessBenchmarkContentAddressedArtifactStore:
    """Immutable bytes and bindings beneath an injected private workspace root."""

    def __init__(self, root: Path) -> None:
        resolved = root.resolve()
        if ".captain-cook" not in {part.casefold() for part in resolved.parts}:
            raise ValueError("artifact root must use the gitignored .captain-cook namespace")
        self._root = resolved
        self._content_root = resolved / "content" / "sha256"
        self._reference_root = resolved / "references"
        self._binding_root = resolved / "bindings"
        for directory in (
            self._content_root,
            self._reference_root,
            self._binding_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def put(self, content: bytes, media_type: str, *, namespace: str) -> ArtifactRef:
        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes")
        self._require_component(namespace, "artifact namespace")
        if _MEDIA_TYPE.fullmatch(media_type) is None:
            raise ValueError("artifact media type is invalid")
        digest = hashlib.sha256(content).hexdigest()
        reference = ArtifactRef(
            uri=f"{_ARTIFACT_PREFIX}{namespace}/{digest}",
            sha256=digest,
            media_type=media_type,
        )
        target = self._content_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_immutable(target, content)
        metadata_path = self._reference_path(reference.uri)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_immutable(
            metadata_path,
            self._canonical_json(reference.model_dump(mode="json")),
        )
        return reference

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        target = self.local_path(reference)
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise BusinessBenchmarkProductionPortError(
                "artifact content is unavailable"
            ) from exc
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise BusinessBenchmarkProductionPortError(
                "artifact content digest changed"
            )
        return content

    def local_path(self, reference: ArtifactRef) -> Path:
        self._require_reference(reference)
        target = self._content_path(reference.sha256)
        if not target.is_file():
            raise BusinessBenchmarkProductionPortError(
                "artifact content is unavailable"
            )
        return target

    def bind(self, kind: str, identity: str, reference: ArtifactRef) -> ArtifactRef:
        self._require_component(kind, "artifact binding kind")
        if not identity or len(identity) > 512 or "\x00" in identity:
            raise ValueError("artifact binding identity is invalid")
        self.read_bytes(reference)
        path = self._binding_path(kind, identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._canonical_json(
            {
                "identity": identity,
                "reference": reference.model_dump(mode="json"),
            }
        )
        try:
            self._write_immutable(path, payload)
        except BusinessBenchmarkProductionPortError as exc:
            raise BusinessBenchmarkProductionPortError(
                "immutable artifact binding changed"
            ) from exc
        return reference

    def binding(self, kind: str, identity: str) -> ArtifactRef | None:
        self._require_component(kind, "artifact binding kind")
        if not identity or len(identity) > 512 or "\x00" in identity:
            raise ValueError("artifact binding identity is invalid")
        path = self._binding_path(kind, identity)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("identity") != identity:
                raise ValueError("artifact binding identity digest collision")
            reference = ArtifactRef.model_validate(payload["reference"])
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise BusinessBenchmarkProductionPortError(
                "artifact binding is invalid"
            ) from exc
        self.read_bytes(reference)
        return reference

    def _require_reference(self, reference: ArtifactRef) -> None:
        if (
            not isinstance(reference, ArtifactRef)
            or not reference.uri.startswith(_ARTIFACT_PREFIX)
            or reference.uri.rsplit("/", 1)[-1] != reference.sha256
        ):
            raise BusinessBenchmarkProductionPortError(
                "artifact reference is outside the business benchmark store"
            )
        metadata_path = self._reference_path(reference.uri)
        try:
            stored = ArtifactRef.model_validate_json(metadata_path.read_bytes())
        except FileNotFoundError as exc:
            raise BusinessBenchmarkProductionPortError(
                "artifact reference metadata is unavailable"
            ) from exc
        except (OSError, ValueError) as exc:
            raise BusinessBenchmarkProductionPortError(
                "artifact reference metadata is invalid"
            ) from exc
        if stored != reference:
            raise BusinessBenchmarkProductionPortError(
                "artifact reference metadata changed"
            )

    def _content_path(self, digest: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("artifact digest is invalid")
        return self._safe_path(self._content_root / digest[:2] / digest)

    def _reference_path(self, uri: str) -> Path:
        digest = hashlib.sha256(uri.encode("utf-8")).hexdigest()
        return self._safe_path(self._reference_root / f"{digest}.json")

    def _binding_path(self, kind: str, identity: str) -> Path:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self._safe_path(self._binding_root / kind / f"{digest}.json")

    def _safe_path(self, path: Path) -> Path:
        absolute = Path(os.path.abspath(path))
        try:
            common = os.path.commonpath((self._root, absolute))
        except ValueError as exc:
            raise BusinessBenchmarkProductionPortError(
                "artifact path escapes the business benchmark store"
            ) from exc
        if os.path.normcase(common) != os.path.normcase(str(self._root)):
            raise BusinessBenchmarkProductionPortError(
                "artifact path escapes the business benchmark store"
            )
        return absolute

    @staticmethod
    def _require_component(value: str, label: str) -> None:
        if _SAFE_COMPONENT.fullmatch(value) is None:
            raise ValueError(f"{label} is invalid")

    @staticmethod
    def _canonical_json(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _write_immutable(target: Path, content: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                try:
                    existing = target.read_bytes()
                except OSError as exc:
                    raise BusinessBenchmarkProductionPortError(
                        "immutable artifact could not be verified"
                    ) from exc
                if existing != content:
                    raise BusinessBenchmarkProductionPortError(
                        "immutable artifact content changed"
                    )
        finally:
            temporary.unlink(missing_ok=True)


BenchmarkModelClientFactory = Callable[..., ChatCompletionClient]


def _build_openai_client(*, api_key: str, model: str) -> ChatCompletionClient:
    return OpenAIChatCompletionClient(
        api_key=api_key,
        model=model,
        max_retries=0,
        parallel_tool_calls=False,
    )


@dataclass(frozen=True)
class OpenAIBusinessBenchmarkModelClientBuilder:
    """Build a real OpenAI client only for the exact authorized benchmark job."""

    provider: str
    model: str
    _api_key: str = field(repr=False)
    _client_factory: BenchmarkModelClientFactory = field(
        default=_build_openai_client,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        client_factory: BenchmarkModelClientFactory = _build_openai_client,
    ) -> "OpenAIBusinessBenchmarkModelClientBuilder":
        provider = _required(environment, "CAPTAIN_BENCHMARK_PROVIDER")
        if provider != "openai":
            raise ValueError("business benchmark provider must be openai")
        return cls(
            provider=provider,
            model=_required(environment, "CAPTAIN_BENCHMARK_MODEL"),
            _api_key=_required(environment, "OPENAI_API_KEY"),
            _client_factory=client_factory,
        )

    @classmethod
    def from_environment_deferred(
        cls,
        environment: Mapping[str, str],
        *,
        client_factory: BenchmarkModelClientFactory = _build_openai_client,
    ) -> "OpenAIBusinessBenchmarkModelClientBuilder":
        """Validate public provider settings while deferring the secret to effects."""

        provider = _required(environment, "CAPTAIN_BENCHMARK_PROVIDER")
        if provider != "openai":
            raise ValueError("business benchmark provider must be openai")
        return cls(
            provider=provider,
            model=_required(environment, "CAPTAIN_BENCHMARK_MODEL"),
            _api_key=environment.get("OPENAI_API_KEY", "").strip(),
            _client_factory=client_factory,
        )

    def __call__(
        self,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
    ) -> ChatCompletionClient:
        if (
            invocation.job_id != job.job_id
            or invocation.correlation_id != job.correlation_id
            or invocation.subject_version != job.subject_version
        ):
            raise ValueError("model invocation is not bound to the Captain job")
        if not self._api_key:
            raise ValueError("OpenAI provider secret is not present")
        policy = job.execution_policy
        if (
            self.provider != "openai"
            or not policy.live_execution
            or self.model not in policy.allowed_models
            or FactoryLiveCapability.MODEL_INVOKE not in policy.live_capabilities
        ):
            raise ValueError("OpenAI model is not Captain-authorized")
        return self._client_factory(api_key=self._api_key, model=self.model)


class BusinessBenchmarkPricingQuoteSourcePort(Protocol):
    def resolve_quote(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        provider: str,
        model: str,
        now: datetime,
    ) -> FactoryPricingQuoteV1 | None: ...


class ConfiguredBusinessBenchmarkPricingSource:
    """Serve an immutable operator price card with content-addressed evidence."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        version: str,
        effective_at: datetime,
        max_cost_per_call: Decimal,
        input_cost_per_million: Decimal,
        output_cost_per_million: Decimal,
        minimum_cost_usd: Decimal,
        artifacts: BusinessBenchmarkContentAddressedArtifactStore,
    ) -> None:
        if provider != "openai":
            raise ValueError("business benchmark pricing provider must be openai")
        if not model.strip() or not version.strip() or len(version) > 64:
            raise ValueError("business benchmark price card identity is invalid")
        if effective_at.tzinfo is None or effective_at.utcoffset() != timezone.utc.utcoffset(effective_at):
            raise ValueError("business benchmark price card time must be UTC")
        if (
            max_cost_per_call <= 0
            or input_cost_per_million < 0
            or output_cost_per_million < 0
            or minimum_cost_usd < 0
            or minimum_cost_usd > max_cost_per_call
        ):
            raise ValueError("business benchmark price card values are invalid")
        self._provider = provider
        self._model = model
        self._version = version
        self._effective_at = effective_at
        self._max_cost_per_call = max_cost_per_call
        self._input_cost_per_million = input_cost_per_million
        self._output_cost_per_million = output_cost_per_million
        self._minimum_cost_usd = minimum_cost_usd
        self._artifacts = artifacts

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        artifacts: BusinessBenchmarkContentAddressedArtifactStore,
    ) -> "ConfiguredBusinessBenchmarkPricingSource":
        provider = _required(environment, "CAPTAIN_BENCHMARK_PROVIDER")
        if provider != "openai":
            raise ValueError("business benchmark pricing provider must be openai")
        maximum = _decimal(
            environment,
            "CAPTAIN_BENCHMARK_MAX_COST_PER_CALL_USD",
            positive=True,
        )
        minimum = _decimal(
            environment,
            "CAPTAIN_BENCHMARK_PRICING_MINIMUM_COST_USD",
            positive=False,
        )
        if minimum > maximum:
            raise ValueError("minimum benchmark price exceeds the per-call maximum")
        return cls(
            provider=provider,
            model=_required(environment, "CAPTAIN_BENCHMARK_MODEL"),
            version=_required(environment, "CAPTAIN_BENCHMARK_PRICING_VERSION"),
            effective_at=_utc_timestamp(
                environment,
                "CAPTAIN_BENCHMARK_PRICING_EFFECTIVE_AT",
            ),
            max_cost_per_call=maximum,
            input_cost_per_million=_decimal(
                environment,
                "CAPTAIN_BENCHMARK_PRICING_INPUT_COST_PER_MILLION_USD",
                positive=False,
            ),
            output_cost_per_million=_decimal(
                environment,
                "CAPTAIN_BENCHMARK_PRICING_OUTPUT_COST_PER_MILLION_USD",
                positive=False,
            ),
            minimum_cost_usd=minimum,
            artifacts=artifacts,
        )

    def resolve_quote(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        provider: str,
        model: str,
        now: datetime,
    ) -> FactoryPricingQuoteV1 | None:
        if (
            invocation.job_id != job.job_id
            or invocation.correlation_id != job.correlation_id
            or invocation.subject_version != job.subject_version
        ):
            raise ValueError("pricing invocation is not bound to the Captain job")
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise ValueError("pricing quote clock must be UTC")
        if (
            provider != self._provider
            or model != self._model
            or model not in job.execution_policy.allowed_models
            or now < self._effective_at
        ):
            return None
        policy_sha256 = factory_execution_policy_sha256(job)
        evidence = {
            "schema": "captain.business-benchmark-price-card-evidence.v1",
            "job_id": str(job.job_id),
            "subject_version": job.subject_version,
            "execution_policy_sha256": policy_sha256,
            "provider": provider,
            "model": model,
            "pricing_version": self._version,
            "effective_at": self._effective_at.isoformat(),
            "max_cost_per_call": str(self._max_cost_per_call),
            "input_cost_per_million": str(self._input_cost_per_million),
            "output_cost_per_million": str(self._output_cost_per_million),
            "minimum_cost_usd": str(self._minimum_cost_usd),
        }
        encoded = BusinessBenchmarkContentAddressedArtifactStore._canonical_json(
            evidence
        )
        evidence_ref = self._artifacts.put(
            encoded,
            "application/json",
            namespace="pricing-quote",
        )
        return FactoryPricingQuoteV1(
            quote_id=f"benchmark-price-{evidence_ref.sha256[:24]}",
            job_id=job.job_id,
            subject_version=job.subject_version,
            execution_policy_sha256=policy_sha256,
            provider=provider,
            model=model,
            version=self._version,
            effective_at=self._effective_at,
            max_cost_per_call=self._max_cost_per_call,
            input_cost_per_million=self._input_cost_per_million,
            output_cost_per_million=self._output_cost_per_million,
            minimum_cost_usd=self._minimum_cost_usd,
            evidence_ref=evidence_ref,
        )


class BusinessBenchmarkPricingAuthority:
    """Accept only a quote bound to the current job, invocation, and policy."""

    def __init__(self, source: BusinessBenchmarkPricingQuoteSourcePort) -> None:
        self._source = source

    def resolve(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        provider: str,
        model: str,
        now: datetime,
    ) -> FactoryPricingQuoteV1:
        if (
            invocation.job_id != job.job_id
            or invocation.correlation_id != job.correlation_id
            or invocation.subject_version != job.subject_version
        ):
            raise ValueError("pricing invocation is not bound to the Captain job")
        quote = self._source.resolve_quote(
            job=job,
            invocation=invocation,
            provider=provider,
            model=model,
            now=now,
        )
        if quote is None:
            raise ValueError("pricing quote is unknown")
        if (
            quote.job_id != job.job_id
            or quote.subject_version != job.subject_version
            or quote.execution_policy_sha256 != factory_execution_policy_sha256(job)
            or quote.provider != provider
            or quote.model != model
            or quote.effective_at > now
        ):
            raise ValueError("pricing quote is not bound to this Captain job and model")
        return quote


class BusinessBenchmarkCandidateBindingV1(BaseModel):
    """Immutable Captain binding from one exact V3 job to a CAS manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.business-benchmark-candidate-binding.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    candidate_id: str = Field(min_length=1)
    candidate_ref: ArtifactRef
    manifest_ref: ArtifactRef


class BusinessBenchmarkCandidateAuthority:
    """Resolve a benchmark candidate only from its immutable Captain CAS binding."""

    _BINDING_KIND = "candidate-for-job"

    def __init__(
        self,
        artifacts: BusinessBenchmarkContentAddressedArtifactStore,
    ) -> None:
        self._artifacts = artifacts

    def bind_candidate(
        self,
        *,
        job: AgentFactoryJobV3,
        manifest_ref: ArtifactRef,
    ) -> ArtifactRef:
        manifest = self._load_manifest(manifest_ref)
        self._require_source_archive(manifest.source_archive_ref)
        binding = BusinessBenchmarkCandidateBindingV1(
            schema="captain.business-benchmark-candidate-binding.v1",
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            candidate_id=manifest.candidate_id,
            candidate_ref=manifest.source_archive_ref,
            manifest_ref=manifest_ref,
        )
        binding_ref = self._artifacts.put(
            binding.model_dump_json(by_alias=True).encode("utf-8"),
            "application/json",
            namespace="candidate-binding",
        )
        return self._artifacts.bind(
            self._BINDING_KIND,
            str(job.job_id),
            binding_ref,
        )

    def resolve(
        self,
        *,
        job: AgentFactoryJobV3,
        expected_candidate_id: str,
        expected_candidate_ref: ArtifactRef,
    ) -> ResolvedFactoryCandidate:
        binding_ref = self._artifacts.binding(
            self._BINDING_KIND,
            str(job.job_id),
        )
        if binding_ref is None:
            raise BusinessBenchmarkProductionPortError(
                "business benchmark candidate binding is missing"
            )
        try:
            binding = BusinessBenchmarkCandidateBindingV1.model_validate_json(
                self._artifacts.read_bytes(binding_ref)
            )
        except ValueError as exc:
            raise BusinessBenchmarkProductionPortError(
                "business benchmark candidate binding is invalid"
            ) from exc
        if (
            binding.job_id != job.job_id
            or binding.correlation_id != job.correlation_id
            or binding.subject_version != job.subject_version
            or binding.candidate_id != expected_candidate_id
            or binding.candidate_ref != expected_candidate_ref
        ):
            raise BusinessBenchmarkProductionPortError(
                "business benchmark candidate scope changed"
            )
        manifest = self._load_manifest(binding.manifest_ref)
        if (
            manifest.candidate_id != binding.candidate_id
            or manifest.source_archive_ref != binding.candidate_ref
        ):
            raise BusinessBenchmarkProductionPortError(
                "business benchmark candidate manifest changed"
            )
        self._require_source_archive(manifest.source_archive_ref)
        return ResolvedFactoryCandidate(
            candidate=manifest,
            source_archive=self._artifacts.local_path(
                manifest.source_archive_ref
            ),
        )

    def _load_manifest(self, reference: ArtifactRef) -> FactoryCandidateManifest:
        if reference.media_type != "application/json":
            raise BusinessBenchmarkProductionPortError(
                "business benchmark candidate manifest media type is invalid"
            )
        try:
            return FactoryCandidateManifest.model_validate_json(
                self._artifacts.read_bytes(reference)
            )
        except ValueError as exc:
            raise BusinessBenchmarkProductionPortError(
                "business benchmark candidate manifest is invalid"
            ) from exc

    def _require_source_archive(self, reference: ArtifactRef) -> None:
        if reference.media_type != "application/zip":
            raise BusinessBenchmarkProductionPortError(
                "business benchmark candidate source is not a ZIP archive"
            )
        self._artifacts.read_bytes(reference)


def factory_execution_policy_sha256(job: AgentFactoryJobV3) -> str:
    return hashlib.sha256(
        BusinessBenchmarkContentAddressedArtifactStore._canonical_json(
            job.execution_policy.model_dump(mode="json", by_alias=True)
        )
    ).hexdigest()


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"required production setting is missing: {name}")
    return value


def _decimal(
    environment: Mapping[str, str],
    name: str,
    *,
    positive: bool,
) -> Decimal:
    raw = _required(environment, name)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"production money setting is invalid: {name}") from exc
    if (
        not value.is_finite()
        or value < 0
        or (positive and value <= 0)
        or value.as_tuple().exponent < -8
    ):
        raise ValueError(f"production money setting is invalid: {name}")
    return value


def _utc_timestamp(environment: Mapping[str, str], name: str) -> datetime:
    raw = _required(environment, name)
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"production UTC timestamp is invalid: {name}") from exc
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"production UTC timestamp is invalid: {name}")
    return value


__all__ = [
    "BusinessBenchmarkCandidateAuthority",
    "BusinessBenchmarkCandidateBindingV1",
    "BusinessBenchmarkPricingAuthority",
    "BusinessBenchmarkPricingQuoteSourcePort",
    "BusinessBenchmarkContentAddressedArtifactStore",
    "BusinessBenchmarkProductionPortError",
    "ConfiguredBusinessBenchmarkPricingSource",
    "OpenAIBusinessBenchmarkModelClientBuilder",
    "factory_execution_policy_sha256",
]
