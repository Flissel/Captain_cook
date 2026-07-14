"""Real LLM-backed `llm_judge` for
`agenten.constitution.gatekeeper.ConstitutionGatekeeper`.

`ConstitutionGatekeeper` already wraps this call with its own timeout and a
conservative-reject-on-failure/timeout policy (see
`agenten/constitution/gatekeeper.py::_run_llm_judge`) -- this module's only
job is to make the one LLM call and return `True`/`False`, or raise on a
genuine API/parsing error and let the caller's existing conservative-reject
handling take it from there. No timeout logic is duplicated here.
"""
import logging
from typing import Awaitable, Callable

from autogen_core.models import ChatCompletionClient, SystemMessage, UserMessage
from pydantic import BaseModel

from agenten.constitution.ruleset import ConstitutionRuleset

logger = logging.getLogger(__name__)


class JudgeVerdict(BaseModel):
    accept: bool
    reason: str = ""


_SYSTEM_MESSAGE = (
    "You are the constitution gatekeeper for a supply-chain problem-solving "
    "system. You are given a candidate subproblem description plus the "
    "ruleset it must comply with. Decide whether to ACCEPT or REJECT the "
    "subproblem on semantic/quality grounds: is it in scope, and does it "
    "meet the quality rubric? Deterministic/malformed-input checks (empty "
    "description, near-duplicates, etc.) are already handled elsewhere; "
    "judge scope and quality only.\n\n"
    "Respond with the structured verdict only."
)


def _format_task(description: str, ruleset: ConstitutionRuleset) -> str:
    prohibited = ", ".join(ruleset.prohibited_topics) if ruleset.prohibited_topics else "(none)"
    return (
        f"Subproblem description:\n{description}\n\n"
        f"Scope statement:\n{ruleset.scope_statement}\n\n"
        f"Quality rubric:\n{ruleset.quality_rubric}\n\n"
        f"Prohibited topics: {prohibited}"
    )


def make_llm_judge(
    model_client: ChatCompletionClient,
) -> Callable[[str, ConstitutionRuleset], Awaitable[bool]]:
    """Build a real `llm_judge` callable backed by `model_client`.

    Issues a single structured-output `model_client.create(...)` call --
    a full `AssistantAgent` is unnecessary machinery for a one-shot yes/no
    judgement -- and parses the response into a `JudgeVerdict`.
    """

    async def llm_judge(description: str, ruleset: ConstitutionRuleset) -> bool:
        messages = [
            SystemMessage(content=_SYSTEM_MESSAGE),
            UserMessage(content=_format_task(description, ruleset), source="user"),
        ]
        result = await model_client.create(messages, json_output=JudgeVerdict)

        content = result.content
        if not isinstance(content, str):
            logger.error("llm_judge: expected a JSON string response, got %r", type(content))
            raise ValueError(f"llm_judge: expected a JSON string response, got {type(content)!r}")

        verdict = JudgeVerdict.model_validate_json(content)
        return verdict.accept

    return llm_judge
