from agenten.functions.relevance_scoring import score_relevance  # Relevanzbewertung
from agenten.functions.extract_content_from_url import extract_text_from_url  # Text-Extraktion aus URLs


class NestedChatForURLEvaluation:
    def __init__(self, urls, target_goal, llm_config=None):
        self.urls = urls
        self.target_goal = target_goal
        self.llm_config = llm_config or {"model": "gpt-4"}
        self.agents = {}
        self.captain = None

    def setup_agents(self):
        """Describe evaluation roles without constructing unused LLM agents."""
        self.captain = "URLCaptain"
        self.agents = {
            "URLProcessor": "Evaluate extracted URL content for relevance.",
            "Coordinator": "Prioritize the most relevant URL results.",
        }

    async def evaluate_urls(self):
        """
        Extrahiert Inhalte aus URLs, bewertet deren Relevanz und sortiert die Ergebnisse.
        """
        results = []

        # URLs durchgehen und Inhalte bewerten
        for url in self.urls:
            print(f"Processing: {url}")
            try:
                # Extrahiere Inhalt aus der URL
                content = await extract_text_from_url(url)  # Asynchron für große Websites
                content = content or ""
                relevance_score = score_relevance(self.target_goal, content)
                
                # Ergebnisse sammeln
                results.append({
                    "url": url,
                    "content_preview": content[:300],  # Vorschau auf den extrahierten Text
                    "score": relevance_score
                })
            except Exception as e:
                print(f"Error processing {url}: {e}")
        
        # Sortiere Ergebnisse nach Relevanz
        results.sort(key=lambda x: x["score"], reverse=True)
        return results

# Hauptfunktion zur Ausführung
async def main():
    urls = [
        
        "https://github.com/magenta/magenta",
        "https://huggingface.co/docs/transformers/main/en/audio",
        "https://deepmind.com/research/publications",
        "https://www.tensorflow.org/io/tutorials/audio",
        "https://paperswithcode.com/task/audio-generat",
        "https://openai.com/research/musenet",
        "https://towardsdatascience.com/",
        "https://ieeexplore.ieee.org/Xplore/home.jsp",
        "https://www.kaggle.com/learn/audio-data",
        "https://github.com/craffel/mir_eval",
    ]
    
    target_goal = "Achieving high-quality AI music synthesis"
    
    # Initialisiere Nested Chat
    nested_chat = NestedChatForURLEvaluation(urls, target_goal)
    nested_chat.setup_agents()

    # Ergebnisse auswerten
    results = await nested_chat.evaluate_urls()

    # Ausgabe der Ergebnisse
    for result in results[:5]:  # Nur Top 5 anzeigen
        print(f"URL: {result['url']}")
        print(f"Relevance Score: {result['score']}")
        print(f"Content Preview: {result['content_preview'][:500]}...\n")


