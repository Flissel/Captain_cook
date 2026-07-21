"""Deterministic, read-only discovery of reusable Factory components."""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import re
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agenten.agent_runtime.contracts import ArtifactRef, SHA256_PATTERN

from .forge_contracts import DocumentationEvidence, DocumentationQuery
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
    r"\$schema|architecture|TODO_TOOL\.v1|__main__|def\s+(?:build|main|run))"
)
_SECRET_PATH_PATTERN = re.compile(
    r"(?i)(?:^|[._-])(?:api[_-]?key|credentials?|password|secrets?|tokens?)"
    r"(?:$|[._-])"
)


class WorktreeObservation(BaseModel):
    """Safe, minimal worktree state recorded during inspection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: str = Field(pattern=_REVISION_PATTERN)
    relative_name: str = Field(min_length=1)
    branch: str | None = None
    detached: bool = False
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
    def resolve(self, query: DocumentationQuery) -> DocumentationEvidence: ...


class GitWorktreeInspectionPort(Protocol):
    def observe(self, root: Path) -> tuple[WorktreeObservation, ...]: ...


class PackageMetadataPort(Protocol):
    def installed_version(self, distribution: str) -> str: ...


class ToolCatalogPort(Protocol):
    def match(
        self,
        capability_key: str,
        reusable_component_ids: tuple[str, ...],
    ) -> tuple[str, ...]: ...


class DiscoveryEvidenceStore(Protocol):
    def seal(self, kind: str, payload: object) -> ArtifactRef: ...


class SubprocessGitWorktreeInspection:
    """Observe the assigned worktree through fixed argv-only Git commands."""

    def observe(self, root: Path) -> tuple[WorktreeObservation, ...]:
        top_level = Path(self._run(root, "rev-parse", "--show-toplevel")).resolve()
        if top_level != root.resolve():
            raise ValueError("Git top-level does not match assigned repository root")
        records = _parse_git_worktree_porcelain(
            self._run(root, "worktree", "list", "--porcelain")
        )
        observations = []
        for record in records:
            worktree_root = Path(record["worktree"]).resolve()
            branch_ref = record.get("branch")
            branch = (
                branch_ref.removeprefix("refs/heads/")
                if branch_ref is not None
                else None
            )
            observations.append(
                WorktreeObservation(
                    revision=record["HEAD"],
                    relative_name=(
                        "."
                        if worktree_root == root.resolve()
                        else _registered_worktree_name(worktree_root)
                    ),
                    branch=branch,
                    detached="detached" in record,
                    dirty=bool(
                        self._run(worktree_root, "status", "--porcelain=v1")
                    ),
                )
            )
        if not any(item.relative_name == "." for item in observations):
            raise ValueError("Git worktree list omitted the assigned repository root")
        return tuple(observations)

    @staticmethod
    def _run(root: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        )
        return completed.stdout.strip()


class ImportlibPackageMetadata:
    """Read the installed runtime version from Python distribution metadata."""

    def installed_version(self, distribution: str) -> str:
        try:
            return importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError(f"installed package metadata is missing for {distribution}") from exc


class FilesystemRepositoryInspection:
    """Read-only filesystem adapter constrained to one assigned repository root."""

    def __init__(
        self,
        root: Path,
        *,
        expected_revision: str,
        git_worktrees: GitWorktreeInspectionPort | None = None,
    ) -> None:
        resolved_root = root.resolve()
        if not resolved_root.is_dir():
            raise ValueError("repository root must be an existing directory")
        if re.fullmatch(_REVISION_PATTERN, expected_revision) is None:
            raise ValueError("expected revision must be a full lowercase commit digest")
        self._root = resolved_root
        self._read_paths: set[str] = set()
        observer = git_worktrees or SubprocessGitWorktreeInspection()
        observations = tuple(observer.observe(self._root))
        if not observations:
            raise ValueError("Git worktree observation must not be empty")
        current = next(
            (item for item in observations if item.relative_name == "."),
            None,
        )
        if current is None:
            raise ValueError("Git worktree observation must include the assigned worktree")
        if current.revision != expected_revision:
            raise ValueError("caller revision mismatch with observed Git HEAD")
        self._revision = current.revision
        self._worktrees = observations

    @property
    def read_paths(self) -> tuple[str, ...]:
        """Expose safe paths for deterministic adapter verification."""

        return tuple(sorted(self._read_paths))

    def revision(self) -> str:
        return self._revision

    def worktrees(self) -> tuple[WorktreeObservation, ...]:
        return self._worktrees

    def search(self, pattern: str, globs: tuple[str, ...]) -> tuple[SourceMatch, ...]:
        compiled = re.compile(pattern)
        matches: list[SourceMatch] = []
        for relative_path in self._candidate_paths(globs):
            content = self.read_text(PurePosixPath(relative_path))
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            symbols_by_line = _python_symbols_by_line(relative_path, content)
            for line_number, line in enumerate(content.splitlines(), start=1):
                if compiled.search(line) is None:
                    continue
                matches.append(
                    SourceMatch(
                        relative_path=relative_path,
                        line=line_number,
                        symbol=symbols_by_line.get(line_number),
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
        unresolved = self._root / Path(*PurePosixPath(normalized).parts)
        if unresolved.is_symlink() or any(
            parent.is_symlink()
            for parent in unresolved.parents
            if parent != self._root and self._root in parent.parents
        ):
            raise ValueError("repository symlinks are excluded from inspection")
        target = unresolved.resolve()
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("repository path escapes assigned scope") from exc
        if not target.is_file():
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
        package_metadata: PackageMetadataPort | None = None,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._documentation = documentation
        self._tool_catalog = tool_catalog
        self._evidence_store = evidence_store
        self._package_metadata = package_metadata or ImportlibPackageMetadata()
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

        worktrees = _sorted_worktrees(self._repository.worktrees())
        assigned_worktree = next(
            (item for item in worktrees if item.relative_name == "."),
            None,
        )
        if assigned_worktree is None or assigned_worktree.revision != revision:
            raise ValueError("repository worktree evidence does not match observed revision")
        matches = _sorted_matches(
            self._repository.search(_SEMANTIC_PATTERN, _SEARCH_GLOBS)
        )
        contents = {
            path: self._repository.read_text(PurePosixPath(path))
            for path in sorted({match.relative_path for match in matches})
        }
        for match in matches:
            observed_digest = hashlib.sha256(
                contents[match.relative_path].encode("utf-8")
            ).hexdigest()
            if observed_digest != match.content_sha256:
                raise ValueError(
                    f"repository source changed during snapshot: {match.relative_path}"
                )
        categories = self._categorize(contents, matches)
        reusable_ids = self._reusable_component_ids(categories)
        source_refs = self._source_refs(categories, contents)
        autogen_version = self._package_metadata.installed_version("autogen-agentchat")
        if not autogen_version.strip():
            raise ValueError("installed AutoGen package metadata returned a blank version")
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
            [item.model_dump(mode="json") for item in worktrees],
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
            "symbols": sorted(
                {item.symbol for item in matches if item.symbol is not None}
            ),
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
        required_capabilities = {"repository.read", "context7.read"}
        missing = sorted(required_capabilities - set(invocation.lease.capabilities))
        if missing:
            raise PermissionError(
                f"discovery lease is missing required capability {missing[0]}"
            )

    @staticmethod
    def _categorize(
        contents: dict[str, str],
        matches: tuple[SourceMatch, ...],
    ) -> dict[str, tuple[str, ...]]:
        categories: dict[str, set[str]] = {}
        observed_symbols = {
            path: {match.symbol for match in matches if match.relative_path == path}
            for path in contents
        }
        for path, content in contents.items():
            path_lower = path.lower()
            present_categories = _path_categories(path_lower, content)
            if path_lower.endswith(".py"):
                present_categories.update(
                    _python_categories(
                        content,
                        observed_symbols.get(path, set()),
                    )
                )
            for category in present_categories:
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
                    installed_version="declared-intent",
                    query="n8n typed workflow contracts and execution APIs",
                    required=True,
                )
            )
        references: list[ArtifactRef] = []
        for query in queries:
            evidence = self._documentation.resolve(query)
            expected_query_sha256 = _documentation_query_sha256(query)
            if evidence.query != query:
                raise ValueError("documentation evidence query does not match request")
            if evidence.query_sha256 != expected_query_sha256:
                raise ValueError("documentation evidence query digest does not match request")
            if query.ecosystem == "autogen" and (
                evidence.retrieved_version.split(".")[:2]
                != autogen_version.split(".")[:2]
            ):
                raise ValueError("AutoGen documentation version does not match runtime")
            references.append(
                self._evidence_store.seal(
                    f"documentation-{query.ecosystem}",
                    evidence.model_dump(mode="json"),
                )
            )
        return _unique_refs(tuple(references))

    def _gap_refs(
        self,
        *,
        contents: dict[str, str],
        specification: CompiledFactorySpecification,
        tool_matches: tuple[str, ...],
    ) -> tuple[ArtifactRef, ...]:
        markers: list[ToolGapMarker] = []
        accepted = set(specification.assertion_ids)
        for path, payload in _tool_gap_payloads(contents):
            try:
                marker = ToolGapMarker.model_validate(payload)
            except ValueError as exc:
                raise ValueError(f"invalid TODO_TOOL.v1 marker at {path}") from exc
            if set(marker.acceptance_assertion_ids) - accepted:
                raise ValueError("TODO_TOOL.v1 contains unknown Captain assertions")
            markers.append(marker)

        if not tool_matches:
            markers.extend(self._required_gaps(specification))

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

    def _required_gaps(
        self,
        specification: CompiledFactorySpecification,
    ) -> tuple[ToolGapMarker, ...]:
        assertion_chunks = tuple(
            specification.assertion_ids[index : index + 3]
            for index in range(0, len(specification.assertion_ids), 3)
        )
        return tuple(
            self._required_gap(
                specification,
                assertion_ids=assertion_ids,
                part=index,
                part_count=len(assertion_chunks),
            )
            for index, assertion_ids in enumerate(assertion_chunks, start=1)
        )

    def _required_gap(
        self,
        specification: CompiledFactorySpecification,
        *,
        assertion_ids: tuple[str, ...],
        part: int,
        part_count: int,
    ) -> ToolGapMarker:
        blueprints = [
            (
                "reuse-released-tool",
                "Reuse a Captain-released tool with the required typed contract.",
            ),
            (
                "implement-typed-local-adapter",
                "Implement a typed local adapter with schema, auth, health, and "
                "idempotency tests.",
            ),
        ]
        if _has_n8n_intent(specification):
            blueprints.append(
                (
                    "implement-typed-n8n-integration",
                    "Provide a typed n8n integration under an approved capability lease.",
                )
            )
        while len(blueprints) < len(assertion_ids):
            index = len(blueprints) + 1
            blueprints.append(
                (
                    f"implement-typed-local-adapter-{index}",
                    "Implement a typed local adapter with schema, auth, health, and "
                    "idempotency tests.",
                )
            )
        options = [
            ToolImplementationOption(
                option_id=option_id,
                description=description,
                acceptance_assertion_id=assertion_ids[index % len(assertion_ids)],
            )
            for index, (option_id, description) in enumerate(blueprints)
        ]
        rationale_ref = self._evidence_store.seal(
            "required-tool-gap-rationale",
            {
                "capability_key": specification.capability_key,
                "acceptance_assertion_ids": list(assertion_ids),
                "reason": "no released tool catalog match",
                "part": part,
                "part_count": part_count,
            },
        )
        gap_id = f"missing-{specification.capability_key}"
        if part_count > 1:
            gap_id = f"{gap_id}-part-{part}"
        return ToolGapMarker.model_validate(
            {
                "schema": "TODO_TOOL.v1",
                "gap_id": gap_id,
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
                "acceptance_assertion_ids": assertion_ids,
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


def _parse_git_worktree_porcelain(output: str) -> tuple[dict[str, str], ...]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, separator, value = line.partition(" ")
        if key == "worktree" and current:
            records.append(current)
            current = {}
        if key in {"worktree", "HEAD", "branch"} and separator:
            current[key] = value
        elif key == "detached":
            current["detached"] = "true"
    if current:
        records.append(current)
    if not records or any(
        "worktree" not in record or "HEAD" not in record for record in records
    ):
        raise ValueError("Git returned incomplete worktree porcelain evidence")
    return tuple(records)


def _registered_worktree_name(root: Path) -> str:
    digest = hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest()[:12]
    return f"registered-{digest}"


def _is_excluded(path: PurePosixPath) -> bool:
    lowered_parts = tuple(part.lower() for part in path.parts)
    return any(part in _EXCLUDED_PARTS for part in lowered_parts) or any(
        part.startswith(".env") or _SECRET_PATH_PATTERN.search(part) is not None
        for part in lowered_parts
    )


def _python_symbols_by_line(relative_path: str, content: str) -> dict[int, str]:
    if not relative_path.lower().endswith(".py"):
        return {}
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return {}
    definitions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef))
    ]
    definitions.sort(
        key=lambda node: (
            -((getattr(node, "end_lineno", node.lineno) or node.lineno) - node.lineno),
            node.lineno,
        )
    )
    symbols: dict[int, str] = {}
    for node in definitions:
        decorator_lines = [
            decorator.lineno
            for decorator in getattr(node, "decorator_list", ())
        ]
        start_line = min([node.lineno, *decorator_lines])
        end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
        for line_number in range(start_line, end_line + 1):
            symbols[line_number] = node.name
    return symbols


def _path_categories(path_lower: str, content: str) -> set[str]:
    categories: set[str] = set()
    path = PurePosixPath(path_lower)
    parts = path.parts
    if (parts and parts[0] == "tests") or path.name.startswith("test_"):
        categories.add("test")
    if "prompts" in parts:
        categories.add("prompt")
    if "architecture" in path.name:
        categories.add("architecture")
    if path_lower.endswith(".json"):
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            if "$schema" in payload:
                categories.add("schema")
            if payload.get("schema") == "TODO_TOOL.v1":
                categories.add("tool_gap")
            if "n8n" in parts or _json_contains_n8n_type(payload):
                categories.add("n8n")
    return categories


def _python_categories(content: str, observed_symbols: set[str | None]) -> set[str]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set()
    categories: set[str] = set()
    contract_classes: set[str] = set()
    tool_symbols: set[str] = set()
    decorated_typed_tool_symbols: set[str] = set()
    team_constructors: set[str] = set()
    team_module_aliases: set[str] = set()
    imported_schema_symbols: set[str] = set()
    imported_schema_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            module_parts = set(node.module.split("."))
            for alias in node.names:
                local_name = alias.asname or alias.name
                if node.module.startswith("autogen") and "teams" in module_parts:
                    team_constructors.add(local_name)
                if alias.name == "teams" and node.module.startswith("autogen"):
                    team_module_aliases.add(local_name)
                if _is_schema_import(node.module):
                    imported_schema_symbols.add(local_name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".")[0]
                module_parts = set(alias.name.split("."))
                if alias.name.startswith("autogen") and "teams" in module_parts:
                    team_module_aliases.add(local_name)
                if _is_schema_import(alias.name):
                    imported_schema_modules.add(local_name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.startswith("autogen") for alias in node.names):
                categories.add("autogen")
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and node.module.startswith("autogen"):
                categories.add("autogen")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            if (
                node.name.startswith("build_") or node.name in {"main", "run"}
            ) and node.name in observed_symbols and _function_returns_autogen_team(
                node,
                team_constructors=team_constructors,
                team_module_aliases=team_module_aliases,
            ):
                categories.add("entrypoint")
            is_tool_decorated = any(
                _qualified_name(item).split(".")[-1]
                in {"function_tool", "tool", "typed_tool"}
                for item in node.decorator_list
            )
            if is_tool_decorated:
                tool_symbols.add(node.name)
                if _has_imported_schema_parameter(
                    node,
                    imported_schema_symbols=imported_schema_symbols,
                    imported_schema_modules=imported_schema_modules,
                ):
                    decorated_typed_tool_symbols.add(node.name)
        elif isinstance(node, ast.ClassDef):
            base_names = {_qualified_name(base) for base in node.bases}
            if base_names & {"BaseModel", "TypedDict"}:
                contract_classes.add(node.name)
            if node.name.endswith("Tool"):
                tool_symbols.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            target_names = _assignment_names(node)
            if any(name.endswith("PROMPT") for name in target_names):
                categories.add("prompt")
            if any("MEMORY" in name for name in target_names):
                categories.add("memory")
            if any(name in {"HANDOFF", "HANDOFFS"} for name in target_names):
                categories.add("handoff")
            if "model_client" in {name.lower() for name in target_names}:
                categories.add("model_client")
        elif isinstance(node, ast.Call):
            call_name = _qualified_name(node.func)
            if call_name.endswith("ChatCompletionClient"):
                categories.add("model_client")
            if call_name.endswith("Termination") or call_name.endswith(
                "TerminationCondition"
            ):
                categories.add("termination")
            if call_name.endswith("Handoff"):
                categories.add("handoff")
            if call_name.endswith("Memory"):
                categories.add("memory")
        elif isinstance(node, ast.If) and _is_main_guard(node.test):
            categories.add("entrypoint")
    has_local_typed_tool = bool(
        contract_classes & observed_symbols and tool_symbols & observed_symbols
    )
    has_imported_typed_tool = bool(
        decorated_typed_tool_symbols & observed_symbols
    )
    if has_local_typed_tool or has_imported_typed_tool:
        categories.add("typed_tool")
    return categories


def _is_schema_import(module: str) -> bool:
    if module == "pydantic" or module.startswith("pydantic."):
        return True
    if module.startswith("autogen"):
        return False
    semantic_parts = {"contracts", "models", "schemas", "types"}
    return bool(set(module.lower().split(".")) & semantic_parts)


def _function_returns_autogen_team(
    function: ast.AsyncFunctionDef | ast.FunctionDef,
    *,
    team_constructors: set[str],
    team_module_aliases: set[str],
) -> bool:
    team_variables: set[str] = set()
    nodes = tuple(_function_body_nodes(function))
    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is not None and _is_autogen_team_call(
                value,
                team_constructors=team_constructors,
                team_module_aliases=team_module_aliases,
            ):
                team_variables.update(_assignment_names(node))
    return any(
        isinstance(node, ast.Return)
        and node.value is not None
        and (
            _is_autogen_team_call(
                node.value,
                team_constructors=team_constructors,
                team_module_aliases=team_module_aliases,
            )
            or (isinstance(node.value, ast.Name) and node.value.id in team_variables)
        )
        for node in nodes
    )


def _function_body_nodes(
    function: ast.AsyncFunctionDef | ast.FunctionDef,
) -> tuple[ast.AST, ...]:
    pending = list(reversed(function.body))
    nodes: list[ast.AST] = []
    while pending:
        node = pending.pop()
        nodes.append(node)
        if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef, ast.Lambda)):
            continue
        pending.extend(reversed(tuple(ast.iter_child_nodes(node))))
    return tuple(nodes)


def _is_autogen_team_call(
    node: ast.AST,
    *,
    team_constructors: set[str],
    team_module_aliases: set[str],
) -> bool:
    if not isinstance(node, ast.Call):
        return False
    call_name = _qualified_name(node.func)
    if call_name in team_constructors:
        return True
    root_name = call_name.split(".", maxsplit=1)[0]
    return "." in call_name and root_name in team_module_aliases


def _has_imported_schema_parameter(
    function: ast.AsyncFunctionDef | ast.FunctionDef,
    *,
    imported_schema_symbols: set[str],
    imported_schema_modules: set[str],
) -> bool:
    arguments = (
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    )
    if function.args.vararg is not None:
        arguments += (function.args.vararg,)
    if function.args.kwarg is not None:
        arguments += (function.args.kwarg,)
    return any(
        argument.annotation is not None
        and _annotation_uses_imported_schema(
            argument.annotation,
            imported_schema_symbols=imported_schema_symbols,
            imported_schema_modules=imported_schema_modules,
        )
        for argument in arguments
    )


def _annotation_uses_imported_schema(
    annotation: ast.AST,
    *,
    imported_schema_symbols: set[str],
    imported_schema_modules: set[str],
) -> bool:
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id in imported_schema_symbols:
            return True
        if isinstance(node, ast.Attribute):
            root_name = _qualified_name(node).split(".", maxsplit=1)[0]
            if root_name in imported_schema_modules:
                return True
    return False


def _qualified_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _qualified_name(node.func)
    return ""


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False
    if not isinstance(node.ops[0], ast.Eq) or len(node.comparators) != 1:
        return False
    left = node.left
    right = node.comparators[0]
    return (
        isinstance(left, ast.Name)
        and left.id == "__name__"
        and isinstance(right, ast.Constant)
        and right.value == "__main__"
    ) or (
        isinstance(right, ast.Name)
        and right.id == "__name__"
        and isinstance(left, ast.Constant)
        and left.value == "__main__"
    )


def _assignment_names(node: ast.Assign | ast.AnnAssign) -> set[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return {
        target.id
        for target in targets
        if isinstance(target, ast.Name)
    }


def _json_contains_n8n_type(payload: object) -> bool:
    if isinstance(payload, dict):
        return any(
            (key == "type" and isinstance(value, str) and value.startswith("n8n-"))
            or _json_contains_n8n_type(value)
            for key, value in payload.items()
        )
    if isinstance(payload, list):
        return any(_json_contains_n8n_type(item) for item in payload)
    return False


def _tool_gap_payloads(contents: dict[str, str]) -> tuple[tuple[str, object], ...]:
    payloads: list[tuple[str, object]] = []
    for path, content in sorted(contents.items()):
        if not path.lower().endswith(".json"):
            continue
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            if "TODO_TOOL.v1" in content:
                raise ValueError(f"invalid TODO_TOOL.v1 JSON at {path}") from exc
            continue
        if isinstance(payload, dict) and payload.get("schema") == "TODO_TOOL.v1":
            payloads.append((path, payload))
    return tuple(payloads)


def _sorted_worktrees(
    observations: tuple[WorktreeObservation, ...],
) -> tuple[WorktreeObservation, ...]:
    unique = {
        (
            item.relative_name,
            item.revision,
            item.branch or "",
            item.detached,
            item.dirty,
        ): item
        for item in observations
    }
    return tuple(unique[key] for key in sorted(unique))


def _sorted_matches(matches: tuple[SourceMatch, ...]) -> tuple[SourceMatch, ...]:
    unique = {
        (item.relative_path, item.line, item.symbol or "", item.content_sha256): item
        for item in matches
    }
    return tuple(unique[key] for key in sorted(unique))


def _documentation_query_sha256(query: DocumentationQuery) -> str:
    payload = json.dumps(
        query.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


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
