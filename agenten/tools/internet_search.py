"""InternetSearcher exposed through the generic Tool interface."""
from ..Internet_searcher import InternetSearcher
from .base import Tool


class InternetSearchTool(Tool):
    name = "internet_searcher"

    def __init__(self, searcher: InternetSearcher = None):
        self.searcher = searcher or InternetSearcher()

    async def run(self, query: str):
        return await self.searcher.search_and_score(query)
