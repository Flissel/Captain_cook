"""Credential-isolated HTTP transports for OpenAI and the Captain Gateway."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from .contracts import ProviderBudgetContextV1

from .service import ProviderReply, ProviderStreamReply


class OpenAIResponsesHTTPClient:
    def __init__(self, *, base_url: str, api_key: str, client: httpx.AsyncClient) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.openai.com"
            or parsed.port is not None
            or parsed.path.rstrip("/") != "/v1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("OpenAI base URL must be canonical https://api.openai.com/v1")
        if not api_key.strip():
            raise ValueError("OpenAI API key must not be blank")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    async def count_input_tokens(self, payload: dict[str, object]) -> int:
        response = await self._client.post(
            f"{self._base_url}/responses/input_tokens",
            headers=self._headers,
            json=payload,
        )
        self._require_success(response, "OpenAI input-token count")
        value = response.json().get("input_tokens")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("OpenAI input-token count response is invalid")
        return value

    async def create_response(self, payload: dict[str, object]) -> ProviderReply:
        response = await self._client.post(
            f"{self._base_url}/responses", headers=self._headers, json=payload
        )
        self._require_success(response, "OpenAI response creation")
        return self._reply(response)

    async def open_response_stream(
        self, payload: dict[str, object]
    ) -> ProviderStreamReply:
        request = self._client.build_request(
            "POST",
            f"{self._base_url}/responses",
            headers=self._headers,
            json=payload,
        )
        response = await self._client.send(request, stream=True)
        if not 200 <= response.status_code < 300:
            await response.aread()
            await response.aclose()
            raise RuntimeError(
                f"OpenAI response stream failed with HTTP {response.status_code}"
            )

        async def chunks():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()

        return ProviderStreamReply(
            response.status_code,
            {"content-type": response.headers.get("content-type", "text/event-stream")},
            chunks(),
        )

    async def read_response(self, response_id: str, query: str) -> ProviderReply:
        self._require_response_id(response_id)
        url = f"{self._base_url}/responses/{response_id}"
        if query:
            url = f"{url}?{query}"
        response = await self._client.get(url, headers=self._headers)
        self._require_success(response, "OpenAI response readback")
        return self._reply(response)

    async def cancel_response(self, response_id: str) -> ProviderReply:
        self._require_response_id(response_id)
        response = await self._client.post(
            f"{self._base_url}/responses/{response_id}/cancel", headers=self._headers
        )
        self._require_success(response, "OpenAI response cancellation")
        return self._reply(response)

    @staticmethod
    def _reply(response: httpx.Response) -> ProviderReply:
        return ProviderReply(
            status_code=response.status_code,
            headers={"content-type": response.headers.get("content-type", "application/json")},
            body=response.content,
        )

    @staticmethod
    def _require_success(response: httpx.Response, operation: str) -> None:
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"{operation} failed with HTTP {response.status_code}")

    @staticmethod
    def _require_response_id(value: str) -> None:
        if not value.startswith("resp_") or not value.replace("_", "").isalnum():
            raise ValueError("OpenAI response ID is invalid")


class GatewayProviderSettlementHTTPClient:
    def __init__(
        self, *, base_url: str, provider_token: str, client: httpx.AsyncClient
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.path not in {"", "/"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Gateway provider-settlement URL must be loopback HTTP(S)")
        if not provider_token.strip():
            raise ValueError("Gateway provider token must not be blank")
        self._base_url = base_url.rstrip("/")
        self._provider_token = provider_token
        self._client = client

    async def require_active_provider_authority(
        self, context: ProviderBudgetContextV1
    ) -> ProviderBudgetContextV1:
        response = await self._client.post(
            f"{self._base_url}/v1/capability-resumes/provider-authorities/preflight",
            headers={
                "Authorization": f"Bearer {self._provider_token}",
                "Content-Type": "application/json",
            },
            json=context.model_dump(mode="json", by_alias=True),
        )
        if response.status_code != 200:
            raise RuntimeError("Gateway provider authority is unavailable")
        try:
            returned = ProviderBudgetContextV1.model_validate(response.json())
        except (ValueError, TypeError):
            raise RuntimeError("Gateway provider authority is unavailable") from None
        if returned != context:
            raise RuntimeError("Gateway provider authority is unavailable")
        return returned

    async def resolve_provider_authority(
        self, reference: str
    ) -> ProviderBudgetContextV1:
        prefix = "gateway://provider-budget-contexts/"
        digest = reference.removeprefix(prefix) if reference.startswith(prefix) else ""
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise RuntimeError("Gateway provider authority is unavailable")
        response = await self._client.get(
            f"{self._base_url}/v1/capability-resumes/provider-authorities/by-context/{digest}",
            headers={"Authorization": f"Bearer {self._provider_token}"},
        )
        if response.status_code != 200:
            raise RuntimeError("Gateway provider authority is unavailable")
        try:
            returned = ProviderBudgetContextV1.model_validate(response.json())
        except (ValueError, TypeError):
            raise RuntimeError("Gateway provider authority is unavailable") from None
        if returned.sha256 != digest:
            raise RuntimeError("Gateway provider authority is unavailable")
        return returned

    async def record_provider_settlement(self, payload: dict[str, object]) -> None:
        response = await self._client.post(
            f"{self._base_url}/v1/capability-resumes/provider-settlements",
            headers={
                "Authorization": f"Bearer {self._provider_token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if response.status_code not in {200, 201}:
            raise RuntimeError(
                f"Gateway provider settlement failed with HTTP {response.status_code}"
            )
        receipt = payload.get("receipt")
        request_id = receipt.get("request_id") if isinstance(receipt, dict) else None
        try:
            response_payload = response.json()
        except ValueError:
            raise RuntimeError("Gateway provider settlement receipt is invalid") from None
        if (
            not isinstance(request_id, str)
            or not isinstance(response_payload, dict)
            or response_payload.get("operation_id") != request_id
            or type(response_payload.get("replayed")) is not bool
        ):
            raise RuntimeError("Gateway provider settlement receipt is invalid")
