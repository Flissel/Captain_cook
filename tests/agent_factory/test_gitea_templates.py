from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import ValidationError

from agenten.agent_factory.gitea_template_contracts import GiteaTemplateReleaseV1
from agenten.agent_factory.gitea_templates import GiteaTemplateClient, GiteaTemplateError


ORIGIN = "https://gitea.internal.example"
REVISION = "1a2b3c4d5e6f789012345678901234567890abcd"
BODY = b'{"name":"renewal-team"}'
BODY_SHA256 = "48df0faf241567063c851b59532a14c0bc5decdd251cf192a1e2309f22c379bd"


def _release(**changes: str) -> GiteaTemplateReleaseV1:
    values = {
        "repository": "captain/templates",
        "revision": REVISION,
        "path": "teams/renewal.json",
        "contents_url": f"{ORIGIN}/captain/templates/raw/commit/{REVISION}/teams/renewal.json",
        "sha256": BODY_SHA256,
    }
    values.update(changes)
    return GiteaTemplateReleaseV1(**values)


@asynccontextmanager
async def _client(
    handler: httpx.AsyncBaseTransport,
    **kwargs: object,
) -> AsyncIterator[GiteaTemplateClient]:
    http = httpx.AsyncClient(transport=handler)
    try:
        yield GiteaTemplateClient(origin=ORIGIN, http=http, **kwargs)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_fetch_verified_template_returns_digest_only_artifact_reference() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == _release().contents_url
        return httpx.Response(200, content=BODY, request=request)

    async with _client(httpx.MockTransport(handler)) as client:
        result = await client.fetch_verified_template(_release())

    assert result.uri == f"artifact://gitea/{BODY_SHA256}"
    assert result.sha256 == BODY_SHA256
    assert result.media_type == "application/octet-stream"
    assert BODY.decode() not in result.model_dump_json()


@pytest.mark.asyncio
async def test_fetch_verified_payload_keeps_verified_bytes_out_of_repr() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY, request=request)

    async with _client(httpx.MockTransport(handler)) as client:
        result = await client.fetch_verified_payload(_release())

    assert result.ref.sha256 == BODY_SHA256
    assert result.content == BODY
    assert BODY.decode() not in repr(result)


@pytest.mark.asyncio
async def test_fetch_verified_template_rejects_changed_bytes() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"changed", request=request)

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(GiteaTemplateError, match="template digest mismatch"):
            await client.fetch_verified_template(_release())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "../captain/templates"),
        ("revision", "main"),
        ("revision", REVISION.upper()),
        ("path", "../secret.env"),
        ("path", "/absolute/template.json"),
        ("path", "teams\\template.json"),
        ("contents_url", "http://gitea.internal.example/template"),
        ("contents_url", "https://user:secret@gitea.internal.example/template"),
        ("contents_url", "https://gitea.internal.example/template?token=secret"),
        ("contents_url", "https://gitea.internal.example/template#fragment"),
        ("sha256", "A" * 64),
    ],
)
def test_release_rejects_unsafe_or_mutable_metadata(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        _release(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "captain/temp\x00lates"),
        ("revision", REVISION[:20] + "\x1f" + REVISION[21:]),
        ("path", "teams/renewal\x07.json"),
        (
            "contents_url",
            f"{ORIGIN}/captain/templates/raw/commit/{REVISION}/teams/renew\x00al.json",
        ),
        ("sha256", BODY_SHA256[:20] + "\x0b" + BODY_SHA256[21:]),
    ],
)
def test_release_rejects_c0_control_characters(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match="control"):
        _release(**{field: value})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "other/templates"),
        ("revision", "f" * 40),
        ("path", "teams/claims.json"),
    ],
)
async def test_fetch_verified_template_binds_metadata_to_exact_raw_url(
    field: str,
    value: str,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=BODY, request=request)

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(GiteaTemplateError, match="release metadata"):
            await client.fetch_verified_template(_release(**{field: value}))

    assert calls == 0


@pytest.mark.asyncio
async def test_fetch_verified_template_rejects_foreign_origin_before_network() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=BODY, request=request)

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(GiteaTemplateError, match="configured Gitea origin"):
            await client.fetch_verified_template(
                _release(contents_url=f"https://external.example/raw/{REVISION}/template")
            )

    assert calls == 0


@pytest.mark.asyncio
async def test_fetch_verified_template_rejects_redirect_without_following_it() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://external.example/secret"},
            request=request,
        )

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(GiteaTemplateError, match="template request failed") as error:
            await client.fetch_verified_template(_release())

    assert calls == [_release().contents_url]
    assert "external.example" not in str(error.value)


@pytest.mark.asyncio
async def test_fetch_verified_template_normalizes_non_success_status() -> None:
    secret = "provider-secret-diagnostic"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=secret.encode(), request=request)

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(GiteaTemplateError, match="template request failed") as error:
            await client.fetch_verified_template(_release())

    assert secret not in str(error.value)
    assert "503" not in str(error.value)


@pytest.mark.asyncio
async def test_fetch_verified_template_normalizes_timeout() -> None:
    secret = "credential-in-timeout"

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(secret, request=request)

    async with _client(httpx.MockTransport(handler), timeout_seconds=1.0) as client:
        with pytest.raises(GiteaTemplateError, match="template request timed out") as error:
            await client.fetch_verified_template(_release())

    assert secret not in str(error.value)


@pytest.mark.asyncio
async def test_fetch_verified_template_rejects_response_over_size_limit() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"12345", request=request)

    async with _client(httpx.MockTransport(handler), max_response_bytes=4) as client:
        with pytest.raises(GiteaTemplateError, match="template response exceeded size limit"):
            await client.fetch_verified_template(_release())


class _ChunkedStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"123"
        yield b"45"


@pytest.mark.asyncio
async def test_fetch_verified_template_rejects_chunked_response_over_size_limit() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_ChunkedStream(), request=request)

    async with _client(httpx.MockTransport(handler), max_response_bytes=4) as client:
        with pytest.raises(GiteaTemplateError, match="template response exceeded size limit"):
            await client.fetch_verified_template(_release())


@pytest.mark.asyncio
async def test_client_rejects_unsafe_configured_origin() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    try:
        with pytest.raises(ValueError, match="Gitea origin"):
            GiteaTemplateClient(origin="https://user:secret@gitea.internal.example", http=http)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_client_rejects_c0_control_character_in_configured_origin() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    try:
        with pytest.raises(ValueError, match="Gitea origin"):
            GiteaTemplateClient(origin="https://gitea.internal\x00.example", http=http)
    finally:
        await http.aclose()
