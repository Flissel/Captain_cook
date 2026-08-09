"""Deployable loopback entrypoint for the Captain provider budget proxy."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
import uvicorn

from .app import create_provider_proxy_app
from .codex_config import write_isolated_codex_config
from .contracts import OpenAIPriceCardV1, ProviderBudgetContextV1, RuntimeProviderPolicyV1
from .repository import SQLiteProviderBudgetRepository
from .service import ProviderBudgetProxy
from .transports import GatewayProviderSettlementHTTPClient, OpenAIResponsesHTTPClient


_REQUIRED_SECRETS = (
    "OPENAI_API_KEY",
    "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
    "CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN",
    "CAPTAIN_RUNTIME_TOKEN",
)
_ISOLATED_SECRETS = _REQUIRED_SECRETS + (
    "CAPTAIN_GATEWAY_TOKEN",
    "CAPTAIN_WORKER_GATEWAY_TOKEN",
)


def require_distinct_authority_secrets(environ: Mapping[str, str]) -> None:
    for key in _REQUIRED_SECRETS:
        if not environ.get(key, "").strip():
            raise ValueError(f"{key} is required")
    present = [environ[key] for key in _ISOLATED_SECRETS if environ.get(key, "").strip()]
    if len(present) != len(set(present)):
        raise ValueError("provider, proxy, Gateway, and Runtime secrets must be distinct")


def _load_contract(path: Path, contract_type):
    try:
        payload = json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"contract file is unavailable: {path.name}") from exc
    return contract_type.model_validate(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Captain's bounded Responses proxy")
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--price-card", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8090")
    parser.add_argument("--port", type=int, default=18091)
    parser.add_argument("--codex-home", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    require_distinct_authority_secrets(os.environ)
    context = _load_contract(args.context, ProviderBudgetContextV1)
    card = _load_contract(args.price_card, OpenAIPriceCardV1)
    policy = _load_contract(args.policy, RuntimeProviderPolicyV1)
    state_root = args.state_root.resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    http = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0))
    provider = OpenAIResponsesHTTPClient(
        base_url="https://api.openai.com/v1",
        api_key=os.environ["OPENAI_API_KEY"],
        client=http,
    )
    gateway = GatewayProviderSettlementHTTPClient(
        base_url=args.gateway_url,
        provider_token=os.environ["CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN"],
        client=http,
    )
    from datetime import datetime, timezone

    proxy = ProviderBudgetProxy(
        context=context,
        policy=policy,
        price_card=card,
        repository=SQLiteProviderBudgetRepository(state_root / "budget.sqlite3"),
        cas_root=state_root / "cas",
        provider=provider,
        gateway=gateway,
        clock=lambda: datetime.now(timezone.utc),
    )
    if args.codex_home is not None:
        write_isolated_codex_config(
            root=args.codex_home,
            proxy_base_url=f"http://127.0.0.1:{args.port}/v1",
        )
    app = create_provider_proxy_app(
        proxy=proxy,
        client_token=os.environ["CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN"],
        runtime_finalize_token=os.environ["CAPTAIN_RUNTIME_TOKEN"],
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, log_level="warning")
    return 0
