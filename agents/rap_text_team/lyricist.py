from autogen import ConversableAgent

class Lyricist(ConversableAgent):
    def __init__(self, name="Lyricist", llm_config=None):
        super().__init__(
            name=name,
            llm_config=llm_config,
            human_input_mode="NEVER",  # Fully autonomous
        )
        print(f"{self.name} initialized as a Conversable Lyricist Agent.")

    def generate_lyrics(self, theme, keywords):
        prompt = (
            f"Write rap lyrics about '{theme}' using the following keywords: {', '.join(keywords)}.\n"
            "Make sure the lyrics have clever rhymes, modern slang, and fit a confident tone."
        )
        response = self.send_message(prompt)
        print(f"Lyrics generated for theme '{theme}': {response}")
        return response
