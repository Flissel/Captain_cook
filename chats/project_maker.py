"""Compatibility entry points for the project-definition workflow.

The former implementation used the removed ``pyautogen`` nested-chat API.
The canonical workflow now lives in ``agenten.workflows.project_definition``
and is reached through ``agenten.project_definer``.
"""


def setup_captain_nested_chats(project_description, captain):
    """Return ``captain`` for callers that still perform explicit setup."""
    del project_description
    return captain


def execute_project_definition(project_description, captain):
    """Run the current AgentChat-backed project-definition workflow."""
    from agenten.project_definer import execute_project_definition as execute

    return execute(project_description, captain)
