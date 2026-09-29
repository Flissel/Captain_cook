import os

import pytest
from autogen_core.models import UserMessage

from agenten.llm.model_client import build_model_client
from config.llm_provider import resolve_provider

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_nemotron_answers_through_token_factory():
    profile = resolve_provider(os.environ)
    if profile.name != "nebius" or profile.api_key is None:
        pytest.skip("NEBIUS_API_KEY not configured")
    client = build_model_client(profile=profile)
    try:
        result = await client.create([UserMessage(content="Reply with the word ok.", source="user")])
    finally:
        await client.close()
    assert "ok" in str(result.content).lower()
