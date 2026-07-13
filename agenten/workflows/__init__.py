"""Declarative nested-chat agent workflows.

Importing this package registers every built-in workflow (see the
``register_workflow`` calls in the sibling modules) so that
``agenten.workflows.registry.get_workflow(name)`` can find them.
"""
from . import (  # noqa: F401  (imported for their registration side effects)
    project_definition,
    project_structuring,
    system_prompt,
    subtask_extraction,
    subtask_generation,
)
from .base import AgentRoleSpec, NestedChatWorkflow, WorkflowStep
from .registry import get_workflow, list_workflows, register_workflow

__all__ = [
    "AgentRoleSpec",
    "NestedChatWorkflow",
    "WorkflowStep",
    "get_workflow",
    "list_workflows",
    "register_workflow",
]
