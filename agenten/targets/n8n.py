"""Typed n8n deployment and execution target with verified provider evidence."""
from __future__ import annotations
import asyncio
import copy
import re
from typing import Any, Literal, Protocol
import httpx
from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_runtime.n8n_endpoint import N8nEndpoint

class N8nTargetError(RuntimeError):
    """An n8n provider operation failed without exposing credentials."""

class N8nEvidenceError(N8nTargetError):
    """Provider success lacked matching durable execution evidence."""

class SealedArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    namespace: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    workflow: dict[str, Any]

class ValidationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=128)
    input_payload: dict[str, Any]

class N8nDeployment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workflow_id: str = Field(min_length=1)
    workflow_name: str = Field(min_length=1)
    webhook_path: str = Field(pattern=r"^[a-z0-9-]+$")
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

class N8nExecutionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    execution_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    correlation_id: str = Field(min_length=1)
    status: Literal["success"]

class N8nWorkflowRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)

class N8nExecutionStart(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    execution_id: str = Field(min_length=1)

class N8nExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    execution_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    status: str
    output: dict[str, Any]

class N8nClient(Protocol):
    async def create_or_update_workflow(self, *, name: str, definition: dict[str, Any]) -> N8nWorkflowRecord: ...
    async def activate_workflow(self, workflow_id: str) -> None: ...
    async def execute_webhook(self, webhook_path: str, payload: dict[str, Any]) -> N8nExecutionStart: ...
    async def fetch_execution(self, execution_id: str) -> N8nExecutionRecord: ...

def _provider_output(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        run_data = payload["data"]["resultData"]["runData"]
        for runs in reversed(tuple(run_data.values())):
            for run in reversed(runs):
                main = run["data"]["main"]
                if main and main[0]:
                    output = main[0][0]["json"]
                    if isinstance(output, dict) and {
                        "artifact_digest",
                        "correlation_id",
                    }.issubset(output):
                        return output
    except (KeyError, IndexError, TypeError):
        pass
    raise N8nEvidenceError("n8n did not return matching execution evidence")

class N8nHttpClient:
    def __init__(
        self,
        *,
        api_base_url: str,
        webhook_base_url: str,
        api_key: str,
        http: httpx.AsyncClient,
        evidence_attempts: int = 30,
        evidence_delay_seconds: float = 0.1,
    ) -> None:
        if not api_key:
            raise ValueError("n8n api_key must not be empty")
        if evidence_attempts < 1 or evidence_delay_seconds < 0:
            raise ValueError("n8n evidence polling configuration is invalid")
        self._api_base_url = api_base_url.rstrip("/")
        self._webhook_base_url = webhook_base_url.rstrip("/")
        self._headers = {"X-N8N-API-KEY": api_key}
        self._http = http
        self._evidence_attempts = evidence_attempts
        self._evidence_delay_seconds = evidence_delay_seconds

    @classmethod
    def from_endpoint(
        cls,
        endpoint: N8nEndpoint,
        http: httpx.AsyncClient,
    ) -> "N8nHttpClient":
        """Create a client only from an already selected endpoint contract."""

        return cls(
            api_base_url=endpoint.api_base_url,
            webhook_base_url=endpoint.webhook_base_url,
            api_key=endpoint.api_key,
            http=http,
        )

    async def create_or_update_workflow(self, *, name: str, definition: dict[str, Any]) -> N8nWorkflowRecord:
        response = await self._request("GET", f"{self._api_base_url}/api/v1/workflows", params={"limit": 100})
        body = response.json()
        candidates = body.get("data", []) if isinstance(body, dict) else []
        existing = next((item for item in candidates if isinstance(item, dict) and item.get("name") == name), None)
        payload = {"name": name, **definition}
        if existing is None:
            stored = await self._request("POST", f"{self._api_base_url}/api/v1/workflows", json=payload)
        else:
            workflow_id = str(existing.get("id", ""))
            if not workflow_id:
                raise N8nTargetError("n8n workflow lookup returned invalid data")
            stored = await self._request("PUT", f"{self._api_base_url}/api/v1/workflows/{workflow_id}", json=payload)
        try:
            return N8nWorkflowRecord.model_validate(stored.json())
        except ValueError:
            raise N8nTargetError("n8n workflow write returned invalid data") from None

    async def activate_workflow(self, workflow_id: str) -> None:
        response = await self._request("POST", f"{self._api_base_url}/api/v1/workflows/{workflow_id}/activate")
        if response.json().get("active") is not True:
            raise N8nTargetError("n8n workflow activation was not confirmed")

    async def execute_webhook(self, webhook_path: str, payload: dict[str, Any]) -> N8nExecutionStart:
        response = await self._request("POST", f"{self._webhook_base_url}/webhook/{webhook_path}", json=payload, authenticated=False)
        try:
            return N8nExecutionStart.model_validate(response.json())
        except ValueError:
            raise N8nEvidenceError("n8n webhook did not return an execution id") from None

    async def fetch_execution(self, execution_id: str) -> N8nExecutionRecord:
        for attempt in range(self._evidence_attempts):
            response = await self._request(
                "GET",
                f"{self._api_base_url}/api/v1/executions/{execution_id}",
                params={"includeData": "true"},
            )
            try:
                payload = response.json()
                return N8nExecutionRecord(
                    execution_id=str(payload["id"]),
                    workflow_id=str(payload["workflowId"]),
                    status=str(payload["status"]),
                    output=_provider_output(payload),
                )
            except (KeyError, ValueError, N8nEvidenceError):
                if attempt + 1 == self._evidence_attempts:
                    break
                await asyncio.sleep(self._evidence_delay_seconds)
        raise N8nEvidenceError("n8n did not return matching execution evidence") from None

    async def _request(self, method: str, url: str, *, json: dict[str, Any] | None = None, params: dict[str, Any] | None = None, authenticated: bool = True) -> httpx.Response:
        try:
            response = await self._http.request(method, url, json=json, params=params, headers=self._headers if authenticated else None)
            response.raise_for_status()
            return response
        except (httpx.HTTPError, ValueError):
            raise N8nTargetError("n8n provider request failed") from None

class N8nTarget:
    def __init__(
        self,
        client: N8nClient,
        *,
        evidence_attempts: int = 10,
        evidence_delay_seconds: float = 0.1,
    ) -> None:
        if evidence_attempts < 1 or evidence_delay_seconds < 0:
            raise ValueError("n8n evidence polling configuration is invalid")
        self._client = client
        self._evidence_attempts = evidence_attempts
        self._evidence_delay_seconds = evidence_delay_seconds

    async def deploy(self, artifact: SealedArtifact) -> N8nDeployment:
        forbidden_identity = {"name", "id", "workflowId", "webhookId", "webhook_path"}
        if forbidden_identity.intersection(artifact.workflow):
            raise ValueError("workflow definition must not override provider identity")
        for node in artifact.workflow.get("nodes", []):
            if not isinstance(node, dict):
                continue
            parameters = node.get("parameters")
            if (
                isinstance(parameters, dict)
                and "path" in parameters
                and parameters["path"] != "{{CAPTAIN_WEBHOOK_PATH}}"
            ):
                raise ValueError("workflow definition must not override provider identity")
        name = f"captain::{artifact.namespace}::{artifact.artifact_id}::{artifact.artifact_digest[:12]}"
        webhook_path = re.sub(r"[^a-z0-9-]+", "-", f"captain-{artifact.namespace}-{artifact.artifact_id}-{artifact.artifact_digest[:12]}").strip("-")
        definition = copy.deepcopy(artifact.workflow)
        self._replace_webhook_placeholder(definition, webhook_path)
        stored = await self._client.create_or_update_workflow(name=name, definition=definition)
        if stored.name != name:
            raise N8nEvidenceError("n8n workflow identity did not match deployment")
        await self._client.activate_workflow(stored.id)
        return N8nDeployment(workflow_id=stored.id, workflow_name=name, webhook_path=webhook_path, artifact_digest=artifact.artifact_digest)

    async def execute(self, deployment: N8nDeployment, case: ValidationCase) -> N8nExecutionEvidence:
        payload = {"artifact_digest": deployment.artifact_digest, "correlation_id": case.correlation_id, "case_id": case.case_id, "input": case.input_payload}
        started = await self._client.execute_webhook(deployment.webhook_path, payload)
        for attempt in range(self._evidence_attempts):
            record = await self._client.fetch_execution(started.execution_id)
            if (
                record.execution_id == started.execution_id
                and record.workflow_id == deployment.workflow_id
                and record.status == "success"
                and record.output.get("artifact_digest") == deployment.artifact_digest
                and record.output.get("correlation_id") == case.correlation_id
            ):
                return N8nExecutionEvidence(
                    execution_id=record.execution_id,
                    workflow_id=record.workflow_id,
                    artifact_digest=deployment.artifact_digest,
                    correlation_id=case.correlation_id,
                    status="success",
                )
            if attempt + 1 < self._evidence_attempts:
                await asyncio.sleep(self._evidence_delay_seconds)
        raise N8nEvidenceError("n8n did not return matching execution evidence")

    @classmethod
    def _replace_webhook_placeholder(cls, value: Any, webhook_path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if item == "{{CAPTAIN_WEBHOOK_PATH}}":
                    value[key] = webhook_path
                else:
                    cls._replace_webhook_placeholder(item, webhook_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if item == "{{CAPTAIN_WEBHOOK_PATH}}":
                    value[index] = webhook_path
                else:
                    cls._replace_webhook_placeholder(item, webhook_path)
