"""Generate a system prompt for a task via generator/critic reflection."""
from .base import AgentRoleSpec, NestedChatWorkflow, WorkflowStep, reflection_message, update_message
from .registry import register_workflow


@register_workflow("system_prompt")
def build() -> NestedChatWorkflow:
    return NestedChatWorkflow(
        name="system_prompt",
        roles=[
            AgentRoleSpec("generator", "You create system prompts."),
            AgentRoleSpec("critic", "You critique the generated system prompts."),
            AgentRoleSpec("user_proxy", "You are the user proxy and orchestrate the task.", kind="user_proxy"),
        ],
        steps=[
            WorkflowStep(recipient_role="critic", message=reflection_message, summary_method="reflection_with_llm"),
            WorkflowStep(recipient_role="generator", message=update_message),
            WorkflowStep(recipient_role="critic", message=reflection_message, summary_method="reflection_with_llm"),
            WorkflowStep(recipient_role="generator", message=update_message),
        ],
        entry_role="generator",
        trigger_role="user_proxy",
        kickoff_message="Create a system prompt for the task: {task_description}.",
    )
