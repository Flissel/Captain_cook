"""Extract subtasks from a system prompt via generator/critic reflection."""
from .base import AgentRoleSpec, NestedChatWorkflow, WorkflowStep, reflection_message, update_message
from .registry import register_workflow


@register_workflow("subtask_extraction")
def build() -> NestedChatWorkflow:
    return NestedChatWorkflow(
        name="subtask_extraction",
        roles=[
            AgentRoleSpec("generator", "You create subtasks based on the provided Tasks."),
            AgentRoleSpec("critic", "You review and provide feedback on the generated subtasks."),
            AgentRoleSpec(
                "user_proxy", "You orchestrate the task and manage nested chat flows.", kind="user_proxy"
            ),
        ],
        steps=[
            WorkflowStep(
                recipient_role="generator",
                message=(
                    "Decompose the following task into smaller subtasks: {system_prompt}. "
                    "Ensure each subtask includes a title, description, priority (High, Medium, Low), "
                    "and dependencies, if applicable."
                ),
            ),
            WorkflowStep(recipient_role="critic", message=reflection_message),
            WorkflowStep(recipient_role="generator", message=update_message, summary_method="reflection_with_llm"),
        ],
        entry_role="generator",
        trigger_role="user_proxy",
        kickoff_message="Please decompose the following system prompt into smaller subtasks: {system_prompt}.",
        result_index=-2,
    )
