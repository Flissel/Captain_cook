"""Real LLM-backed `HouseholderExecutor` for
`agenten.household.worker.HouseholderWorker`.

`DeterministicHouseholderExecutor` (agenten/household/executor.py) stays the
offline default used by tests and the local Devpost demo -- it deliberately
invokes no LLM. This module is the second, optional implementation of the
same `HouseholderExecutor` port, following the pattern `agenten.llm.judge`
already established for the constitution gatekeeper: take an injected
`autogen_core.models.ChatCompletionClient` (built by
`agenten.llm.model_client.build_model_client`), issue one
`model_client.create(messages, json_output=...)` call, and parse the result
with `Model.model_validate_json`.

Tool boundary this executor does NOT cross: it never invokes a tool itself,
it only asks a model for a structured report. A role's `permitted_tools`
(agenten/household/roles.py) is therefore passed into the prompt as the only
tools the role may *claim* to have used, and the response is checked against
that list before being trusted -- see the comment at that check for why this
is a claim check on the model's self-report, not a call-boundary sandbox.
"""
import logging
from typing import List

from autogen_core.models import ChatCompletionClient, SystemMessage, UserMessage
from pydantic import BaseModel, Field, ValidationError

from agenten.household.executor import HouseholderExecutionError, HouseholderReport
from agenten.household.roles import HouseholderRoleSpec
from agenten.llm.resilience import LlmSchemaError, LlmStage, LlmStageError, run_llm_stage

logger = logging.getLogger(__name__)


class HouseholderReportModel(BaseModel):
    """Structured shape asked of the model for one householder report.

    `role` is deliberately absent from this schema: the caller (this
    module), not the model, is the authoritative source for which role
    produced the report -- see `LlmHouseholderExecutor.run`.
    """

    decision: str
    artifacts: List[str] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)
    tools_used: List[str] = Field(default_factory=list)


def _build_system_message(role: HouseholderRoleSpec, prompt_text: str) -> str:
    permitted = ", ".join(role.permitted_tools)
    return (
        f"You are the Captain Cook householder role {role.role_id!r}. Perform the "
        "role described below for exactly one assigned subproblem, then respond "
        "with one structured report only.\n\n"
        f"--- Role definition ({role.prompt_path.name}) ---\n"
        f"{prompt_text}\n"
        "--- End role definition ---\n\n"
        f"You may report `tools_used` ONLY from this exact list, and only tools "
        f"you genuinely used for this subproblem: {permitted}. Never claim a tool "
        "outside this list.\n\n"
        "Respond with the structured report only."
    )


def _build_user_message(subproblem_id: str, description: str) -> str:
    return f"Subproblem id: {subproblem_id}\n\nDescription:\n{description}"


class LlmHouseholderExecutor:
    """`HouseholderExecutor` backed by one injected `ChatCompletionClient`.

    `create_householder_worker_factories` shares one executor instance
    across all four household roles, so `role` is supplied per `run()` call
    rather than at construction time.
    """

    def __init__(
        self,
        model_client: ChatCompletionClient,
        *,
        timeout_seconds: float = 30.0,
        max_attempts: int = 2,
    ) -> None:
        self._model_client = model_client
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts

    async def run(
        self,
        role: HouseholderRoleSpec,
        subproblem_id: str,
        description: str,
    ) -> HouseholderReport:
        if not subproblem_id:
            raise HouseholderExecutionError("householder execution requires a subproblem_id", retriable=False)
        if not description.strip():
            raise HouseholderExecutionError("householder execution requires a description", retriable=False)

        try:
            prompt_text = role.prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HouseholderExecutionError(
                f"householder {role.role_id!r} prompt could not be read from {role.prompt_path}: {exc}",
                retriable=False,
            ) from exc

        messages = [
            SystemMessage(content=_build_system_message(role, prompt_text)),
            UserMessage(content=_build_user_message(subproblem_id, description), source="user"),
        ]

        async def invoke() -> HouseholderReportModel:
            result = await self._model_client.create(messages, json_output=HouseholderReportModel)
            content = result.content
            if not isinstance(content, str):
                logger.error(
                    "householder %r: expected a JSON string response, got %r", role.role_id, type(content)
                )
                raise LlmSchemaError(
                    LlmStage.HOUSEHOLDER_EXECUTE,
                    f"expected a JSON string response, got {type(content)!r}",
                )
            try:
                return HouseholderReportModel.model_validate_json(content)
            except ValidationError as exc:
                raise LlmSchemaError(
                    LlmStage.HOUSEHOLDER_EXECUTE,
                    f"model response failed schema validation: {exc}",
                ) from exc

        try:
            parsed = await run_llm_stage(
                LlmStage.HOUSEHOLDER_EXECUTE,
                invoke,
                timeout_seconds=self._timeout_seconds,
                max_attempts=self._max_attempts,
            )
        except LlmStageError as exc:
            # A timeout and a schema failure are both retriable -- the model
            # may simply answer correctly on the next attempt.
            raise HouseholderExecutionError(
                f"householder {role.role_id!r} LLM execution failed: {exc}", retriable=True
            ) from exc

        claimed_tools = tuple(parsed.tools_used)
        unpermitted = [tool for tool in claimed_tools if tool not in role.permitted_tools]
        if unpermitted:
            # Claim check, not a call boundary: this executor never invokes a
            # tool itself, so there is no sandbox here to enforce. This only
            # catches the model asserting -- in its own structured
            # self-report -- that it used a tool its role definition does
            # not permit. A future tool-invoking executor implementation
            # would need its own real enforcement at the actual call
            # boundary; this check must not be mistaken for that.
            raise HouseholderExecutionError(
                f"householder {role.role_id!r} claimed unpermitted tools {unpermitted}; "
                f"permitted: {list(role.permitted_tools)}",
                retriable=True,
            )

        evidence = tuple(parsed.evidence) + tuple(f"tool_used:{tool}" for tool in claimed_tools)

        return HouseholderReport(
            role=role.role_id,
            decision=parsed.decision,
            artifacts=tuple(parsed.artifacts),
            evidence=evidence,
            limitations=tuple(parsed.limitations),
        )
