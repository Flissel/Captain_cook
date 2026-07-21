"""Captain-owned contracts and lifecycle policies for generated agent teams."""

from .contracts import (
    AgentFactoryJob,
    AgentFactoryJobV2,
    AgentFactoryJobV3,
    FactoryJob,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryLease,
    FactoryPhase,
    FactoryRole,
    PromotedCapability,
    parse_factory_job,
)
from .execution_policy import (
    FactoryExecutionMode,
    FactoryExecutionPolicyV1,
    FactoryLiveCapability,
    FactorySandboxMode,
)
from .job_builder import build_factory_job_v3
from .input_contracts import FactoryInputDocumentV2
from .input_document import FactoryInputError, load_factory_input, parse_factory_input_bytes
from .service import FactoryCoordinator, FactoryRepository, InMemoryFactoryRepository

__all__ = [
    "AgentFactoryJob",
    "AgentFactoryJobV2",
    "AgentFactoryJobV3",
    "FactoryJob",
    "FactoryBlockStatus",
    "FactoryEvidenceBlock",
    "FactoryLease",
    "FactoryPhase",
    "FactoryRole",
    "FactoryExecutionMode",
    "FactoryExecutionPolicyV1",
    "FactoryLiveCapability",
    "FactorySandboxMode",
    "PromotedCapability",
    "FactoryCoordinator",
    "FactoryRepository",
    "InMemoryFactoryRepository",
    "FactoryInputDocumentV2",
    "FactoryInputError",
    "load_factory_input",
    "parse_factory_input_bytes",
    "parse_factory_job",
    "build_factory_job_v3",
]
