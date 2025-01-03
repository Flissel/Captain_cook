from autogen import ConversableAgent

class ToneSpecialist(ConversableAgent):
    def __init__(self, name="ToneSpecialist", llm_config=None):
        super().__init__(
            name=name,
            llm_config=llm_config,
            human_input_mode="NEVER",  # Fully autonomous
        )
        print(f"{self.name} initialized as a Conversable Tone Specialist Agent.")

    def adjust_tone(self, lyrics, tone):
        prompt = (
            f"Take these lyrics and adjust them to have a {tone} tone:\n\n{lyrics}\n\n"
            "Ensure the language is confident and modern."
        )
        response = self.send_message(prompt)
        print(f"Tone adjusted to '{tone}' for lyrics.")
        return response
