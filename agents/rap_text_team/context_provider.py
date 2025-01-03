from autogen import ConversableAgent

class ContextProvider(ConversableAgent):
    def __init__(self, name="ContextProvider", llm_config=None):
        super().__init__(
            name=name,
            silent=True,
            llm_config=llm_config,
            human_input_mode="NEVER",  # Fully autonomous
            is_termination_msg=lambda msg: "terminate" in msg["content"].lower(),
        )
        print(f"{self.name} initialized as a Conversable Context_Provider Agent.")