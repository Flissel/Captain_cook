"""Registry mapping a workflow name to a factory that builds it.

New agent logic is added by writing a module with a
``@register_workflow("my_workflow")`` factory function (see
project_definition.py for the pattern) — nothing else in the codebase has
to change to make it runnable via ``CaptainAgent.run_workflow("my_workflow", ...)``.
"""
from typing import Callable, Dict

from .base import NestedChatWorkflow

_REGISTRY: Dict[str, Callable[[], NestedChatWorkflow]] = {}


def register_workflow(name: str):
    def decorator(factory: Callable[[], NestedChatWorkflow]):
        _REGISTRY[name] = factory
        return factory

    return decorator


def get_workflow(name: str) -> NestedChatWorkflow:
    try:
        factory = _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"No workflow registered as '{name}'. Known workflows: {list(_REGISTRY)}"
        ) from None
    return factory()


def list_workflows():
    return list(_REGISTRY)
