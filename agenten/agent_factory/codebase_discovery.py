"""Deterministic, read-only discovery of reusable Factory components."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agenten.agent_runtime.contracts import ArtifactRef, SHA256_PATTERN

from .forge_contracts import DocumentationQuery
from .input_compiler import CompiledFactorySpecification
from .skill_evaluation import ToolGapMarker, ToolImplementationOption
from .skill_workflow_contracts import CodebaseInventoryV1, FactorySkillInvocationV1


_REVISION_PATTERN = r"^[0-9a-f]{40}$"
_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "artifacts",
        "build",
        "dist",
        "node_modules",
    }
)
_SEARCH_GLOBS = (
    "*.py",
    "*.json",
    "*.yaml",
    "*.yml",
    "*.md",
    "*.txt",
    "requirements*.txt",
    "pyproject.toml",
)
_SEMANTIC_PATTERN = (
    r"(?i)(autogen|Swarm|AssistantAgent|model[_ -]?client|system[_ -]?prompt|"
    r"user[_ -]?prompt|memory|termination|handoff|BaseModel|tool|n8n|test_|"
    r"\$schema|architecture|TODO_TOOL\.v1|def\s+(?:build|main|run))"
)
_AUTOGEN_VERSION_PATTERN = re.compile(
    r"(?im)^\s*autogen-(?:agentchat|core|ext)(?:\[[^\]]+\])?\s*"
    r"(?:==|~=|>=)\s*([0-9]+(?:\.[0-9]+){1,3})"
)
_PYTHON_SYMBOL_PATTERN = re.compile(
    r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)"
)


class WorktreeObservation(BaseModel):
    """Safe, minimal worktree state recorded during inspection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: str = Field(pattern=_REVISION_PATTERN)
    relative_name: str = Field(min_length=1)
    branch: str | None = None
    dirty: bool

    @field_validator("relative_name")
    @classmethod
    def require_safe_relative_name(cls, value: str) -> str:
        return _safe_relative_path(value, allow_dot=True)


class SourceMatch(BaseModel):
    """Content-addressed semantic search observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1)
    line: int = Field(ge=1, strict=True)
    symbol: str | None = None
    content_sha256: str = Field(pattern=SHA256_PATTERN)

    @field_validator("relative_path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        return _safe_relative_path(value)


class RepositoryInspectionPort(Protocol):
    def revision(self) -> str: ...

    def worktrees(self) -> tuple[WorktreeObservation, ...]: ...

    def search(self, pattern: str, globs: tuple[str, ...]) -> tuple[SourceMatch, ...]: ...

    def read_text(self, relative_path: PurePosixPath) -> str: ...


class DocumentationDiscoveryPort(Protocol):
    def resolve(self, query: DocumentationQuery) -> tuple[ArtifactRef, ...]: ...


class ToolCatalogPort(Protocol):
    def match(
        self,
        capability_key: str,
        reusable_component_ids: tuple[str, ...],
    ) -> tuple[str, ...]: ...


class DiscoveryEvidenceStore(Protocol):
    def seal(self, kind: str, payload: object) -> ArtifactRef: ...


class FilesystemRepositoryInspection:
    """Read-only filesystem adapter constrained to one assigned repository root."""

    def __init__(self, root: Path, *, revision: str) -> None:
        resolved_root = root.resolve()
        if not resolved_root.is_dir():
            raise ValueError("repository root must be an existing directory")
        if re.fullmatch(_REVISION_PATTERN, revision) is None:
            raise ValueError("repository revision must be a full lowercase commit digest")
        self._root = resolved_root
        self._revision = revision
        self._read_paths: set[str] = set()

    @property
    def read_paths(self) -> tuple[str, ...]:
        """Expose safe paths for deterministic adapter verification."""

        return tuple(sorted(self._read_paths))

    def revision(self) -> str:
        return self._revision

    def worktrees(self) -> tuple[WorktreeObservation, ...]:
        return (
            WorktreeObservation(
                revision=self._revision,
                relative_name=".",
                branch=None,
                dirty=False,
            ),
        )

    def search(self, pattern: str, globs: tuple[str, ...]) -> tuple[SourceMatch, ...]:
        compiled = re.compile(pattern)
        matches: list[SourceMatch] = []
        for relative_path in self._candidate_paths(globs):
            content = self.read_text(PurePosixPath(relative_path))
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            for line_number, line in enumerate(content.splitlines(), start=1):
                if compiled.search(line) is None:
                    continue
                symbol_match = _PYTHON_SYMBOL_PATTERN.match(line)
                matches.append(
                    SourceMatch(
                        relative_path=relative_path,
                        line=line_number,
                        symbol=symbol_match.group(1) if symbol_match else None,
                        content_sha256=digest,
                    )
                )
        return tuple(
            sorted(
                set(matches),
                key=lambda item: (
                    item.relative_path,
                    item.line,
                    item.symbol or "",
                    item.content_sha256,
                ),
            )
        )

    def read_text(self, relative_path: PurePosixPath) -> str:
        normalized = _safe_relative_path(str(relative_path).replace("\\", "/"))
        if _is_excluded(PurePosixPath(normalized)):
            raise ValueError("repository path is excluded from inspection")
        target = (self._root / Path(*PurePosixPath(normalized).parts)).resolve()
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("repository path escapes assigned scope") from exc
        if target.is_symlink() or not target.is_file():
            raise ValueError("repository path must identify an in-scope regular file")
        self._read_paths.add(normalized)
        return target.read_text(encoding="utf-8")

    def _candidate_paths(self, globs: tuple[str, ...]) -> tuple[str, ...]:
        paths: set[str] = set()
        for glob in globs:
            if Path(glob).is_absolute() or ".." in PurePosixPath(glob).parts:
                raise ValueError("repository search globs must stay within assigned scope")
            for candidate in self._root.rglob(glob):
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                relative = PurePosixPath(candidate.relative_to(self._root).as_posix())
                if _is_excluded(relative):
                    continue
                paths.add(relative.as_posix())
        return tuple(sorted(paths))


class CodebaseDiscoveryService:
    """Build a deterministic inventory from semantic repository evidence."""

    def __init__(
        self,
        repository: RepositoryInspectionPort,
        documentation: DocumentationDiscoveryPort,
        tool_catalog: ToolCatalogPort,
        evidence_store: DiscoveryEvidenceStore,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._documentation = documentation
        self._tool_catalog = tool_catalog
        self._evidence_store = evidence_store
        self._clock = clock

    def discover(
        self,
        invocation: FactorySkillInvocationV1,
        specification: CompiledFactorySpecification,
    ) -> CodebaseInventoryV1:
        self._validate_bindings(invocation, specification)
        revision = self._repository.revision()
        if re.fullmatch(_REVISION_PATTERN, revision) is None:
            raise ValueError("repository returned an invalid revision")

        matches = self._repository.search(_SEMANTIC_PATTERN, _SEARCH_GLOBS)
        contents = {
            path: self._repository.read_text(PurePosixPath(path))
            for path in sorted({match.relative_path for match in matches})
        }
        categories = self._categorize(contents)
        reusable_ids = self._reusable_component_ids(categories)
        source_refs = self._source_refs(categories, contents)
        autogen_version = self._autogen_version(contents)
        documentation_refs = self._documentation_refs(
            specification=specification,
            autogen_version=autogen_version,
        )
        tool_matches = tuple(
            sorted(
                set(
                    self._tool_catalog.match(
                        specification.capability_key,
                        reusable_ids,
                    )
                )
            )
        )
        gap_refs = self._gap_refs(
            contents=contents,
            specification=specification,
            tool_matches=tool_matches,
        )

        worktree_ref = self._evidence_store.seal(
            "worktrees",
            [item.model_dump(mode="json") for item in self._repository.worktrees()],
        )
        search_ref = self._evidence_store.seal(
            "semantic-search",
            [item.model_dump(mode="json") for item in matches],
        )
        summary_payload = {
            "schema": "hermes.factory-codebase-discovery-summary.v1",
            "revision": revision,
            "categories": sorted(categories),
            "paths": sorted(contents),
            "reusable_component_ids": list(reusable_ids),
            "tool_catalog_match_ids": list(tool_matches),
            "gap_count": len(gap_refs),
        }
        artifact_ref = self._evidence_store.seal("inventory", summary_payload)
        evidence_refs = _unique_refs(
            (worktree_ref, search_ref, artifact_ref) + documentation_refs + gap_refs
        )

        return CodebaseInventoryV1.model_validate(
            {
                "schema": "hermes.factory-codebase-inventory.v1",
                "invocation": invocation,
                "invocation_id": invocation.invocation_id,
                "job_id": invocation.job_id,
                "correlation_id": invocation.correlation_id,
                "subject_version": invocation.subject_version,
                "attempt": invocation.attempt,
                "occurred_at": self._clock(),
                "producer": "hermes",
                "artifact_ref": artifact_ref,
                "evidence_refs": evidence_refs,
                "acceptance_assertion_ids": invocation.acceptance_assertion_ids,
                "inspected_revision": revision,
                "source_refs": source_refs,
                "reusable_component_ids": reusable_ids,
                "entrypoint_refs": self._category_refs("entrypoint", categories, contents),
                "test_refs": self._category_refs("test", categories, contents),
                "schema_refs": self._category_refs("schema", categories, contents),
                "autogen_version": autogen_version,
                "documentation_refs": documentation_refs,
                "tool_catalog_match_ids": tool_matches,
                "gap_refs": gap_refs,
            }
        )

    @staticmethod
    def _validate_bindings(
        invocation: FactorySkillInvocationV1,
        specification: CompiledFactorySpecification,
    ) -> None:
        if invocation.step.value != "discover":
            raise ValueError("codebase discovery requires a discover invocation")
        if invocation.subject_version != specification.subject_version:
            raise ValueError("compiled specification subject version does not match invocation")
        if invocation.input_ref != specification.source_ref:
            raise ValueError("compiled specification source does not match invocation")
        if invocation.acceptance_assertion_ids != specification.assertion_ids:
            raise ValueError("compiled specification assertions do not match invocation")

    @staticmethod
    def _categorize(contents: dict[str, str]) -> dict[str, tuple[str, ...]]:
        categories: dict[str, set[str]] = {}
        for path, content in contents.items():
            lowered = content.lower()
            path_lower = path.lower()
            tests = {
                "autogen": "autogen" in lowered,
                "entrypoint": bool(
                    re.search(r"(?m)^\s*(?:async\s+)?def\s+(?:build\w*|main|run)\s*\(", content)
                    or "if __name__" in lowered
                ),
                "model_client": "model_client" in lowered or "chatcompletionclient" in lowered,
                "prompt": "prompt" in lowered or "/prompts/" in f"/{path_lower}",
                "memory": "memory" in lowered,
                "termination": "termination" in lowered,
                "handoff": "handoff" in lowered,
                "typed_tool": (
                    path_lower.endswith(".py")
                    and "tool" in lowered
                    and ("basemodel" in lowered or "typeddict" in lowered)
                ),
                "n8n": "n8n" in lowered or "/n8n/" in f"/{path_lower}",
                "test": path_lower.startswith("tests/") or "/test_" in f"/{path_lower}",
                "schema": "$schema" in lowered or "/schemas/" in f"/{path_lower}",
                "architecture": "architecture" in path_lower,
                "tool_gap": path_lower.endswith(".json") and "todo_tool.v1" in lowered,
            }
            for category, present in tests.items():
                if present:
                    categories.setdefault(category, set()).add(path)
        return {
            category: tuple(sorted(paths))
            for category, paths in sorted(categories.items())
        }

    @staticmethod
    def _reusable_component_ids(
        categories: dict[str, tuple[str, ...]],
    ) -> tuple[str, ...]:
        reusable_paths = set(categories.get("entrypoint", ())) | set(
            categories.get("typed_tool", ())
        )
        identifiers = {
            path[:-3].replace("/", ".")
            for path in reusable_paths
            if path.endswith(".py") and not path.startswith("tests/")
        }
        return tuple(sorted(identifiers))

    @staticmethod
    def _source_refs(
        categories: dict[str, tuple[str, ...]],
        contents: dict[str, str],
    ) -> tuple[ArtifactRef, ...]:
        refs = tuple(
            _source_ref(category, path, contents[path])
            for category, paths in sorted(categories.items())
            for path in paths
        )
        if not refs:
            raise ValueError("semantic discovery found no reusable source evidence")
        return _unique_refs(refs)

    @staticmethod
    def _category_refs(
        category: str,
        categories: dict[str, tuple[str, ...]],
        contents: dict[str, str],
    ) -> tuple[ArtifactRef, ...]:
        return tuple(
            _source_ref(category, path, contents[path])
            for path in categories.get(category, ())
        )

    @staticmethod
    def _autogen_version(contents: dict[str, str]) -> str:
        versions = {
            match.group(1)
            for path, content in contents.items()
            if path == "pyproject.toml" or path.startswith("requirements")
            for match in _AUTOGEN_VERSION_PATTERN.finditer(content)
        }
        if not versions:
            raise ValueError("installed AutoGen version was not discovered")
        if len(versions) != 1:
            raise ValueError("conflicting AutoGen versions were discovered")
        return versions.pop()

    def _documentation_refs(
        self,
        *,
        specification: CompiledFactorySpecification,
        autogen_version: str,
    ) -> tuple[ArtifactRef, ...]:
        queries = [
            DocumentationQuery(
                ecosystem="autogen",
                package_id="autogen-agentchat",
                installed_version=autogen_version,
                query="AutoGen agent teams, handoffs, memory, and termination APIs",
                required=True,
            )
        ]
        if _has_n8n_intent(specification):
            queries.append(
                DocumentationQuery(
                    ecosystem="n8n",
                    package_id="n8n",
                    installed_version="declared",
                    query="n8n typed workflow contracts and execution APIs",
                    required=True,
                )
            )
        return _unique_refs(
            tuple(ref for query in queries for ref in self._documentation.resolve(query))
        )

    def _gap_refs(
        self,
        *,
        contents: dict[str, str],
        specification: CompiledFactorySpecification,
        tool_matches: tuple[str, ...],
    ) -> tuple[ArtifactRef, ...]:
        markers: list[ToolGapMarker] = []
        accepted = set(specification.assertion_ids)
        for path in sorted(set(self._categorize(contents).get("tool_gap", ()))):
            try:
                payload = json.loads(contents[path])
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid TODO_TOOL.v1 JSON at {path}") from exc
            marker = ToolGapMarker.model_validate(payload)
            if set(marker.acceptance_assertion_ids) - accepted:
                raise ValueError("TODO_TOOL.v1 contains unknown Captain assertions")
            markers.append(marker)

        if not tool_matches:
            markers.append(self._required_gap(specification))

        marker_ids = [marker.gap_id for marker in markers]
        if len(marker_ids) != len(set(marker_ids)):
            raise ValueError("semantic discovery found duplicate tool gap IDs")
        return tuple(
            self._evidence_store.seal(
                "todo-tool",
                marker.model_dump(mode="json", by_alias=True),
            )
            for marker in sorted(markers, key=lambda item: item.gap_id)
        )

    def _required_gap(
        self,
        specification: CompiledFactorySpecification,
    ) -> ToolGapMarker:
        assertion_id = specification.assertion_ids[0]
        options = [
            ToolImplementationOption(
                option_id="reuse-released-tool",
                description="Reuse a Captain-released tool with the required typed contract.",
                acceptance_assertion_id=assertion_id,
            ),
            ToolImplementationOption(
                option_id="implement-typed-local-adapter",
                description=(
                    "Implement a typed local adapter with schema, auth, health, and "
                    "idempotency tests."
                ),
                acceptance_assertion_id=assertion_id,
            ),
        ]
        if _has_n8n_intent(specification):
            options.append(
                ToolImplementationOption(
                    option_id="implement-typed-n8n-integration",
                    description=(
                        "Provide a typed n8n integration under an approved "
                        "capability lease."
                    ),
                    acceptance_assertion_id=assertion_id,
                )
            )
        rationale_ref = self._evidence_store.seal(
            "required-tool-gap-rationale",
            {
                "capability_key": specification.capability_key,
                "acceptance_assertion_ids": list(specification.assertion_ids),
                "reason": "no released tool catalog match",
            },
        )
        return ToolGapMarker.model_validate(
            {
                "schema": "TODO_TOOL.v1",
                "gap_id": f"missing-{specification.capability_key}",
                "severity": "required",
                "input_contract_ref": _contract_ref(
                    specification.capability_key,
                    "input",
                ),
                "output_contract_ref": _contract_ref(
                    specification.capability_key,
                    "output",
                ),
                "least_privilege_capability": f"{specification.capability_key}.execute",
                "implementation_options": options,
                "acceptance_assertion_ids": specification.assertion_ids,
                "evidence_ref": rationale_ref,
                "status": "unresolved",
            }
        )


def _safe_relative_path(value: str, *, allow_dot: bool = False) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or normalized.startswith("/") or path.is_absolute():
        raise ValueError("repository path must be relative")
    if re.match(r"^[A-Za-z]:", normalized) or ".." in path.parts:
        raise ValueError("repository path must stay within assigned scope")
    if normalized == "." and allow_dot:
        return normalized
    if normalized in {".", ""}:
        raise ValueError("repository path must identify a relative file")
    return path.as_posix()


def _is_excluded(path: PurePosixPath) -> bool:
    return any(part in _EXCLUDED_PARTS for part in path.parts) or any(
        part == ".env" or part.startswith(".env.") for part in path.parts
    )


def _source_ref(category: str, path: str, content: str) -> ArtifactRef:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    encoded_path = quote(path, safe="/")
    media_type = "application/json" if path.endswith(".json") else "text/plain"
    return ArtifactRef(
        uri=f"artifact://factory-discovery/source/{category}/{encoded_path}/{digest}",
        sha256=digest,
        media_type=media_type,
    )


def _contract_ref(capability_key: str, direction: str) -> ArtifactRef:
    identity = f"{capability_key}:{direction}:contract.v1"
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return ArtifactRef(
        uri=f"artifact://factory-discovery/contracts/{capability_key}/{direction}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _has_n8n_intent(specification: CompiledFactorySpecification) -> bool:
    return any(node.kind == "n8n_workflow" for node in specification.work_nodes)


def _unique_refs(refs: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
    unique = {
        (ref.uri, ref.sha256, ref.media_type): ref
        for ref in refs
    }
    return tuple(unique[key] for key in sorted(unique))
