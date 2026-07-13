"""Thin, backward-compatible entry points onto the "project_definition" workflow."""
from .workflows.registry import get_workflow


def execute_project_definition(project_description, captain):
    """
    Refines a raw project idea into a clear, structured description.

    Args:
        project_description (str): The initial idea or description of the project.
        captain (CaptainAgent): The Captain agent managing the workflow.

    Returns:
        str: Final refined project definition.
    """
    return get_workflow("project_definition").run(
        captain, context={"project_description": project_description}
    )


# Kept as an alias: earlier revisions exposed a second, identically-behaved
# entry point under this name.
execute_project_definition_structured = execute_project_definition
