"""Strict, secret-free contracts for the D12 demo bundle."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
CANONICAL_INPUT_PATHS = (
    "demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md",
    "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md",
    "demo_inputs/agent_factory/claims_resolution/TO_BE_BUILT.md",
)
SERVICE_ORDER = ("gateway", "provider_proxy", "runtime", "benchmark")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DemoServiceV1(_FrozenModel):
    name: Literal["gateway", "provider_proxy", "runtime", "benchmark"]
    argv: tuple[str, ...] = Field(min_length=2)
    working_directory: str = Field(min_length=1)
    port: int = Field(ge=1024, le=65535, strict=True)
    health_url: str = Field(min_length=1)
    health_token_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    environment: tuple[str, ...] = Field(min_length=1)
    artifact_paths: tuple[str, ...] = Field(min_length=1)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("service argv is invalid")
        allowed = {"{python}", "{repository}", "{state_root}"}
        for item in value:
            fields = re.findall(r"\{[^{}]+\}", item)
            if any(field not in allowed for field in fields):
                raise ValueError("service argv contains an unsupported placeholder")
        return value

    @model_validator(mode="after")
    def validate_service_authority(self) -> "DemoServiceV1":
        parsed = urlsplit(self.health_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port != self.port
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            raise ValueError("service health URL must be its exact loopback listener")
        if len(set(self.environment)) != len(self.environment):
            raise ValueError("service environment contains duplicate names")
        if self.health_token_env not in self.environment:
            raise ValueError("service health token is outside its environment authority")
        return self


class DemoBundleV1(_FrozenModel):
    schema_name: Literal["captain.capability-demo-bundle.v1"] = Field(alias="schema")
    repository_revision: str
    canonical_inputs: dict[str, str]
    artifact_digests: dict[str, str]
    compose_files: dict[str, str]
    compose_project: str = Field(pattern=r"^[a-z][a-z0-9-]{2,62}$")
    benchmark_catalog_path: str = Field(min_length=1)
    benchmark_catalog_sha256: str
    gateway_base_url: str
    minibook_evidence_url_template: str
    minibook_token_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    services: tuple[DemoServiceV1, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def validate_bundle_contract(self) -> "DemoBundleV1":
        if REVISION_PATTERN.fullmatch(self.repository_revision) is None:
            raise ValueError("repository revision is invalid")
        if set(self.canonical_inputs) != set(CANONICAL_INPUT_PATHS):
            raise ValueError("bundle must bind the three canonical repository inputs")
        if len(set(self.canonical_inputs.values())) != 3:
            raise ValueError("bundle canonical inputs must have distinct bytes")
        if tuple(service.name for service in self.services) != SERVICE_ORDER:
            raise ValueError("bundle service dependency order is invalid")
        if len({service.port for service in self.services}) != len(self.services):
            raise ValueError("bundle service ports must be distinct")
        if self.gateway_base_url != f"http://127.0.0.1:{self.services[0].port}":
            raise ValueError("bundle Gateway URL changed its service binding")
        if self.minibook_evidence_url_template.count("{assembly_id}") != 1:
            raise ValueError("Minibook readback URL must bind one assembly identity")
        for digest in (
            *self.canonical_inputs.values(),
            *self.artifact_digests.values(),
            *self.compose_files.values(),
            self.benchmark_catalog_sha256,
        ):
            if SHA256_PATTERN.fullmatch(digest) is None:
                raise ValueError("bundle contains an invalid SHA-256 digest")
        if not self.compose_files or not self.artifact_digests:
            raise ValueError("bundle must bind Compose and process artifacts")
        if "OPENAI_API_KEY" not in self.services[1].environment:
            raise ValueError("provider proxy must own the provider credential")
        if any(
            "OPENAI_API_KEY" in service.environment
            for service in (self.services[0], self.services[2], self.services[3])
        ):
            raise ValueError("OPENAI_API_KEY escaped the provider proxy boundary")
        return self


__all__ = [
    "CANONICAL_INPUT_PATHS",
    "DemoBundleV1",
    "DemoServiceV1",
    "SERVICE_ORDER",
]
