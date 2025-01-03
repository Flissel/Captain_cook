from autogen import ConversableAgent

class CoordinatorAgent(ConversableAgent):
    def __init__(self, name="Coordinator", llm_config=None):
        super().__init__(
            name=name,
            silent=True,
            llm_config=llm_config,
            human_input_mode="NEVER",  
            is_termination_msg=lambda msg: "terminate" in msg["content"].lower(),
        )
        print(f"{self.name} initialized as a Conversable Coordinator Agent.")
