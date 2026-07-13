"""Worker that answers a subproblem by running a web search + relevance
scoring pass through the existing InternetSearchTool.
"""
from typing import Any, Dict, List

from .base import WorkerAgent, WorkerExecutionError

SEARCH_TOOL_NAME = "internet_searcher"


class ResearchWorker(WorkerAgent):
    agent_type = "research_worker"
    capability_tags = ["research", "web_search"]

    async def execute(self, subproblem_id: str, description: str) -> Dict[str, Any]:
        try:
            tool = self.tools.get(SEARCH_TOOL_NAME)
        except KeyError as exc:
            # Missing tool registration is a deployment/config problem, not a
            # transient one -- retrying without fixing the registry would
            # just fail again.
            raise WorkerExecutionError(str(exc), retriable=False) from exc

        try:
            scored_results = await tool.run(query=description)
        except Exception as exc:  # noqa: BLE001 - network/Selenium/scraping errors, all treated as transient
            raise WorkerExecutionError(
                f"internet search failed for subproblem {subproblem_id!r}: {exc}",
                retriable=True,
            ) from exc

        results: List[Dict[str, Any]] = list(scored_results or [])
        return {
            "query": description,
            "results": results,
            "top_result": results[0] if results else None,
            "result_count": len(results),
        }
