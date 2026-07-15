from typing import Any, Dict, Optional

from autogen_agentchat.agents import AssistantAgent
from autogen_core.models import ChatCompletionClient

from .llm.model_client import build_model_client


class CriticAgent(AssistantAgent):
    """AutoGen 0.7 critic compatibility wrapper.

    ``human_input_mode`` and ``is_termination_msg`` were legacy constructor
    options. They are accepted so old callers can migrate incrementally, but
    termination is now owned by AgentChat team conditions.
    """

    def __init__(
        self,
        name: str,
        llm_config: Optional[Dict[str, Any]] = None,
        system_message: str = "",
        human_input_mode: str = "NEVER",
        is_termination_msg: Optional[Any] = None,
        model_client: Optional[ChatCompletionClient] = None,
    ):
        del human_input_mode, is_termination_msg
        config_list = (llm_config or {}).get("config_list", [])
        config = config_list[0] if config_list else {}
        client = model_client or build_model_client(
            api_key=config.get("api_key"),
            model=config.get("model"),
        )
        super().__init__(name=name, model_client=client, system_message=system_message)
