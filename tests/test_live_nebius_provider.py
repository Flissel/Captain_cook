import pytest
from autogen_core.models import UserMessage

from agenten.llm.model_client import build_model_client
from config import llm_config

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_nemotron_answers_through_token_factory():
    # Same resolution as every real caller: process env over the local .env.
    # A selected live gate never skips (pytest.ini): misconfiguration fails.
    llm_config.reset_profile_cache()
    profile = llm_config.get_profile()
    if profile.name != "nebius":
        pytest.fail(
            f"live Nemotron smoke needs CAPTAIN_LLM_PROVIDER=nebius; configured: {profile.name}"
        )
    if profile.api_key is None:
        pytest.fail("NEBIUS_API_KEY is not configured in the environment or .env")
    client = build_model_client(profile=profile)
    try:
        result = await client.create([UserMessage(content="Reply with the word ok.", source="user")])
    finally:
        await client.close()
    assert "ok" in str(result.content).lower()
