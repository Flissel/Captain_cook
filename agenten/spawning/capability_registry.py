"""Maps capability tags (e.g. "research", "codegen") to the agent TYPE that
handles them. SpawnCoordinatorAgent (unit U4) resolves each accepted
subproblem's capability_tags through this registry to decide which worker
agent type to address — new specialized agents are added by registering a
tag, not by editing the coordinator.

Validated against the actually-registered runtime agent types at boot
(unit U11) rather than trusted blindly, so a registered-but-never-deployed
capability fails fast instead of dispatching into a void.
"""
from typing import Dict, List


class NoCapableAgentType(Exception):
    def __init__(self, capability_tags: List[str]):
        self.capability_tags = capability_tags
        super().__init__(f"No agent type registered for any of: {capability_tags}")


class CapabilityRegistry:
    def __init__(self):
        self._by_tag: Dict[str, str] = {}

    def register(self, capability_tag: str, agent_type: str) -> None:
        self._by_tag[capability_tag] = agent_type

    def resolve(self, capability_tags: List[str]) -> str:
        """Return the agent type for the first tag with a registered handler.

        Raises NoCapableAgentType if none of the given tags resolve.
        """
        for tag in capability_tags:
            if tag in self._by_tag:
                return self._by_tag[tag]
        raise NoCapableAgentType(capability_tags)

    def known_tags(self) -> List[str]:
        return list(self._by_tag)

    def registered_agent_types(self) -> List[str]:
        return sorted(set(self._by_tag.values()))
