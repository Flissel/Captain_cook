"""Executor port and deterministic implementation for householder work."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agenten.household.roles import HouseholderRoleSpec


class HouseholderExecutionError(Exception):
    """An executor failure with an explicit retry decision."""

    def __init__(self, message: str, retriable: bool = True) -> None:
        self.retriable = retriable
        super().__init__(message)


@dataclass(frozen=True)
class HouseholderReport:
    """A JSON-safe, audit-friendly result emitted by every householder."""

    role: str
    decision: str
    artifacts: tuple[str, ...]
    evidence: tuple[str, ...]
    limitations: tuple[str, ...]

    def as_result(self) -> dict[str, object]:
        return {
            "role": self.role,
            "decision": self.decision,
            "artifacts": list(self.artifacts),
            "evidence": list(self.evidence),
            "limitations": list(self.limitations),
        }


class HouseholderExecutor(Protocol):
    """Port to be implemented later by an LLM/MCP-backed delivery adapter."""

    async def run(
        self,
        role: HouseholderRoleSpec,
        subproblem_id: str,
        description: str,
    ) -> HouseholderReport:
        """Return one structured report or raise ``HouseholderExecutionError``."""


class DeterministicHouseholderExecutor:
    """Offline executor used by tests and the local Devpost demo.

    It deliberately does not interpret the Markdown prompt as a command and
    does not invoke an LLM, MCP server, browser, filesystem mutation, or
    deployment.  A future live executor can implement ``HouseholderExecutor``
    while keeping the worker, pipeline, and ledger contracts unchanged.
    """

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

        return HouseholderReport(
            role=role.role_id,
            decision="offline_review_completed",
            artifacts=(role.prompt_path.as_posix(),),
            evidence=("deterministic offline executor", f"subproblem:{subproblem_id}"),
            limitations=("No LLM, MCP server, browser, or deployment was invoked.",),
        )
