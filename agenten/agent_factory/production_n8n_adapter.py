"""Captain-authorized n8n MCP tools with provider-observed REST evidence."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from autogen_core import CancellationToken
from autogen_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.capability_live_adapters import (
    ContentAddressedArtifactStore,
)
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_factory.team_execution import (
    FactoryN8nExecutionEvidenceV1,
    FactoryN8nToolAuthorizationV1,
)
from agenten.agent_runtime.capabilities import PROFILE_CAPABILITIES
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeCommandPayload,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    CapabilityProfile,
    IntegrationIntent,
    RuntimeLimits,
    RuntimeOperation,
    RuntimeStatus,
)
from agenten.agent_runtime.n8n_mcp_broker import McpLeaseIssuer
from agenten.agent_runtime.n8n_endpoint import N8nEndpoint
from agenten.targets.n8n import (
    N8nExecutionEvidence,
    N8nExecutionRecord,
    N8nHttpClient,
    N8nWorkflowRecord,
)


class CaptainN8nAdapterError(RuntimeError):
    """An n8n effect lacks exact Captain or provider evidence."""


CAPTAIN_FACTORY_N8N_WORKFLOW_NAME = "Captain Factory Integration Evidence"


@dataclass(frozen=True)
class CaptainN8nToolBinding:
    """Bind a candidate-visible typed tool to one broker-visible MCP tool."""

    tool: TypedN8nTool
    mcp_tool_name: str
    workflow_id: str
    workflow_name: str
    required_node_types: tuple[str, ...] = (
        "n8n-nodes-base.webhook",
        "n8n-nodes-base.set",
    )

    def __post_init__(self) -> None:
        if not self.mcp_tool_name or any(
            character.isspace() for character in self.mcp_tool_name
        ):
            raise ValueError("mcp_tool_name must be a nonblank identifier")
        if not self.workflow_id.strip():
            raise ValueError("n8n tool binding requires a pinned workflowId")
        if not self.workflow_name.strip():
            raise ValueError("n8n tool binding requires a pinned workflow name")
        if (
            not self.required_node_types
            or len(self.required_node_types) != len(set(self.required_node_types))
            or any(not item.strip() for item in self.required_node_types)
        ):
            raise ValueError("n8n tool binding requires unique node types")


@dataclass(frozen=True)
class CaptainN8nWorkflowObservation:
    record: N8nWorkflowRecord
    definition: Mapping[str, object]


def build_captain_factory_n8n_binding(
    environ: Mapping[str, str],
    *,
    tool: TypedN8nTool,
) -> CaptainN8nToolBinding:
    """Pin the one validated demo workflow; never expose workflow choice to a model."""

    workflow_id = environ.get("CAPTAIN_FACTORY_N8N_WORKFLOW_ID", "").strip()
    if not workflow_id:
        raise CaptainN8nAdapterError(
            "TODO_TOOL.v1 required capability=n8n; "
            "name=CAPTAIN_FACTORY_N8N_WORKFLOW_ID"
        )
    return CaptainN8nToolBinding(
        tool=tool,
        mcp_tool_name="execute_workflow",
        workflow_id=workflow_id,
        workflow_name=CAPTAIN_FACTORY_N8N_WORKFLOW_NAME,
    )


class N8nGrantRegistrarPort(Protocol):
    def register(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> None: ...


class N8nMcpTransportPort(Protocol):
    async def call(
        self,
        *,
        broker_url: str,
        lease_token: str,
        tool_name: str,
        arguments: Mapping[str, object],
        call_id: str,
    ) -> Mapping[str, object]: ...


class N8nExecutionReaderPort(Protocol):
    async def fetch_workflow(
        self,
        workflow_id: str,
    ) -> CaptainN8nWorkflowObservation: ...

    async def fetch_execution(self, execution_id: str) -> N8nExecutionRecord: ...


class CaptainN8nRestExecutionReader:
    """Read durable execution evidence from Captain n8n REST on port 5679."""

    def __init__(self, endpoint: N8nEndpoint, client: httpx.AsyncClient) -> None:
        if endpoint.mode != "captain-builder":
            raise ValueError("Factory n8n evidence requires Captain builder ownership")
        self._client = N8nHttpClient.from_endpoint(endpoint, client)
        self._endpoint = endpoint
        self._http = client

    async def fetch_workflow(
        self,
        workflow_id: str,
    ) -> CaptainN8nWorkflowObservation:
        try:
            response = await self._http.get(
                f"{self._endpoint.api_base_url.rstrip('/')}/api/v1/workflows/{workflow_id}",
                headers={"X-N8N-API-KEY": self._endpoint.api_key},
            )
            response.raise_for_status()
            definition = response.json()
            record = N8nWorkflowRecord.model_validate(definition)
        except (httpx.HTTPError, ValueError):
            raise CaptainN8nAdapterError(
                "Captain n8n workflow could not be validated"
            ) from None
        if record.id != workflow_id:
            raise CaptainN8nAdapterError(
                "Captain n8n workflow identity did not match its binding"
            )
        if not isinstance(definition, dict):
            raise CaptainN8nAdapterError(
                "Captain n8n workflow definition is invalid"
            )
        return CaptainN8nWorkflowObservation(
            record=record,
            definition=definition,
        )

    async def fetch_execution(self, execution_id: str) -> N8nExecutionRecord:
        return await self._client.fetch_execution(execution_id)


class N8nMcpToolArguments(BaseModel):
    """Opaque typed-tool payload; workflow identity remains host-owned."""

    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, object] = Field(default_factory=dict)


class GatewayN8nGrantRegistrar:
    """Synchronously materialize command/grant authority before tool dispatch."""

    def __init__(
        self,
        *,
        gateway_url: str,
        token: str,
        client: httpx.Client,
    ) -> None:
        if not gateway_url.strip() or not token:
            raise ValueError("Gateway n8n grant registration is not configured")
        self._gateway_url = gateway_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = client

    def register(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> None:
        self._post(
            "/v1/runtime/commands",
            command.model_dump(mode="json", by_alias=True),
            {202},
            "runtime command",
        )
        self._post(
            "/v1/runtime/grants",
            grant.model_dump(mode="json", by_alias=True),
            {200, 201},
            "capability grant",
        )

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        expected: set[int],
        label: str,
    ) -> None:
        try:
            response = self._client.post(
                f"{self._gateway_url}{path}",
                headers=self._headers,
                json=payload,
            )
        except httpx.HTTPError:
            raise CaptainN8nAdapterError(
                f"Captain Gateway could not register n8n {label}"
            ) from None
        if response.status_code not in expected:
            raise CaptainN8nAdapterError(
                f"Captain Gateway rejected n8n {label}"
            )


class HttpxStreamableN8nMcpTransport:
    """Minimal MCP Streamable-HTTP client for one lease-scoped tool call."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def call(
        self,
        *,
        broker_url: str,
        lease_token: str,
        tool_name: str,
        arguments: Mapping[str, object],
        call_id: str,
    ) -> Mapping[str, object]:
        endpoint = f"{broker_url.rstrip('/')}/mcp-server/http"
        headers = {
            "Authorization": f"Bearer {lease_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        initialized = await self._post(
            endpoint,
            headers,
            {
                "jsonrpc": "2.0",
                "id": f"init-{call_id}",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "captain-factory",
                        "version": "1",
                    },
                },
            },
        )
        initialized_payload = _decode_jsonrpc_response(initialized)
        if "error" in initialized_payload or not isinstance(
            initialized_payload.get("result"), Mapping
        ):
            raise CaptainN8nAdapterError("n8n MCP initialization was rejected")
        session_id = initialized.headers.get("mcp-session-id", "").strip()
        call_headers = dict(headers)
        if session_id:
            call_headers["Mcp-Session-Id"] = session_id
        await self._post(
            endpoint,
            call_headers,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            allow_empty=True,
        )
        response = await self._post(
            endpoint,
            call_headers,
            {
                "jsonrpc": "2.0",
                "id": call_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": dict(arguments)},
            },
        )
        payload = _decode_jsonrpc_response(response)
        if payload.get("id") != call_id or "error" in payload:
            raise CaptainN8nAdapterError("n8n MCP tool call was rejected")
        result = payload.get("result")
        extracted = _extract_mcp_result(result)
        if extracted is None:
            raise CaptainN8nAdapterError(
                "n8n MCP tool call returned no structured evidence"
            )
        return extracted

    async def _post(
        self,
        endpoint: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        *,
        allow_empty: bool = False,
    ) -> httpx.Response:
        try:
            response = await self._client.post(
                endpoint,
                headers=dict(headers),
                json=dict(payload),
            )
            response.raise_for_status()
        except httpx.HTTPError:
            raise CaptainN8nAdapterError("Captain n8n MCP broker call failed") from None
        if not allow_empty and not response.content:
            raise CaptainN8nAdapterError("Captain n8n MCP broker returned no response")
        return response


class _CaptainBrokerTool(BaseTool[N8nMcpToolArguments, dict[str, object]]):
    def __init__(self, adapter: "CaptainBrokerN8nToolAdapter", binding: CaptainN8nToolBinding) -> None:
        super().__init__(
            N8nMcpToolArguments,
            dict,
            binding.tool.name,
            binding.tool.description,
        )
        self._adapter = adapter
        self._binding = binding

    async def run(
        self,
        args: N8nMcpToolArguments,
        cancellation_token: CancellationToken,
    ) -> dict[str, object]:
        return await self._adapter._run(
            self._binding,
            args.arguments,
            cancellation_token,
        )


class CaptainBrokerN8nToolAdapter:
    """Actual Factory n8n adapter: Gateway authority, MCP effect, REST proof."""

    def __init__(
        self,
        *,
        bindings: Sequence[CaptainN8nToolBinding],
        correlation_id: UUID,
        subject_version: int,
        project_id: str,
        batch_id: str,
        workspace_ref: str,
        broker_url: str,
        signing_secret: str,
        registrar: N8nGrantRegistrarPort,
        mcp: N8nMcpTransportPort,
        executions: N8nExecutionReaderPort,
        artifacts: ContentAddressedArtifactStore,
        clock: Callable[[], datetime] | None = None,
        grant_lifetime: timedelta = timedelta(minutes=5),
    ) -> None:
        by_name = {binding.tool.name: binding for binding in bindings}
        if not by_name or len(by_name) != len(tuple(bindings)):
            raise ValueError("n8n tool bindings must be nonempty and unique")
        _require_local_broker_url(broker_url)
        if not workspace_ref.startswith("workspace://"):
            raise ValueError("n8n workspace_ref must be opaque")
        if grant_lifetime <= timedelta(0) or grant_lifetime > timedelta(minutes=15):
            raise ValueError("n8n grant lifetime is outside Captain policy")
        self._bindings = by_name
        self._correlation_id = correlation_id
        self._subject_version = subject_version
        self._project_id = project_id
        self._batch_id = batch_id
        self._workspace_ref = workspace_ref
        self._broker_url = broker_url.rstrip("/")
        self._issuer = McpLeaseIssuer(signing_secret)
        self._registrar = registrar
        self._mcp = mcp
        self._executions = executions
        self._artifacts = artifacts
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._grant_lifetime = grant_lifetime
        self._tools = {
            name: _CaptainBrokerTool(self, binding)
            for name, binding in by_name.items()
        }
        self._claims: ContextVar[
            dict[str, FactoryN8nToolAuthorizationV1]
        ] = ContextVar("captain_n8n_claims", default={})
        self._evidence: list[FactoryN8nExecutionEvidenceV1] = []

    def tool(self, name: str) -> BaseTool[BaseModel, Any]:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ValueError("n8n tool is not registered") from exc

    def authorization(self, name: str) -> FactoryN8nToolAuthorizationV1:
        try:
            binding = self._bindings[name]
        except KeyError as exc:
            raise ValueError("n8n tool is not registered") from exc
        now = self._utc_now()
        command_id = uuid4()
        tool_ref = binding.tool.opaque_reference()
        digest = _tool_ref_digest(tool_ref.model_dump(mode="json", by_alias=True))
        command = AgentRuntimeCommand(
            schema_name="captain.agent-runtime-command.v1",
            event_id=command_id,
            correlation_id=self._correlation_id,
            occurred_at=now,
            producer="captain",
            subject_id=name,
            subject_version=self._subject_version,
            payload=AgentRuntimeCommandPayload(
                operation=RuntimeOperation.CODEX_RUN,
                project_id=self._project_id,
                batch_id=self._batch_id,
                subtask_id=name,
                workspace_ref=self._workspace_ref,
                prompt_ref=ArtifactRef(
                    uri=f"artifact://factory-n8n-tool/{digest}",
                    sha256=digest,
                    media_type="application/json",
                ),
                integration_intent=IntegrationIntent.N8N,
                capability_profile=CapabilityProfile.N8N_BUILDER,
                limits=RuntimeLimits(wall_seconds=60, max_iterations=1),
            ),
        )
        grant = CapabilityGrant(
            schema_name="captain.capability-grant.v1",
            grant_id=f"grant-n8n-{command_id.hex}",
            command_id=command_id,
            batch_id=self._batch_id,
            batch_version=self._subject_version,
            subtask_id=name,
            workspace_ref=self._workspace_ref,
            profile=CapabilityProfile.N8N_BUILDER,
            capabilities=tuple(
                sorted(PROFILE_CAPABILITIES[CapabilityProfile.N8N_BUILDER])
            ),
            mcp_servers=("n8n-mcp",),
            issued_at=now,
            expires_at=now + self._grant_lifetime,
        )
        claim = FactoryN8nToolAuthorizationV1(
            tool_name=name,
            approved_tool_ref=tool_ref,
            runtime_command=command,
            capability_grant=grant,
        )
        self._registrar.register(command, grant)
        current = dict(self._claims.get())
        current[name] = claim
        self._claims.set(current)
        return claim

    def observed_evidence(self) -> tuple[FactoryN8nExecutionEvidenceV1, ...]:
        return tuple(self._evidence)

    async def _run(
        self,
        binding: CaptainN8nToolBinding,
        arguments: Mapping[str, object],
        cancellation_token: CancellationToken,
    ) -> dict[str, object]:
        claims = dict(self._claims.get())
        claim = claims.pop(binding.tool.name, None)
        self._claims.set(claims)
        if claim is None:
            raise ValueError("n8n tool call requires a fresh Captain authorization")
        if cancellation_token.is_cancelled():
            raise CaptainN8nAdapterError("n8n tool call was cancelled before dispatch")
        now = self._utc_now()
        lease_token = self._issuer.issue(
            claim.capability_grant,
            claim.runtime_command,
            self._broker_url,
            now,
        )
        call_id = f"mcp-{uuid4()}"
        correlation = str(self._correlation_id)
        if set(arguments) != {"input"}:
            raise ValueError("n8n model arguments must contain only body.input")
        workflow_id = binding.workflow_id
        workflow = await self._executions.fetch_workflow(workflow_id)
        if (
            workflow.record.id != workflow_id
            or workflow.record.name != binding.workflow_name
        ):
            raise CaptainN8nAdapterError(
                "Captain n8n workflow identity did not match its binding"
            )
        node_types = {
            str(node.get("type", ""))
            for node in workflow.definition.get("nodes", [])
            if isinstance(node, Mapping)
        }
        if not set(binding.required_node_types).issubset(node_types):
            raise CaptainN8nAdapterError(
                "Captain n8n workflow schema did not match its binding"
            )
        workflow_ref = self._artifacts.put(
            _canonical_json(dict(workflow.definition)),
            "application/json",
            namespace="n8n-workflow",
        )
        artifact_digest = workflow_ref.sha256
        scoped_arguments = {
            "workflowId": workflow_id,
            "executionMode": "manual",
            "inputs": {
                "type": "webhook",
                "body": {
                    "input": arguments["input"],
                    "correlation_id": correlation,
                    "artifact_digest": artifact_digest,
                    "idempotency_key": str(claim.runtime_command.event_id),
                },
            },
        }
        raw = await self._mcp.call(
            broker_url=self._broker_url,
            lease_token=lease_token,
            tool_name=binding.mcp_tool_name,
            arguments=scoped_arguments,
            call_id=call_id,
        )
        execution_id = _required_string(raw, "execution_id", "executionId")
        observed_workflow_id = _required_string(raw, "workflow_id", "workflowId")
        if observed_workflow_id != workflow_id:
            raise ValueError("n8n MCP execution used a different pinned workflow")
        record = await self._executions.fetch_execution(execution_id)
        if (
            record.execution_id != execution_id
            or record.workflow_id != workflow_id
            or record.status != "success"
            or record.output.get("artifact_digest") != artifact_digest
            or record.output.get("correlation_id") != correlation
        ):
            raise ValueError("n8n REST execution evidence does not match MCP effect")
        observed_at = self._utc_now()
        observation_ref = self._artifacts.put(
            _canonical_json(
                {
                    "schema": "captain.n8n-provider-observation.v1",
                    "command_id": str(claim.runtime_command.event_id),
                    "grant_id": claim.capability_grant.grant_id,
                    "mcp_call_id": call_id,
                    "tool_name": binding.tool.name,
                    "workflow_id": workflow_id,
                    "execution_id": execution_id,
                    "artifact_digest": artifact_digest,
                    "correlation_id": correlation,
                    "output_sha256": _tool_ref_digest(record.output),
                }
            ),
            "application/json",
            namespace="n8n-execution-evidence",
        )
        runtime_result = AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=uuid5(
                NAMESPACE_URL,
                f"captain-n8n-result|{claim.runtime_command.event_id}|{execution_id}",
            ),
            command_id=claim.runtime_command.event_id,
            correlation_id=self._correlation_id,
            occurred_at=observed_at,
            producer="agent-runtime",
            subject_id=binding.tool.name,
            subject_version=self._subject_version,
            grant_id=claim.capability_grant.grant_id,
            operation=claim.runtime_command.payload.operation,
            status=RuntimeStatus.SUCCEEDED,
            session_id=f"n8n-{execution_id}",
            artifact_refs=(workflow_ref,),
            evidence_refs=(observation_ref,),
        )
        evidence = FactoryN8nExecutionEvidenceV1(
            tool_name=binding.tool.name,
            approved_tool_ref=claim.approved_tool_ref,
            runtime_command=claim.runtime_command,
            capability_grant=claim.capability_grant,
            runtime_result=runtime_result,
            mcp_call_id=call_id,
            workflow_ref=workflow_ref,
            execution=N8nExecutionEvidence(
                execution_id=execution_id,
                workflow_id=workflow_id,
                artifact_digest=artifact_digest,
                correlation_id=correlation,
                status="success",
            ),
            evidence_ref=observation_ref,
        )
        self._evidence.append(evidence)
        result: dict[str, object] = {
            "status": "success",
            "execution_id": execution_id,
            "workflow_id": workflow_id,
        }
        if "result" in record.output:
            result["result"] = record.output["result"]
        return result

    def _utc_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise ValueError("n8n adapter clock must be UTC")
        return now


def _require_local_broker_url(value: str) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Captain n8n MCP broker must be a local explicit HTTP endpoint")


def _required_string(
    value: Mapping[str, object],
    name: str,
    alias: str | None = None,
) -> str:
    raw = value.get(name, value.get(alias) if alias is not None else None)
    if not isinstance(raw, str) or not raw.strip():
        raise CaptainN8nAdapterError(f"n8n MCP result omitted {name}")
    return raw.strip()


def _required_digest(value: Mapping[str, object], name: str) -> str:
    raw = _required_string(value, name)
    if len(raw) != 64 or any(character not in "0123456789abcdef" for character in raw):
        raise CaptainN8nAdapterError(f"n8n MCP result returned invalid {name}")
    return raw


def _tool_ref_digest(value: object) -> str:
    import hashlib

    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _decode_jsonrpc_response(response: httpx.Response) -> dict[str, object]:
    content_type = response.headers.get("content-type", "").casefold()
    try:
        if "text/event-stream" not in content_type:
            value = response.json()
        else:
            data_lines = [
                line[5:].strip()
                for line in response.text.splitlines()
                if line.startswith("data:")
            ]
            value = json.loads("\n".join(data_lines))
    except (ValueError, json.JSONDecodeError):
        raise CaptainN8nAdapterError("n8n MCP response is invalid") from None
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise CaptainN8nAdapterError("n8n MCP response is not JSON-RPC 2.0")
    return value


def _extract_mcp_result(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    structured = value.get("structuredContent")
    if isinstance(structured, Mapping):
        return dict(structured)
    if (
        ("execution_id" in value or "executionId" in value)
        and ("workflow_id" in value or "workflowId" in value)
    ):
        return dict(value)
    content = value.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, Mapping) or item.get("type") != "text":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
    return None
