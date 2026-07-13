"""Worker agents: consume SubproblemAssigned, do the work, publish
SubproblemCompleted/SubproblemFailed. See agenten/workers/base.py for the
shared WorkerAgent contract every worker type implements.
"""
from .base import WorkerAgent, WorkerExecutionError, make_routed_agent_class
from .echo_worker import EchoWorker
from .research_worker import ResearchWorker

__all__ = [
    "WorkerAgent",
    "WorkerExecutionError",
    "make_routed_agent_class",
    "EchoWorker",
    "ResearchWorker",
]
