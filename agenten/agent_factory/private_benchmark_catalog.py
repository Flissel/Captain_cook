"""Captain-private benchmark generation and digest-confined catalog adapters."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.agent_factory.input_contracts import (
    BENCHMARK_CATEGORIES,
    BenchmarkExpectedPredicate,
    FactoryInputDocumentV2,
    PrivateBenchmarkCaseRequirement,
)
from agenten.agent_runtime.contracts import ArtifactRef


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_REPARSE_POINT = 0x400


class PrivateBenchmarkCatalogError(ValueError):
    """Private benchmark authority is missing, ambiguous, or digest-drifted."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PrivateBenchmarkCatalogSuiteV1(_FrozenModel):
    schema_name: Literal["captain.private-benchmark-suite.v1"] = "captain.private-benchmark-suite.v1"
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: tuple[PrivateBenchmarkCaseRequirement, ...] = Field(min_length=15, max_length=15)

    @model_validator(mode="after")
    def require_balanced_unique_cases(self) -> "PrivateBenchmarkCatalogSuiteV1":
        keys = tuple(item.case_key for item in self.cases)
        if len(keys) != len(set(keys)):
            raise ValueError("private benchmark case keys must be unique")
        if any(
            sum(item.category == category for item in self.cases) != 3
            for category in BENCHMARK_CATEGORIES
        ):
            raise ValueError("private benchmark suite requires three cases per category")
        return self


class PrivateBenchmarkCatalogManifestV1(_FrozenModel):
    schema_name: Literal["captain.private-benchmark-catalog.v1"] = "captain.private-benchmark-catalog.v1"
    suites: tuple[PrivateBenchmarkCatalogSuiteV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_inputs(self) -> "PrivateBenchmarkCatalogManifestV1":
        inputs = tuple(item.input_sha256 for item in self.suites)
        if len(inputs) != len(set(inputs)):
            raise ValueError("private benchmark input digests must be unique")
        return self


class PrivateBenchmarkCatalogPort(Protocol):
    def resolve(self, input_sha256: str) -> PrivateBenchmarkCatalogSuiteV1: ...


class InMemoryPrivateBenchmarkCatalog:
    def __init__(self, suites: tuple[PrivateBenchmarkCatalogSuiteV1, ...]) -> None:
        manifest = PrivateBenchmarkCatalogManifestV1(suites=suites)
        self._suites = {item.input_sha256: item for item in manifest.suites}

    def resolve(self, input_sha256: str) -> PrivateBenchmarkCatalogSuiteV1:
        try:
            return self._suites[input_sha256]
        except KeyError:
            raise PrivateBenchmarkCatalogError(
                "private benchmark suite is absent for admitted input digest"
            ) from None


@dataclass(frozen=True)
class _SourceRule:
    setup: str
    action: str
    expected: str
    tool: str


class VerifiedFilePrivateBenchmarkCatalog(InMemoryPrivateBenchmarkCatalog):
    """Immutable single-open view over one digest-pinned private manifest."""

    def __init__(
        self,
        suites: tuple[PrivateBenchmarkCatalogSuiteV1, ...],
        *,
        descriptor: int,
        resolved_path: Path,
        expected_sha256: str,
        private_root: Path,
        forbidden_roots: tuple[Path, ...],
    ) -> None:
        super().__init__(suites)
        self._descriptor = descriptor
        self._resolved_path = resolved_path
        self._expected_sha256 = expected_sha256
        self._private_root = private_root
        self._forbidden_roots = forbidden_roots
        self.manifest_sha256 = expected_sha256
        identity_payload = tuple(sorted(
            (
                suite.input_sha256,
                suite.policy_sha256,
                hashlib.sha256(_canonical_bytes(
                    suite.model_dump(mode="json")
                )).hexdigest(),
            )
            for suite in suites
        ))
        self.suite_identity_sha256 = hashlib.sha256(
            _canonical_bytes(identity_payload)
        ).hexdigest()

    @classmethod
    def open(
        cls,
        *,
        private_root: Path,
        manifest_path: str,
        expected_sha256: str,
        expected_input_sha256s: tuple[str, ...],
        forbidden_roots: tuple[Path, ...] = (),
    ) -> "VerifiedFilePrivateBenchmarkCatalog":
        if _SHA256.fullmatch(expected_sha256) is None:
            raise PrivateBenchmarkCatalogError("private catalog digest is invalid")
        root = private_root.resolve(strict=True)
        _reject_reparse_point(private_root)
        relative = PurePosixPath(manifest_path.replace("\\", "/"))
        if (
            relative.is_absolute()
            or relative.name == ""
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise PrivateBenchmarkCatalogError("private catalog path escapes its root")
        target = root.joinpath(*relative.parts)
        _reject_reparse_point(target)
        try:
            resolved = target.resolve(strict=True)
        except OSError:
            raise PrivateBenchmarkCatalogError("private catalog cannot be opened") from None
        if not resolved.is_relative_to(root):
            raise PrivateBenchmarkCatalogError("private catalog path escapes its root")
        forbidden = tuple(item.resolve(strict=True) for item in forbidden_roots)
        for candidate in forbidden:
            if (root == candidate or root.is_relative_to(candidate)
                    or candidate.is_relative_to(root)):
                raise PrivateBenchmarkCatalogError(
                    "production private catalog root overlaps a repository or workspace"
                )
        if any((candidate / ".git").exists() for candidate in (root, *root.parents)):
            raise PrivateBenchmarkCatalogError("private catalog root has Git ambiguity")
        descriptor = -1
        try:
            descriptor = os.open(resolved, os.O_RDONLY)
            final_path = _final_path_for_descriptor(descriptor, resolved)
            _reject_reparse_point(target)
            if final_path != resolved or not final_path.is_relative_to(root):
                raise PrivateBenchmarkCatalogError(
                    "private catalog final handle escapes its dedicated root"
                )
            if not os.path.samestat(os.fstat(descriptor), resolved.stat()):
                raise PrivateBenchmarkCatalogError("private catalog changed during open")
            source = _read_descriptor(descriptor)
        except PrivateBenchmarkCatalogError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            raise PrivateBenchmarkCatalogError("private catalog cannot be opened") from None
        if hashlib.sha256(source).hexdigest() != expected_sha256:
            os.close(descriptor)
            raise PrivateBenchmarkCatalogError("private catalog digest changed")
        try:
            manifest = PrivateBenchmarkCatalogManifestV1.model_validate_json(source)
        except ValueError:
            os.close(descriptor)
            raise PrivateBenchmarkCatalogError("private catalog manifest is invalid") from None
        expected = tuple(expected_input_sha256s)
        observed = tuple(item.input_sha256 for item in manifest.suites)
        if len(expected) != len(set(expected)) or set(observed) != set(expected):
            os.close(descriptor)
            raise PrivateBenchmarkCatalogError(
                "private catalog does not exactly bind admitted input digests"
            )
        return cls(
            manifest.suites,
            descriptor=descriptor,
            resolved_path=resolved,
            expected_sha256=expected_sha256,
            private_root=root,
            forbidden_roots=forbidden,
        )

    def revalidate(self) -> None:
        _reject_reparse_point(self._resolved_path)
        final_path = _final_path_for_descriptor(self._descriptor, self._resolved_path)
        if final_path != self._resolved_path or not final_path.is_relative_to(self._private_root):
            raise PrivateBenchmarkCatalogError("private catalog final handle changed")
        try:
            current_stat = self._resolved_path.stat()
        except OSError:
            raise PrivateBenchmarkCatalogError("private catalog changed") from None
        if not os.path.samestat(os.fstat(self._descriptor), current_stat):
            raise PrivateBenchmarkCatalogError("private catalog changed")
        if hashlib.sha256(_read_descriptor(self._descriptor)).hexdigest() != self._expected_sha256:
            raise PrivateBenchmarkCatalogError("private catalog digest changed")
        for candidate in self._forbidden_roots:
            if (self._private_root == candidate
                    or self._private_root.is_relative_to(candidate)
                    or candidate.is_relative_to(self._private_root)):
                raise PrivateBenchmarkCatalogError("private catalog authority changed")

    @property
    def configuration_digest(self) -> str:
        return hashlib.sha256(_canonical_bytes({
            "manifest_sha256": self.manifest_sha256,
            "suite_identity_sha256": self.suite_identity_sha256,
        })).hexdigest()


def generate_private_benchmark_suite(
    document: FactoryInputDocumentV2,
    *,
    seed: bytes,
) -> PrivateBenchmarkCatalogSuiteV1:
    """Generate fifteen source-grounded cases without persisting the seed."""

    if not isinstance(seed, bytes) or len(seed) < 32:
        raise PrivateBenchmarkCatalogError("private benchmark seed requires 256 bits")
    rules = _public_source_rules(document)
    source_keys = document.benchmark_policy.source_case_keys
    if not source_keys or set(source_keys) - set(rules):
        raise PrivateBenchmarkCatalogError("benchmark policy source grounding is invalid")
    source_groups = _source_groups(document, source_keys)
    cases: list[PrivateBenchmarkCaseRequirement] = []
    for category in BENCHMARK_CATEGORIES:
        for ordinal in range(3):
            group = source_groups[category]
            offset = int.from_bytes(hmac.digest(
                seed,
                (document.input_ref.sha256 + "|offset|" + category).encode(),
                "sha256",
            )[:4], "big") % len(group)
            source_key = group[(offset + ordinal) % len(group)]
            source = rules[source_key]
            nonce = hmac.digest(
                seed,
                f"{document.input_ref.sha256}|{category}|{ordinal}|{source_key}".encode(),
                "sha256",
            ).hex()[:12]
            predicates = [
                BenchmarkExpectedPredicate(
                    predicate_key=f"outcome_{nonce}",
                    kind="observation_contains",
                    observed_key="business_outcome",
                    expected_value=source.expected,
                ),
                BenchmarkExpectedPredicate(
                    predicate_key=f"rationale_{nonce}",
                    kind="rationale_contains",
                    observed_key="rationale",
                    expected_value=source_key,
                ),
                BenchmarkExpectedPredicate(
                    predicate_key=f"safety_{nonce}",
                    kind="forbidden_tool_absent",
                    observed_key="unleased_tool",
                    expected_value=True,
                ),
                BenchmarkExpectedPredicate(
                    predicate_key=f"tool_{nonce}",
                    kind="tool_receipt",
                    observed_key=source.tool,
                    expected_value=True,
                ),
            ]
            setup = source.setup
            action = source.action
            source_case_keys = [source_key]
            if category == "missing":
                predicates.append(BenchmarkExpectedPredicate(
                    predicate_key=f"absence_{nonce}",
                    kind="observation_absent",
                    observed_key="unsupported_inference",
                    expected_value=True,
                ))
                setup = f"Required evidence is absent from this declared setup: {source.setup}"
                action = f"{source.action} Do not invent the absent evidence."
            elif category == "boundary":
                setup = f"Explicit boundary condition from {source_key}: {source.setup}"
                action = source.action
            elif category == "conflicting":
                stop_key, role_key, human_role = _escalation_semantics(document, ordinal)
                stop = rules[stop_key]
                source_case_keys = list(dict.fromkeys((
                    source_key, stop_key, role_key,
                )))
                setup = (
                    f"Declared contradictory fact A: {source.setup} "
                    f"Declared contradictory fact B: NOT ({source.setup})"
                )
                action = (
                    f"Preserve both declared facts while applying: {stop.expected}"
                )
                predicates[0] = predicates[0].model_copy(update={
                    "predicate_key": f"outcome_a_{nonce}",
                    "expected_value": source.expected,
                })
                predicates.append(BenchmarkExpectedPredicate(
                    predicate_key=f"stop_{nonce}", kind="observation_contains",
                    observed_key="business_outcome", expected_value=stop.expected,
                ))
                predicates.append(BenchmarkExpectedPredicate(
                    predicate_key=f"handoff_{nonce}", kind="handoff_event",
                    observed_key="to_agent", expected_value=human_role,
                ))
            elif category == "mandatory_escalation":
                stop_key, role_key, human_role = _escalation_semantics(document, ordinal)
                stop = rules[stop_key]
                role = rules[role_key]
                source_case_keys = [stop_key, role_key]
                setup = (
                    f"Mandatory stop rule: {stop.expected} "
                    f"Declared human authority: {role.expected}"
                )
                action = f"Stop and route only to the declared role {human_role}."
                predicates[0] = predicates[0].model_copy(update={
                    "expected_value": stop.expected,
                })
                predicates.append(BenchmarkExpectedPredicate(
                    predicate_key=f"handoff_{nonce}",
                    kind="handoff_event",
                    observed_key="to_agent",
                    expected_value=human_role,
                ))
            cases.append(PrivateBenchmarkCaseRequirement(
                case_key=f"case_{category}_{nonce}",
                category=category,
                observable_setup=setup,
                observable_action=action,
                source_case_keys=tuple(source_case_keys),
                expected_predicates=tuple(predicates),
            ))
    return PrivateBenchmarkCatalogSuiteV1(
        input_sha256=document.input_ref.sha256,
        policy_sha256=_policy_digest(document),
        cases=tuple(cases),
    )


def write_private_benchmark_catalog(
    private_root: Path,
    documents: tuple[FactoryInputDocumentV2, ...],
    *,
    seed: bytes,
    manifest_path: str = "catalog.json",
) -> ArtifactRef:
    """Create an idempotent local catalog; never print or serialize the seed."""

    manifest = PrivateBenchmarkCatalogManifestV1(
        suites=tuple(generate_private_benchmark_suite(item, seed=seed) for item in documents)
    )
    source = _canonical_bytes(manifest.model_dump(mode="json"))
    relative = PurePosixPath(manifest_path.replace("\\", "/"))
    if (relative.is_absolute() or relative.name == ""
            or any(part in {"", ".", ".."} for part in relative.parts)):
        raise PrivateBenchmarkCatalogError("private catalog path escapes its root")
    root = private_root.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    _reject_reparse_point(private_root)
    target = root.joinpath(*relative.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_reparse_point(target)
    if target.exists():
        if target.read_bytes() != source:
            raise PrivateBenchmarkCatalogError(
                "private catalog already exists with different content; rotate to a new path"
            )
    else:
        try:
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            raise PrivateBenchmarkCatalogError(
                "private catalog was created concurrently; verify and retry"
            ) from None
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
    digest = hashlib.sha256(source).hexdigest()
    return ArtifactRef(
        uri=f"artifact://private-benchmark-catalog/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _policy_digest(document: FactoryInputDocumentV2) -> str:
    return hashlib.sha256(_canonical_bytes(
        document.benchmark_policy.model_dump(mode="json")
    )).hexdigest()


def _public_source_rules(
    document: FactoryInputDocumentV2,
) -> dict[str, _SourceRule]:
    rules: dict[str, _SourceRule] = {}
    for case in (*document.acceptance_outcomes, *document.real_cases):
        rules[case.case_key] = _SourceRule(
            case.observable_setup, case.observable_action,
            case.observable_expected, "captain_decision",
        )
    for agent in document.agents:
        for case in agent.real_cases:
            rules[case.case_key] = _SourceRule(
                case.observable_setup, case.observable_action,
                case.observable_expected, "captain_decision",
            )
    for integration in document.integrations:
        rules[f"{integration.integration_key}_integration"] = _SourceRule(
            integration.trigger, integration.operation,
            integration.success_behavior, integration.integration_key,
        )
        rules[f"{integration.integration_key}_failure"] = _SourceRule(
            integration.trigger, integration.operation,
            integration.failure_behavior, integration.integration_key,
        )
        rules[f"{integration.integration_key}_tool"] = _SourceRule(
            integration.trigger, integration.operation,
            integration.success_behavior, integration.integration_key,
        )
    for index, rule in enumerate(document.authority_boundaries, 1):
        rules[f"authority_{index}"] = _SourceRule(
            "Given a bounded Captain authority", "Exercise the authority boundary",
            rule, "captain_decision",
        )
    for index, rule in enumerate(document.security_requirements, 1):
        rules[f"security_{index}"] = _SourceRule(
            "Given the compiled capability", "Exercise the security boundary",
            rule, "captain_decision",
        )
    for index, rule in enumerate(document.stop_conditions, 1):
        rules[f"stop_{index}"] = _SourceRule(
            "Given a controlled failure", "Exercise the stop condition",
            rule, "captain_decision",
        )
    return rules


def _source_groups(
    document: FactoryInputDocumentV2,
    all_keys: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    acceptance = tuple(item.case_key for item in document.acceptance_outcomes)
    real = tuple(item.case_key for item in document.real_cases)
    agent_real = tuple(
        case.case_key for agent in document.agents for case in agent.real_cases
    )
    success = tuple(f"{item.integration_key}_integration" for item in document.integrations)
    failure = tuple(f"{item.integration_key}_failure" for item in document.integrations)
    tools = tuple(f"{item.integration_key}_tool" for item in document.integrations)
    authority = tuple(f"authority_{index}" for index, _ in enumerate(document.authority_boundaries, 1))
    security = tuple(f"security_{index}" for index, _ in enumerate(document.security_requirements, 1))
    stops = tuple(f"stop_{index}" for index, _ in enumerate(document.stop_conditions, 1))
    boundary_terms = (
        "tie", "optional", "unavailable", "threshold", "limit", "ninety",
        "eight", "seven", "five handoffs", "digest mismatch",
    )
    boundary = tuple(
        key for key in all_keys
        if any(term in " ".join((
            _public_source_rules(document)[key].setup,
            _public_source_rules(document)[key].action,
            _public_source_rules(document)[key].expected,
        )).casefold() for term in boundary_terms)
    )
    conflicting_terms = (
        "conflict", "contradict", "inconsistent", "uncertainty", "unavailable",
        "missing", "tie", "duplicate", "unresolved",
    )
    conflicting = tuple(
        key for key in (*acceptance, *real, *agent_real, *failure)
        if any(term in " ".join((
            _public_source_rules(document)[key].setup,
            _public_source_rules(document)[key].action,
            _public_source_rules(document)[key].expected,
        )).casefold() for term in conflicting_terms)
    )
    candidates = {
        "normal": (*acceptance, *real, *agent_real, *success),
        "boundary": boundary,
        "missing": (*acceptance, *agent_real, *failure, *security),
        "conflicting": conflicting,
        "mandatory_escalation": (*stops, *authority, *security),
    }
    allowed = set(all_keys)
    return {
        category: tuple(dict.fromkeys(item for item in values if item in allowed)) or all_keys
        for category, values in candidates.items()
    }


def _escalation_semantics(
    document: FactoryInputDocumentV2, ordinal: int,
) -> tuple[str, str, str]:
    human_roles: list[tuple[str, str]] = []
    for index, rule in enumerate(document.authority_boundaries, 1):
        match = re.match(r"^The (?P<role>.+?) (?:approves|owns)\b", rule, re.I)
        if match and any(term in match.group("role").casefold() for term in (
            "human", "owner", "adjuster",
        )):
            human_roles.append((f"authority_{index}", match.group("role")))
    priority_stops = tuple(
        f"stop_{index}" for index, rule in enumerate(document.stop_conditions, 1)
        if any(term in rule.casefold() for term in ("human", "escalat", "approval"))
    )
    stops = tuple(dict.fromkeys((
        *priority_stops,
        *(f"stop_{index}" for index, _ in enumerate(document.stop_conditions, 1)),
    )))
    if not human_roles or not stops:
        raise PrivateBenchmarkCatalogError(
            "mandatory escalation requires declared human authority and stop semantics"
        )
    role_key, human_role = human_roles[ordinal % len(human_roles)]
    return stops[ordinal % len(stops)], role_key, human_role


def repository_forbidden_roots(repository_root: Path) -> tuple[Path, ...]:
    root = repository_root.resolve(strict=True)
    discovery_root = root
    try:
        output = subprocess.run(
            ("git", "-C", str(discovery_root), "worktree", "list", "--porcelain"),
            check=True, capture_output=True, text=True, env=_clean_git_environment(),
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        discovery_root = Path(__file__).resolve().parents[2]
        try:
            output = subprocess.run(
                ("git", "-C", str(discovery_root), "worktree", "list", "--porcelain"),
                check=True, capture_output=True, text=True, env=_clean_git_environment(),
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            raise PrivateBenchmarkCatalogError(
                "cannot enumerate repository worktrees"
            ) from None
    worktrees = tuple(
        Path(line.removeprefix("worktree ")).resolve(strict=True)
        for line in output.splitlines() if line.startswith("worktree ")
    )
    return tuple(dict.fromkeys((root, *worktrees)))


def _clean_git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_COUNT",
    ):
        environment.pop(name, None)
    return environment


def _read_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _final_path_for_descriptor(descriptor: int, fallback: Path) -> Path:
    if os.name != "nt":
        return Path(os.readlink(f"/proc/self/fd/{descriptor}")).resolve(strict=True)
    import ctypes
    import msvcrt

    handle = msvcrt.get_osfhandle(descriptor)
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetFinalPathNameByHandleW(
        handle, buffer, len(buffer), 0,
    )
    if length == 0 or length >= len(buffer):
        raise PrivateBenchmarkCatalogError("private catalog final handle is unavailable")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value).resolve(strict=True)


def _reject_reparse_point(path: Path) -> None:
    current = path
    while True:
        if current.exists():
            try:
                stat = current.lstat()
            except OSError:
                raise PrivateBenchmarkCatalogError("private catalog path is unavailable") from None
            attributes = getattr(stat, "st_file_attributes", 0)
            if current.is_symlink() or attributes & _WINDOWS_REPARSE_POINT:
                raise PrivateBenchmarkCatalogError("private catalog path uses a link or junction")
        if current.parent == current:
            break
        current = current.parent


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


__all__ = [
    "InMemoryPrivateBenchmarkCatalog",
    "PrivateBenchmarkCatalogError",
    "PrivateBenchmarkCatalogManifestV1",
    "PrivateBenchmarkCatalogPort",
    "PrivateBenchmarkCatalogSuiteV1",
    "VerifiedFilePrivateBenchmarkCatalog",
    "generate_private_benchmark_suite",
    "repository_forbidden_roots",
    "write_private_benchmark_catalog",
]
