from autogen import ConversableAgent

class Reviewer1(ConversableAgent):
    def __init__(self, name="Reviewer1", llm_config=None):
        super().__init__(
            name=name,
            llm_config=llm_config,
            human_input_mode="ALWAYS",  # Allow feedback review
        )
        print(f"{self.name} initialized as a Conversable Reviewer 1 Agent.")