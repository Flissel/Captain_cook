from autogen import ConversableAgent

class Reviewer2(ConversableAgent):
    def __init__(self, name="Reviewer2", llm_config=None):
        super().__init__(
            name=name,
            llm_config=llm_config,
            human_input_mode="ALWAYS",  # Allow feedback review
        )
        print(f"{self.name} initialized as a Conversable Reviewer 2 Agent.")