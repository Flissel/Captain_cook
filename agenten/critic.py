from autogen import AssistantAgent

class CriticAgent(AssistantAgent):
    def __init__(self, name, llm_config, system_message, human_input_mode="NEVER", is_termination_msg = None):
        super().__init__(name=name, llm_config=llm_config,system_message = system_message, human_input_mode= human_input_mode, is_termination_msg = is_termination_msg)
