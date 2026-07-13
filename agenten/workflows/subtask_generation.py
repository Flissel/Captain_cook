"""Decompose a task description into structured, dependency-aware subtasks."""
from .base import AgentRoleSpec, NestedChatWorkflow, WorkflowStep
from .registry import register_workflow


@register_workflow("subtask_generation")
def build() -> NestedChatWorkflow:
    return NestedChatWorkflow(
        name="subtask_generation",
        roles=[
            AgentRoleSpec("generator", "You generate a perfect system prompt to solve the given Task."),
            AgentRoleSpec("critic", "You critique the generated system prompt if it's solving the Task."),
            AgentRoleSpec("user_proxy", "You are the user proxy and orchestrate the task.", kind="user_proxy"),
        ],
        steps=[
            WorkflowStep(
                recipient_role="generator",
                message=(
                    "Decompose the following task into structured subtasks: {task_description}. "
                    "Ensure each subtask includes title, description, priority (High, Medium, Low), and dependencies."
                ),
            ),
            WorkflowStep(
                recipient_role="critic",
                message=(
                    "Please review the generated subtasks.\n"
                    "                              - Ensure the subtasks are clear, actionable, and well-structured.\n"
                    "                              - it must be in struct that llm can handle best."
                ),
            ),
            WorkflowStep(
                recipient_role="generator",
                message=(
                    "Refine the subtasks based on feedback from CriticAgent.\n"
                    "                              Ensure the subtasks are clear, actionable, and well-structured.\n"
                    "                              devide the SUBTASKS with '---------------------------------------'\n"
                    "                              Ensure all fields are filled correctly and dependencies are accurate."
                ),
            ),
        ],
        entry_role="generator",
        trigger_role="user_proxy",
        kickoff_message="Decompose the following task into structured subtasks: {task_description}.",
    )
