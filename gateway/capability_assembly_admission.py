"""Production admission for repository-owned capability assembly inputs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from agenten.agent_factory.holdout_store import PrivateHoldoutStore
from agenten.agent_factory.input_compiler import FactoryInputCompiler
from agenten.agent_factory.input_document import (
    FactoryInputError,
    parse_factory_input_bytes,
)
from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_runtime.production_bootstrap import (
    RuntimeAdapterManifestError,
    read_verified_repository_bytes,
)
from gateway.capability_assembly_contracts import (
    CapabilityAssemblyAdmittedInput,
    CapabilityAssemblyCanonicalInput,
    CapabilityAssemblyInputRequest,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_PATTERN = re.compile(
    r"^\s*Pattern key:\s*(sequential|selector_group_chat|handoff_swarm)\b",
    flags=re.IGNORECASE | re.MULTILINE,
)
_PATTERN_NORMALIZATION = {
    "sequential": "sequential",
    "selector_group_chat": "selector_group_chat",
    "handoff_swarm": "swarm",
}


class CapabilityAssemblyAdmissionError(ValueError):
    """A repository input cannot be admitted without changing its authority."""


class RepositoryCapabilityAssemblyInputAdmission:
    """Single-open, Git-attested implementation of the assembly admission port."""

    def __init__(
        self,
        *,
        repository_root: Path,
        expected_repository_revision: str,
        expected_sha256_by_path: Mapping[str, str],
        compiled_tool_gaps_by_source_sha256: Mapping[
            str, tuple[ToolGapMarker, ...]
        ],
        holdout_store: PrivateHoldoutStore,
        subject_version: int,
    ) -> None:
        if _REVISION.fullmatch(expected_repository_revision) is None:
            raise ValueError("expected repository revision is invalid")
        if isinstance(subject_version, bool) or subject_version < 1:
            raise ValueError("subject_version must be a positive integer")
        if not callable(getattr(holdout_store, "put", None)):
            raise ValueError("holdout_store must implement put")
        configured: dict[str, str] = {}
        for path, digest in expected_sha256_by_path.items():
            canonical = _canonical_repository_path(path)
            if _SHA256.fullmatch(digest) is None:
                raise ValueError(f"expected source digest is invalid: {canonical}")
            configured[canonical] = digest
        if len(configured) != len(expected_sha256_by_path):
            raise ValueError("expected source paths are not unique")
        expected_source_digests = set(configured.values())
        if set(compiled_tool_gaps_by_source_sha256) != expected_source_digests:
            raise ValueError(
                "compiled tool-gap output must bind every expected source digest"
            )
        compiled_tool_gaps: dict[str, tuple[ToolGapMarker, ...]] = {}
        for digest, markers in compiled_tool_gaps_by_source_sha256.items():
            if _SHA256.fullmatch(digest) is None:
                raise ValueError("compiled tool-gap source digest is invalid")
            compiled_tool_gaps[digest] = tuple(
                ToolGapMarker.model_validate(marker) for marker in markers
            )

        self._repository_root = repository_root.resolve(strict=True)
        self._expected_repository_revision = expected_repository_revision
        self._expected_sha256_by_path = configured
        self._compiled_tool_gaps_by_source_sha256 = compiled_tool_gaps
        self._compiler = FactoryInputCompiler(holdout_store=holdout_store)
        self._subject_version = subject_version

    def admit(
        self,
        request: CapabilityAssemblyInputRequest,
    ) -> CapabilityAssemblyAdmittedInput:
        repository_path = _canonical_repository_path(request.repository_path)
        expected_digest = self._expected_sha256_by_path.get(repository_path)
        if expected_digest is None:
            raise CapabilityAssemblyAdmissionError(
                "repository input is not present in the digest-pinned admission set"
            )
        try:
            _, source = read_verified_repository_bytes(
                repository_path,
                expected_sha256=expected_digest,
                expected_repository_revision=self._expected_repository_revision,
                repository_root=self._repository_root,
            )
            document = parse_factory_input_bytes(
                source,
                logical_name="TO_BE_BUILT.md",
            )
            pattern = _conversation_pattern(source)
            compiled = self._compiler.compile(
                document,
                subject_version=self._subject_version,
            )
            canonical = CapabilityAssemblyCanonicalInput(
                input_id=request.input_id,
                conversation_pattern=pattern,
                document=document,
                compiled_specification=compiled,
                tool_gaps=self._compiled_tool_gaps_by_source_sha256[expected_digest],
            )
        except (FactoryInputError, RuntimeAdapterManifestError, ValueError) as exc:
            raise CapabilityAssemblyAdmissionError(str(exc)) from None
        return CapabilityAssemblyAdmittedInput(
            repository_path=repository_path,
            input=canonical,
        )


def _canonical_repository_path(value: str) -> str:
    if "\\" in value or re.match(r"^[A-Za-z]:", value):
        raise ValueError("repository-relative TO_BE_BUILT.md path is required")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.name != "TO_BE_BUILT.md"
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("repository-relative TO_BE_BUILT.md path is required")
    return path.as_posix()


def _conversation_pattern(source: bytes) -> str:
    text = source.decode("utf-8", errors="strict")
    matches = tuple(match.group(1).casefold() for match in _PATTERN.finditer(text))
    if len(matches) != 1:
        raise CapabilityAssemblyAdmissionError(
            "canonical input has a missing or ambiguous conversation pattern"
        )
    return _PATTERN_NORMALIZATION[matches[0]]


__all__ = [
    "CapabilityAssemblyAdmissionError",
    "RepositoryCapabilityAssemblyInputAdmission",
]
