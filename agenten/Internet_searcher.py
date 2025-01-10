import asyncio
from sentence_transformers import SentenceTransformer, util
from bs4 import BeautifulSoup
import aiohttp

class InternetSearcher:
    def __init__(self, model_name="all-MiniLM-L6-v2"):
        self.model = SentenceTransformer(model_name)

    async def scrape_bing(self, query):
        url = f"https://www.bing.com/search?q={query}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                html = await response.text()
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for item in soup.select("li.b_algo"):
            title = item.select_one("h2").text
            link = item.select_one("a")["href"]
            snippet = item.select_one(".b_caption p").text if item.select_one(".b_caption p") else "No snippet available"
            results.append({"title": title, "link": link, "snippet": snippet})
        return results

    async def extract_text_from_url(self, url):
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                html = await response.text()
        soup = BeautifulSoup(html, "html.parser")
        paragraphs = soup.find_all("p")
        text = " ".join([p.get_text() for p in paragraphs])
        return text

    async def score_relevance(self, query, content):
        query_embedding = self.model.encode(query, convert_to_tensor=True)
        content_embedding = self.model.encode(content, convert_to_tensor=True)
        score = util.pytorch_cos_sim(query_embedding, content_embedding)
        return score.item()

    async def search_and_score(self, query):
        results = await self.scrape_bing(query)
        scored_results = []
        for result in results:
            content = await self.extract_text_from_url(result["link"])
            score = await self.score_relevance(query, content)
            scored_results.append({
                "title": result["title"],
                "link": result["link"],
                "score": score,
                "content": content
            })
        scored_results.sort(key=lambda x: x["score"], reverse=True)
        return scored_results
