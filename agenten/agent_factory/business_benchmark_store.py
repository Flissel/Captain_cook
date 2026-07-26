"""Captain-private benchmark suites and append-only redacted evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol
from uuid import UUID

from pydantic import BaseModel

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkReceiptV1,
    BusinessBenchmarkRunReceiptV1,
    BusinessBenchmarkSuiteV1,
    BusinessBenchmarkSummaryV1,
)
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_runtime.contracts import ArtifactRef


_SECRET_KEY_PATTERN = re.compile(
    r"(?i)(?:^|_)(?:api[_-]?key|authorization|credential|password|private[_-]?key|secret|token)(?:$|_)"
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"(?i)(?:^|_)(?:private|raw(?:_provider)?_output|transcript|prompt|case[_-]?body|workspace|local[_-]?path)(?:$|_)"
)
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(?:\b(?:sk-[a-z0-9_-]{8,}|bearer\s+\S+)|\b(?:api[_-]?key|authorization|credential|password|secret|token)\b\s*[=:])"
)
_ENDPOINT_PATTERN = re.compile(r"(?i)https?://[^\s\"'<>]+")
_FILE_URI_PATTERN = re.compile(r"(?i)(?<![a-z0-9_])file:")
_WINDOWS_OR_UNC_PATH_PATTERN = re.compile(r"(?i)(?:^|[^a-z0-9])(?:[a-z]:[\\/]|\\\\)")
_POSIX_PATH_PATTERN = re.compile(r"(?:^|[^a-z0-9])/(?!/)")
_TRAVERSAL_PATTERN = re.compile(r"(?:^|[^a-z0-9])\.\.(?:[\\/]|$)")
_EVIDENCE_URI_PREFIX = "artifact://business-benchmark-evidence/"


class BusinessBenchmarkConflictError(ValueError):
    """An immutable benchmark identity was replayed with changed content."""


class BusinessBenchmarkRepository(Protocol):
    """Private Captain port; it is intentionally not a Gateway/Minibook API."""

    def suite_ref(self, profile_id: str, suite_version: int) -> PrivateHoldoutRef: ...

    def private_suite(self, reference: PrivateHoldoutRef) -> BusinessBenchmarkSuiteV1: ...

    def record_run_receipt(self, receipt: BusinessBenchmarkRunReceiptV1) -> ArtifactRef: ...

    def record_case_receipt(self, receipt: BusinessBenchmarkReceiptV1) -> ArtifactRef: ...

    def record_summary(self, summary: BusinessBenchmarkSummaryV1) -> ArtifactRef: ...

    def summary(self, summary_id: UUID) -> BusinessBenchmarkSummaryV1 | None: ...


class PrivateBusinessBenchmarkStore:
    """Persist a suite body under a Captain-only holdout reference."""

    def __init__(self, root: Path, reference: PrivateHoldoutRef) -> None:
        self._root = root
        self._reference = reference

    @classmethod
    def from_fixture(
        cls,
        fixture: BusinessBenchmarkSuiteV1 | Mapping[str, object],
        root: Path,
    ) -> "PrivateBusinessBenchmarkStore":
        suite = _canonical_suite(fixture)
        content = _canonical_json(suite)
        digest = hashlib.sha256(content).hexdigest()
        holdout_id = f"holdout-{digest[:12]}"
        reference = PrivateHoldoutRef(
            holdout_id=holdout_id,
            uri=f"holdout://{holdout_id}",
            sha256=digest,
        )
        cls._write_once(root / "private-suites" / f"{holdout_id}.json", content)
        return cls(root, reference)

    def public_suite_ref(self) -> PrivateHoldoutRef:
        """Return only an opaque reference; this method never returns case bodies."""

        return self._reference

    def suite_ref(self, profile_id: str, suite_version: int) -> PrivateHoldoutRef:
        suite = self.private_suite(self._reference)
        if suite.profile_id != profile_id or suite.suite_version != suite_version:
            raise KeyError("no private suite matches profile and version")
        return self._reference

    def private_suite(self, reference: PrivateHoldoutRef) -> BusinessBenchmarkSuiteV1:
        if reference != self._reference:
            raise KeyError("private suite reference is not held by this store")
        content = (self._root / "private-suites" / f"{reference.holdout_id}.json").read_bytes()
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise BusinessBenchmarkConflictError("private suite content does not match its reference")
        return BusinessBenchmarkSuiteV1.model_validate_json(content)

    @staticmethod
    def _write_once(path: Path, content: bytes) -> None:
        FilesystemBusinessBenchmarkEvidenceStore._write_once(path, content)


@dataclass
class InMemoryBusinessBenchmarkRepository:
    """Deterministic private repository for unit tests and offline composition."""

    _suites: dict[PrivateHoldoutRef, BusinessBenchmarkSuiteV1] = field(default_factory=dict)
    _runs: dict[UUID, tuple[BusinessBenchmarkRunReceiptV1, ArtifactRef]] = field(
        default_factory=dict
    )
    _cases: dict[UUID, tuple[BusinessBenchmarkReceiptV1, ArtifactRef]] = field(
        default_factory=dict
    )
    _summaries: dict[UUID, tuple[BusinessBenchmarkSummaryV1, ArtifactRef]] = field(
        default_factory=dict
    )

    def add_suite(self, suite: BusinessBenchmarkSuiteV1) -> PrivateHoldoutRef:
        canonical = _canonical_suite(suite)
        content = _canonical_json(canonical)
        digest = hashlib.sha256(content).hexdigest()
        holdout_id = f"holdout-{digest[:12]}"
        reference = PrivateHoldoutRef(
            holdout_id=holdout_id,
            uri=f"holdout://{holdout_id}",
            sha256=digest,
        )
        existing = self._suites.get(reference)
        if existing is not None and existing != canonical:
            raise BusinessBenchmarkConflictError("private suite reference already has different content")
        self._suites[reference] = canonical
        return reference

    def suite_ref(self, profile_id: str, suite_version: int) -> PrivateHoldoutRef:
        for reference, suite in self._suites.items():
            if suite.profile_id == profile_id and suite.suite_version == suite_version:
                return reference
        raise KeyError("no private suite matches profile and version")

    def private_suite(self, reference: PrivateHoldoutRef) -> BusinessBenchmarkSuiteV1:
        try:
            return self._suites[reference]
        except KeyError as exc:
            raise KeyError("private suite reference is not held by this repository") from exc

    def record_run_receipt(self, receipt: BusinessBenchmarkRunReceiptV1) -> ArtifactRef:
        canonical = _canonical_run(receipt)
        return self._append(self._runs, canonical.run_id, canonical, "runs")

    def record_case_receipt(self, receipt: BusinessBenchmarkReceiptV1) -> ArtifactRef:
        canonical = _canonical_case(receipt)
        return self._append(self._cases, canonical.receipt_id, canonical, "cases")

    def record_summary(self, summary: BusinessBenchmarkSummaryV1) -> ArtifactRef:
        canonical = _canonical_summary(summary)
        return self._append(self._summaries, canonical.summary_id, canonical, "summaries")

    def summary(self, summary_id: UUID) -> BusinessBenchmarkSummaryV1 | None:
        stored = self._summaries.get(summary_id)
        return None if stored is None else stored[0]

    @staticmethod
    def _append(
        records: dict[UUID, tuple[_BenchmarkModel, ArtifactRef]],
        identity: UUID,
        incoming: _BenchmarkModel,
        record_kind: str,
    ) -> ArtifactRef:
        content = _canonical_json(incoming)
        reference = _evidence_ref(record_kind, identity, content)
        existing = records.get(identity)
        if existing is not None:
            if _canonical_json(existing[0]) != content:
                raise BusinessBenchmarkConflictError(
                    f"{record_kind} identity already exists with different content"
                )
            return existing[1]
        records[identity] = (incoming, reference)
        return reference


class FilesystemBusinessBenchmarkEvidenceStore:
    """Write-once redacted receipts and summaries beneath a Captain-owned root."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def record_run_receipt(self, receipt: BusinessBenchmarkRunReceiptV1) -> ArtifactRef:
        canonical = _canonical_run(receipt)
        return self._record("runs", canonical.run_id, canonical)

    def record_case_receipt(self, receipt: BusinessBenchmarkReceiptV1) -> ArtifactRef:
        canonical = _canonical_case(receipt)
        return self._record("cases", canonical.receipt_id, canonical)

    def record_summary(self, summary: BusinessBenchmarkSummaryV1) -> ArtifactRef:
        canonical = _canonical_summary(summary)
        return self._record("summaries", canonical.summary_id, canonical)

    def summary(self, summary_id: UUID) -> BusinessBenchmarkSummaryV1 | None:
        path = self._root / "summaries" / f"{summary_id}.json"
        if not path.exists():
            return None
        return BusinessBenchmarkSummaryV1.model_validate_json(path.read_bytes())

    def path_for(self, reference: ArtifactRef) -> Path:
        if not reference.uri.startswith(_EVIDENCE_URI_PREFIX):
            raise ValueError("business benchmark reference is outside this evidence store")
        parts = reference.uri.removeprefix(_EVIDENCE_URI_PREFIX).split("/")
        if len(parts) != 3 or parts[2] != reference.sha256:
            raise ValueError("business benchmark reference does not match its digest")
        record_kind, record_id, _ = parts
        if record_kind not in {"runs", "cases", "summaries"}:
            raise ValueError("business benchmark reference has an unknown record kind")
        try:
            parsed_id = UUID(record_id)
        except ValueError as exc:
            raise ValueError("business benchmark reference has an invalid record id") from exc
        return self._root / record_kind / f"{parsed_id}.json"

    def _record(self, record_kind: str, identity: UUID, model: BaseModel) -> ArtifactRef:
        content = _canonical_json(model)
        reference = _evidence_ref(record_kind, identity, content)
        path = self.path_for(reference)
        self._write_once(path, content)
        return reference

    @staticmethod
    def _write_once(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != content:
                    raise BusinessBenchmarkConflictError(
                        "benchmark evidence identity already has different content"
                    )
        finally:
            temporary.unlink(missing_ok=True)


def load_suite(
    repository: BusinessBenchmarkRepository,
    profile_id: str,
    suite_version: int,
) -> BusinessBenchmarkSuiteV1:
    """Resolve a suite only through the private repository port."""

    return repository.private_suite(repository.suite_ref(profile_id, suite_version))


def record_run_receipt(
    repository: BusinessBenchmarkRepository, receipt: BusinessBenchmarkRunReceiptV1
) -> ArtifactRef:
    return repository.record_run_receipt(receipt)


def record_case_receipt(
    repository: BusinessBenchmarkRepository, receipt: BusinessBenchmarkReceiptV1
) -> ArtifactRef:
    return repository.record_case_receipt(receipt)


def record_summary(
    repository: BusinessBenchmarkRepository, summary: BusinessBenchmarkSummaryV1
) -> ArtifactRef:
    return repository.record_summary(summary)


_BenchmarkModel = BusinessBenchmarkRunReceiptV1 | BusinessBenchmarkReceiptV1 | BusinessBenchmarkSummaryV1


def _canonical_json(model: BaseModel) -> bytes:
    payload = model.model_dump(mode="json", by_alias=True)
    _reject_unsafe_evidence(payload, "record")
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_suite(
    value: BusinessBenchmarkSuiteV1 | Mapping[str, object],
) -> BusinessBenchmarkSuiteV1:
    if isinstance(value, BusinessBenchmarkSuiteV1):
        payload = value.model_dump(mode="json", by_alias=True)
    else:
        payload = dict(value)
    return BusinessBenchmarkSuiteV1.model_validate(payload)


def _canonical_run(value: BusinessBenchmarkRunReceiptV1) -> BusinessBenchmarkRunReceiptV1:
    payload = value.model_dump(mode="json", by_alias=True)
    _reject_unsafe_evidence(payload, "run receipt")
    return BusinessBenchmarkRunReceiptV1.model_validate(payload)


def _canonical_case(value: BusinessBenchmarkReceiptV1) -> BusinessBenchmarkReceiptV1:
    payload = value.model_dump(mode="json", by_alias=True)
    _reject_unsafe_evidence(payload, "case receipt")
    return BusinessBenchmarkReceiptV1.model_validate(payload)


def _canonical_summary(value: BusinessBenchmarkSummaryV1) -> BusinessBenchmarkSummaryV1:
    payload = value.model_dump(mode="json", by_alias=True)
    _reject_unsafe_evidence(payload, "summary")
    return BusinessBenchmarkSummaryV1.model_validate(payload)


def _evidence_ref(record_kind: str, identity: UUID, content: bytes) -> ArtifactRef:
    digest = hashlib.sha256(content).hexdigest()
    return ArtifactRef(
        uri=f"{_EVIDENCE_URI_PREFIX}{record_kind}/{identity}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _reject_unsafe_evidence(value: object, location: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            normalized_key = _normalize_field_key(key_text)
            if _SECRET_KEY_PATTERN.search(normalized_key):
                raise ValueError(f"{location} contains a secret-like field")
            if _PRIVATE_KEY_PATTERN.search(normalized_key):
                raise ValueError(f"{location} contains a private or raw field")
            _reject_unsafe_evidence(nested, f"{location}.{key_text}")
        return
    if isinstance(value, (tuple, list)):
        for index, nested in enumerate(value):
            _reject_unsafe_evidence(nested, f"{location}[{index}]")
        return
    if isinstance(value, str):
        path_reason = _unsafe_path_reason(value)
        if path_reason is not None:
            raise ValueError(f"{location} contains {path_reason}")
        if _ENDPOINT_PATTERN.search(value):
            raise ValueError(f"{location} contains a raw provider endpoint")
        if _SECRET_VALUE_PATTERN.search(value):
            raise ValueError(f"{location} contains a secret-like value")


def _normalize_field_key(value: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).replace("-", "_").lower()


def _unsafe_path_reason(value: str) -> str | None:
    if _FILE_URI_PATTERN.search(value):
        return "a local file URI"
    if value.startswith("artifact://") or value.startswith("holdout://"):
        return _unsafe_opaque_uri_reason(value)
    if _WINDOWS_OR_UNC_PATH_PATTERN.search(value) or _POSIX_PATH_PATTERN.search(value):
        return "a local path"
    if _TRAVERSAL_PATTERN.search(value):
        return "traversal"
    return None


def _unsafe_opaque_uri_reason(value: str) -> str | None:
    if value.startswith("artifact://"):
        location = value.removeprefix("artifact://")
    elif value.startswith("holdout://"):
        location = value.removeprefix("holdout://")
    else:
        return None
    if location.startswith("/") or location.startswith("\\"):
        return "a local path"
    if "\\" in location:
        return "a local path"
    if re.search(r"(?:^|/)[a-zA-Z]:/", location):
        return "a local path"
    if any(segment in {".", ".."} for segment in location.split("/")):
        return "traversal"
    return None
