"""Create a disposable Codex home that can authenticate only to the proxy."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit


def write_isolated_codex_config(*, root: Path, proxy_base_url: str) -> Path:
    parsed = urlsplit(proxy_base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1"
    ):
        raise ValueError("Codex proxy base URL must be loopback HTTP ending in /v1")
    destination_root = root.resolve()
    destination_root.mkdir(parents=True, exist_ok=False)
    config = destination_root / "config.toml"
    config.write_text(
        "\n".join(
            (
                'model = "gpt-5.6-terra"',
                'model_provider = "captain_budget_proxy"',
                'model_reasoning_effort = "high"',
                "",
                "[model_providers.captain_budget_proxy]",
                'name = "Captain localhost budget proxy"',
                f'base_url = "{proxy_base_url.rstrip("/")}"',
                'env_key = "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN"',
                'env_http_headers = { "X-Captain-Provider-Context-Ref" = "CAPTAIN_PROVIDER_CONTEXT_REF" }',
                'wire_api = "responses"',
                "request_max_retries = 0",
                "stream_max_retries = 0",
                "",
            )
        ),
        encoding="utf-8",
        newline="\n",
    )
    return config
