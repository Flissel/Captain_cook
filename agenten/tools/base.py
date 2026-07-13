"""Tool interface for capabilities agents can invoke (search, scoring, ...).

A new tool is added by implementing ``Tool`` and calling
``CaptainAgent.register_tool(tool)`` — Captain never needs a dedicated
``create_<tool>`` method the way ``create_internet_searcher`` used to be.
"""
from abc import ABC, abstractmethod
from typing import Any, Optional


class Tool(ABC):
    """A named, callable capability that can be registered on a CaptainAgent."""

    name: str

    @abstractmethod
    async def run(self, *args: Any, **kwargs: Any) -> Any:
        """Execute the tool and return its result."""


class ToolRegistry:
    """Keeps track of the tools registered on a CaptainAgent instance."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, name: Optional[str] = None) -> Tool:
        self._tools[name or tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"No tool registered as '{name}'. Known tools: {list(self._tools)}") from None

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def names(self):
        return list(self._tools)
