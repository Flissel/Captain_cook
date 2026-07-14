"""Generic engine for AutoGen nested-chat pipelines.

Every existing hand-rolled workflow in this project (project definition,
project structuring, system-prompt generation, subtask decomposition) was
the same shape: create a Generator/Critic/(StructuredOutput)/UserProxy
agent, wire them into a nested_chat_queue, register it, kick it off, and
read the last message. NestedChatWorkflow captures that shape once so a
new agent pipeline is added by describing its roles and steps, not by
copy-pasting the wiring.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

Message = Union[str, Callable[..., str]]


@dataclass
class AgentRoleSpec:
    """One agent participating in a workflow."""

    role: str
    system_message: str = ""
    kind: str = "assistant"  # "assistant" | "user_proxy"


@dataclass
class WorkflowStep:
    """One entry in the nested chat queue, addressed by role name."""

    recipient_role: str
    message: Message
    summary_method: str = "last_msg"
    max_turns: int = 1
    carryover: Optional[str] = None


@dataclass
class NestedChatWorkflow:
    """A declarative nested-chat pipeline runnable against any CaptainAgent."""

    name: str
    roles: List[AgentRoleSpec]
    steps: List[WorkflowStep]
    entry_role: str = "generator"
    trigger_role: str = "user_proxy"
    kickoff_message: Message = "Start."
    context_defaults: Dict[str, Any] = field(default_factory=dict)
    result_index: int = -1  # which chat_history entry to return (some workflows want the second-to-last)

    def _render(self, message: Message, context: Dict[str, Any]) -> Any:
        if callable(message):
            return message
        return message.format(**context)

    def run(self, captain, context: Optional[Dict[str, Any]] = None) -> str:
        """Create the workflow's agents on ``captain`` and run the pipeline.

        Returns the last message produced by the chat.
        """
        ctx = {**self.context_defaults, **(context or {})}
        agents = {}
        for spec in self.roles:
            agent_name = f"{self.name}:{spec.role}"
            system_message = self._render(spec.system_message, ctx) if spec.system_message else ""
            if spec.kind == "user_proxy":
                agents[spec.role] = captain.create_agent_user_proxy(agent_name, system_message)
            else:
                agents[spec.role] = captain.create_agent_assistant(agent_name, system_message)

        chat_queue = []
        for step in self.steps:
            if step.recipient_role not in agents:
                raise KeyError(
                    f"Workflow '{self.name}': step references unknown role '{step.recipient_role}'. "
                    f"Known roles: {list(agents)}"
                )
            entry = {
                "recipient": agents[step.recipient_role],
                "message": self._render(step.message, ctx),
                "summary_method": step.summary_method,
                "max_turns": step.max_turns,
            }
            if step.carryover:
                entry["carryover"] = self._render(step.carryover, ctx)
            chat_queue.append(entry)

        entry_agent = agents[self.entry_role]
        trigger_agent = agents[self.trigger_role]
        entry_agent.register_nested_chats(chat_queue=chat_queue, trigger=trigger_agent)

        trigger_message = trigger_agent.initiate_chat(
            recipient=entry_agent,
            message={"content": self._render(self.kickoff_message, ctx)},
            max_turns=1,
        )

        if not trigger_message.chat_history:
            raise RuntimeError(f"Workflow '{self.name}' produced no chat history.")
        return trigger_message.chat_history[self.result_index]["content"]


def reflection_message(recipient, messages, sender, config):
    """Reusable nested-chat message: ask the recipient to critique the last turn."""
    history = sender.chat_messages_for_summary(recipient)
    if not history:
        return "No content to reflect on."
    return f"Reflect on: {history[-1]['content']}"


def update_message(recipient, messages, sender, config):
    """Reusable nested-chat message: ask the recipient to incorporate the critique."""
    history = sender.chat_messages_for_summary(recipient)
    if not history:
        return "Provide updates based on prior discussions."
    return f"Update based on critique: {history[-1]['content']}"
