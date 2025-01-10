import aiohttp
from urllib.parse import urlparse

async def search_and_score(self, query):
    """
    Perform a search, score results, and extract relevant content.

    Args:
        query (str): Search query.

    Returns:
        list: Scored search results.
    """
    results = await self.search(query)  # Perform the search
    scored_results = []

    async with aiohttp.ClientSession() as session:
        for result in results:
            url = result["link"]
            # Validate the URL
            parsed_url = urlparse(url)
            if not (parsed_url.scheme in ["http", "https"] and parsed_url.netloc):
                print(f"Skipping invalid URL: {url}")
                continue

            try:
                # Extract content from valid URLs
                content = await self.extract_text_from_url(url, session)
                score = self.score_relevance(query, content)
                scored_results.append({"title": result["title"], "link": url, "score": score})
            except Exception as e:
                print(f"Error processing URL {url}: {e}")
    
    # Sort by relevance score
    return sorted(scored_results, key=lambda x: x["score"], reverse=True)
