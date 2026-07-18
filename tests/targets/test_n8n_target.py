from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from typing import Any
import httpx
import pytest

from agenten.agent_runtime.n8n_endpoint import resolve_n8n_endpoint
from agenten.targets.n8n import N8nEvidenceError, N8nHttpClient, N8nTarget, SealedArtifact, ValidationCase

class ProviderState:
    def __init__(self) -> None:
        self.workflow: dict[str, Any] | None = None
        self.execution_id = "execution-1"
        self.workflow_id = "workflow-1"
        self.execution_status = "success"
        self.execution_output = {"artifact_digest": "a" * 64, "correlation_id": "correlation-1"}
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

@pytest.fixture
def n8n_server():
    state = ProviderState()
    class Handler(BaseHTTPRequestHandler):
        def _json(self):
            length = int(self.headers.get("content-length", "0"))
            return json.loads(self.rfile.read(length)) if length else {}
        def _send(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status); self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, format, *args): return
        def do_GET(self):
            state.requests.append(("GET", self.path, None))
            if self.path.startswith("/api/v1/workflows"):
                self._send(200, {"data": [] if state.workflow is None else [state.workflow]}); return
            if self.path.startswith("/api/v1/executions/execution-1"):
                self._send(200, {"id":state.execution_id,"workflowId":state.workflow_id,"status":state.execution_status,"data":{"resultData":{"runData":{"Respond":[{"data":{"main":[[{"json":state.execution_output}]]}}]}}}}); return
            self._send(404, {"message":"not found"})
        def do_POST(self):
            payload = self._json(); state.requests.append(("POST", self.path, payload))
            if self.path == "/api/v1/workflows":
                state.workflow={"id":"workflow-1","name":payload["name"]}; self._send(201,state.workflow); return
            if self.path == "/api/v1/workflows/workflow-1/activate":
                self._send(200,{"id":"workflow-1","active":True}); return
            if self.path.startswith("/webhook/"):
                self._send(200,{"execution_id":"execution-1",**payload}); return
            self._send(404,{"message":"not found"})
        def do_PUT(self):
            payload=self._json(); state.requests.append(("PUT",self.path,payload))
            if self.path == "/api/v1/workflows/workflow-1":
                state.workflow={"id":"workflow-1","name":payload["name"]}; self._send(200,state.workflow); return
            self._send(404,{"message":"not found"})
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try: yield f"http://127.0.0.1:{server.server_port}",state
    finally: server.shutdown(); server.server_close(); thread.join(timeout=5)

def artifact():
    return SealedArtifact(artifact_id="harmless-workflow",artifact_digest="a"*64,namespace="captain-gate-a",workflow={"nodes":[{"name":"Respond","type":"n8n-nodes-base.respondToWebhook","typeVersion":1,"position":[0,0],"parameters":{}}],"connections":{},"settings":{}})


@pytest.mark.asyncio
async def test_http_client_is_constructed_from_selected_endpoint(n8n_server):
    base_url, state = n8n_server
    endpoint = resolve_n8n_endpoint(
        {
            "N8N_MODE": "captain-builder",
            "CAPTAIN_N8N_URL": base_url,
            "CAPTAIN_N8N_API_KEY": "local-test-key",
        }
    )

    async with httpx.AsyncClient() as http:
        client = N8nHttpClient.from_endpoint(endpoint, http)
        record = await client.create_or_update_workflow(
            name="captain::selected-endpoint",
            definition={"nodes": [], "connections": {}, "settings": {}},
        )

    assert record.id == "workflow-1"
    assert state.workflow == {
        "id": "workflow-1",
        "name": "captain::selected-endpoint",
    }

@pytest.mark.asyncio
async def test_target_deploys_executes_and_fetches_matching_real_http_evidence(n8n_server):
    base_url,state=n8n_server
    async with httpx.AsyncClient() as http:
        target=N8nTarget(N8nHttpClient(api_base_url=base_url,webhook_base_url=base_url,api_key="local-test-key",http=http))
        deployment=await target.deploy(artifact())
        evidence=await target.execute(deployment,ValidationCase(case_id="case-1",correlation_id="correlation-1",input_payload={"message":"ping"}))
    assert deployment.workflow_id=="workflow-1"
    assert deployment.workflow_name=="captain::captain-gate-a::harmless-workflow::aaaaaaaaaaaa"
    assert evidence.execution_id=="execution-1"
    assert evidence.workflow_id==deployment.workflow_id
    assert evidence.artifact_digest==artifact().artifact_digest
    assert evidence.correlation_id=="correlation-1"
    create=next(item for item in state.requests if item[:2]==("POST","/api/v1/workflows"))
    assert create[2]=={"name":deployment.workflow_name,"nodes":artifact().workflow["nodes"],"connections":{},"settings":{}}
    webhook=next(item for item in state.requests if item[0]=="POST" and item[1].startswith("/webhook/"))
    assert webhook[2]=={"artifact_digest":artifact().artifact_digest,"correlation_id":"correlation-1","case_id":"case-1","input":{"message":"ping"}}



@pytest.mark.asyncio
@pytest.mark.parametrize("identity_field", ["name", "id", "workflowId", "webhook_path"])
async def test_target_rejects_definition_identity_override_before_http(
    n8n_server,
    identity_field,
):
    base_url, state = n8n_server
    unsafe = artifact().model_copy(
        update={"workflow": {**artifact().workflow, identity_field: "attacker-value"}}
    )
    async with httpx.AsyncClient() as http:
        target = N8nTarget(
            N8nHttpClient(
                api_base_url=base_url,
                webhook_base_url=base_url,
                api_key="local-test-key",
                http=http,
            )
        )
        with pytest.raises(ValueError, match="provider identity"):
            await target.deploy(unsafe)
    assert state.requests == []



def test_gate_a_live_contract_is_fresh_supervised_and_causally_linked():
    source = (
        __import__("pathlib").Path("tests/live/test_gate_a_codex_n8n.py")
        .read_text(encoding="utf-8")
    )
    for required in (
        "uuid4()",
        "TemporaryDirectory",
        "CodexExecutionPolicy",
        "PowerShellCodexRunner",
        "CodexSupervisor",
        "GatewayCodexRunRepository",
        "workflow_path.read_bytes()",
        "hashlib.sha256(sealed_bytes)",
        "DeliveryEventEnvelope.model_validate",
        "gateway_client.delivery_events",
        "response.raise_for_status()",
        "primary_error.add_note(message)",
    ):
        assert required in source
    assert "GATE_A_GATEWAY_RUN_ID" not in source

@pytest.mark.asyncio
@pytest.mark.parametrize(("field","bad_value"),[("artifact_digest","b"*64),("correlation_id","wrong-correlation"),("execution_id","other-execution"),("workflow_id","other-workflow"),("execution_status","error")])
async def test_target_rejects_provider_success_without_matching_execution_evidence(n8n_server,field,bad_value):
    base_url,state=n8n_server
    if field in state.execution_output:
        state.execution_output[field]=bad_value
    else:
        setattr(state, field, bad_value)
    async with httpx.AsyncClient() as http:
        target=N8nTarget(N8nHttpClient(api_base_url=base_url,webhook_base_url=base_url,api_key="local-test-key",http=http))
        deployment=await target.deploy(artifact())
        with pytest.raises(N8nEvidenceError,match="matching execution evidence"):
            await target.execute(deployment,ValidationCase(case_id="case-1",correlation_id="correlation-1",input_payload={}))
