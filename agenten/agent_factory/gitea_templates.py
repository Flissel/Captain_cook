"""Read-only, digest-verifying access to one configured Gitea origin."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

import httpx

from agenten.agent_factory.gitea_template_contracts import (
    GiteaTemplateReleaseV1,
    validate_safe_https_url,
)
from agenten.agent_runtime.contracts import ArtifactRef


class GiteaTemplateError(RuntimeError):
    """A normalized template retrieval failure without provider diagnostics."""


@dataclass(frozen=True, repr=False)
class VerifiedTemplatePayload:
    """Verified bytes kept process-local; repr never exposes workflow content."""

    ref: ArtifactRef
    content: bytes


def _origin_key(parts: SplitResult) -> tuple[str, str, int]:
    assert parts.hostname is not None
    return parts.scheme, parts.hostname.lower(), parts.port or 443


class GiteaTemplateClient:
    """Fetch templates from one origin and expose only content-addressed refs."""

    def __init__(
        self,
        *,
        origin: str,
        http: httpx.AsyncClient,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 1_048_576,
    ) -> None:
        try:
            validate_safe_https_url(origin, label="Gitea origin")
        except ValueError:
            raise ValueError("Gitea origin must be a safe HTTPS origin") from None
        origin_parts = urlsplit(origin)
        if origin_parts.path not in {"", "/"}:
            raise ValueError("Gitea origin must not contain a path")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("template timeout must be between 0 and 60 seconds")
        if not 0 < max_response_bytes <= 10_485_760:
            raise ValueError("template response size limit must be between 1 and 10485760 bytes")
        self._origin = _origin_key(origin_parts)
        self._origin_url = origin.rstrip("/")
        self._http = http
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_response_bytes = max_response_bytes

    async def fetch_verified_template(
        self,
        release: GiteaTemplateReleaseV1,
    ) -> ArtifactRef:
        """Return a reference only after exact response bytes match the release."""

        return (await self.fetch_verified_payload(release)).ref

    async def fetch_verified_payload(
        self,
        release: GiteaTemplateReleaseV1,
    ) -> VerifiedTemplatePayload:
        """Return exact verified bytes for an in-process workflow materializer."""

        if _origin_key(urlsplit(release.contents_url)) != self._origin:
            raise GiteaTemplateError("template URL does not match configured Gitea origin")
        canonical_url = (
            f"{self._origin_url}/{release.repository}/raw/commit/"
            f"{release.revision}/{release.path}"
        )
        if release.contents_url != canonical_url:
            raise GiteaTemplateError("template URL does not match release metadata")

        try:
            async with self._http.stream(
                "GET",
                release.contents_url,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise GiteaTemplateError("template request failed")
                if _origin_key(urlsplit(str(response.url))) != self._origin:
                    raise GiteaTemplateError("template request left configured Gitea origin")

                declared_size = response.headers.get("content-length")
                if declared_size is not None:
                    try:
                        if int(declared_size) > self._max_response_bytes:
                            raise GiteaTemplateError("template response exceeded size limit")
                    except ValueError:
                        raise GiteaTemplateError("template response metadata was invalid") from None

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self._max_response_bytes:
                        raise GiteaTemplateError("template response exceeded size limit")
                    body.extend(chunk)
        except GiteaTemplateError:
            raise
        except httpx.TimeoutException:
            raise GiteaTemplateError("template request timed out") from None
        except httpx.HTTPError:
            raise GiteaTemplateError("template request failed") from None

        digest = hashlib.sha256(body).hexdigest()
        if digest != release.sha256:
            raise GiteaTemplateError("template digest mismatch")
        return VerifiedTemplatePayload(
            ref=ArtifactRef(
                uri=f"artifact://gitea/{digest}",
                sha256=digest,
                media_type="application/octet-stream",
            ),
            content=bytes(body),
        )
