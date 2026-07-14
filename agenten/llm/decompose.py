"""Real LLM-backed `llm_decompose` for
`agenten.decomposition.decomposer.DecomposerAgent`.

`DecomposerAgent` expects `llm_decompose(description, depth) -> List[Dict]`
where each dict has `description` (str), `capability_tags` (List[str]) and
`atomic` (bool). `DecomposerAgent` already enforces its own depth/fanout/
progress-invariant safety nets on whatever candidates come back from here --
this module's only job is to get a validated set of raw candidates out of
the model and propagate any API/parsing failure honestly (raise, never
silently swallow into `[]`).
"""
import logging
from typing import Any, Awaitable, Callable, Dict, List

from autogen_agentchat.agents import AssistantAgent
from autogen_core.models import ChatCompletionClient
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class SubproblemCandidate(BaseModel):
    description: str
    capability_tags: List[str]
    atomic: bool


class DecomposeResponse(BaseModel):
    subproblems: List[SubproblemCandidate]


def _build_system_message(known_capability_tags: List[str]) -> str:
    tags_list = ", ".join(known_capability_tags) if known_capability_tags else "(none provided)"
    return (
        "You are a decomposition planner for a supply-chain problem-solving "
        "system. Given a problem description (and its current decomposition "
        "depth), break it into a SMALL number of minimal, independently "
        "actionable subproblems.\n\n"
        "Rules:\n"
        "- Prefer as few subproblems as reasonably possible; do not "
        "over-fragment.\n"
        "- Each subproblem must be strictly narrower/more specific than the "
        "problem it came from -- restating the same problem verbatim (or at "
        "greater length) is not a valid subproblem.\n"
        "- Tag each subproblem with exactly one capability tag chosen from "
        f"this fixed set: {tags_list}. Never invent a tag outside this set.\n"
        "- Mark a subproblem atomic=true if it is a leaf-level task that can "
        "be executed directly by a single capable worker without further "
        "decomposition. Otherwise mark it atomic=false.\n\n"
        "Respond with the structured subproblems list only."
    )


def make_llm_decompose(
    model_client: ChatCompletionClient,
    known_capability_tags: List[str],
) -> Callable[[str, int], Awaitable[List[Dict[str, Any]]]]:
    """Build a real `llm_decompose` callable backed by `model_client`.

    `known_capability_tags` is the caller-controlled, injectable set of
    valid capability tags for the caller's domain -- this module does not
    hardcode a tag vocabulary of its own.

    A fresh `AssistantAgent` is constructed on every call (sharing the
    injected `model_client`) so that conversation history from one
    decomposition call never bleeds into the next -- `DecomposerAgent`
    invokes this callable repeatedly, at different depths/branches, over the
    lifetime of a single injected `model_client`.
    """
    system_message = _build_system_message(known_capability_tags)

    async def llm_decompose(description: str, depth: int) -> List[Dict[str, Any]]:
        agent = AssistantAgent(
            name="decomposer",
            model_client=model_client,
            system_message=system_message,
            output_content_type=DecomposeResponse,
        )
        task = f"{description}\n\n(current decomposition depth: {depth})"
        result = await agent.run(task=task)

        if not result.messages:
            logger.error("llm_decompose: model returned no messages for depth=%d", depth)
            raise ValueError("llm_decompose: model returned no messages")

        final_message = result.messages[-1]
        content = getattr(final_message, "content", None)
        if not isinstance(content, DecomposeResponse):
            logger.error(
                "llm_decompose: expected a DecomposeResponse structured message at "
                "depth=%d, got %r",
                depth,
                type(content),
            )
            raise ValueError(
                "llm_decompose: expected a DecomposeResponse structured "
                f"message, got {type(content)!r}"
            )

        return [candidate.model_dump() for candidate in content.subproblems]

    return llm_decompose
