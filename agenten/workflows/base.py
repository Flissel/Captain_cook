"""Declarative workflows backed by the current AutoGen AgentChat API.

The original implementation used ``register_nested_chats`` and
``UserProxyAgent`` from the legacy ``pyautogen`` package.  Those APIs are not
part of the supported AutoGen 0.7 stack.  The workflow description remains
compatible, while execution is now a sequential series of ``AssistantAgent``
``run`` calls with explicit output history.
"""

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

Message = Union[str, Callable[..., str]]


@dataclass
class AgentRoleSpec:
    """One agent participating in a workflow."""

    role: str
    system_message: str = ""
    kind: str = "assistant"  # ``user_proxy`` remains a compatibility role name.


@dataclass
class WorkflowStep:
    """One sequential workflow step, addressed by role name."""

    recipient_role: str
    message: Message
    summary_method: str = "last_msg"  # Retained for config compatibility.
    max_turns: int = 1
    carryover: Optional[str] = None


@dataclass
class NestedChatWorkflow:
    """A declarative workflow runnable against a CaptainAgent."""

    name: str
    roles: List[AgentRoleSpec]
    steps: List[WorkflowStep]
    entry_role: str = "generator"
    trigger_role: str = "user_proxy"  # Retained for config compatibility.
    kickoff_message: Message = "Start."
    context_defaults: Dict[str, Any] = field(default_factory=dict)
    result_index: int = -1

    def _render(self, message: Message, context: Dict[str, Any], history: List[str]) -> str:
        if callable(message):
            return message(history=history, context=context)
        return message.format(**context)

    @staticmethod
    async def _run_agent(agent: Any, task: str) -> str:
        """Run an AgentChat-compatible agent and return its final text."""
        result = agent.run(task=task)
        if inspect.isawaitable(result):
            result = await result

        messages = getattr(result, "messages", None)
        if not messages:
            raise RuntimeError("Workflow agent produced no messages.")

        content = getattr(messages[-1], "content", messages[-1])
        return content if isinstance(content, str) else str(content)

    async def run_async(self, captain, context: Optional[Dict[str, Any]] = None) -> str:
        """Run the workflow using current AutoGen AgentChat agents."""
        ctx = {**self.context_defaults, **(context or {})}
        agents = {}
        for spec in self.roles:
            # AutoGen AgentChat requires identifiers, unlike legacy
            # pyautogen which accepted names containing ':'.
            agent_name = f"{self.name}_{spec.role}"
            system_message = self._render(spec.system_message, ctx, []) if spec.system_message else ""
            if spec.kind == "user_proxy":
                agents[spec.role] = captain.create_agent_user_proxy(agent_name, system_message)
            else:
                agents[spec.role] = captain.create_agent_assistant(agent_name, system_message)

        if self.entry_role not in agents:
            raise KeyError(f"Workflow '{self.name}': unknown entry role '{self.entry_role}'.")

        history: List[str] = []
        kickoff = self._render(self.kickoff_message, ctx, history)
        history.append(await self._run_agent(agents[self.entry_role], kickoff))

        for step in self.steps:
            if step.recipient_role not in agents:
                raise KeyError(
                    f"Workflow '{self.name}': step references unknown role '{step.recipient_role}'. "
                    f"Known roles: {list(agents)}"
                )

            prompt_parts = []
            if step.carryover:
                prompt_parts.append(self._render(step.carryover, ctx, history))
            prompt_parts.append(self._render(step.message, ctx, history))
            if history:
                prompt_parts.append("Previous workflow outputs:\n" + "\n\n".join(history))
            prompt = "\n\n".join(prompt_parts)

            for _ in range(max(step.max_turns, 1)):
                history.append(await self._run_agent(agents[step.recipient_role], prompt))

        try:
            return history[self.result_index]
        except IndexError as exc:
            raise RuntimeError(
                f"Workflow '{self.name}' produced {len(history)} outputs; "
                f"result_index={self.result_index} is invalid."
            ) from exc

    def run(self, captain, context: Optional[Dict[str, Any]] = None) -> str:
        """Synchronously run the workflow for existing non-async callers."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run_async(captain, context=context))
        raise RuntimeError(
            "NestedChatWorkflow.run cannot be called inside a running event loop; use run_async."
        )


def reflection_message(*, history: List[str], context: Dict[str, Any]) -> str:
    """Ask a critic to review the latest workflow output."""
    if not history:
        return "No content to reflect on."
    return f"Reflect on: {history[-1]}"


def update_message(*, history: List[str], context: Dict[str, Any]) -> str:
    """Ask a generator to incorporate the latest critique."""
    if not history:
        return "Provide updates based on prior discussions."
    return f"Update based on critique: {history[-1]}"
