from __future__ import annotations

import hashlib
import json
import multiprocessing
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from agenten.agent_factory.business_benchmark_contracts import (
    BenchmarkDisposition,
    BusinessBenchmarkCaseMetricV1,
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkPolicyV1,
    BusinessBenchmarkReceiptV1,
    BusinessBenchmarkRunReceiptV1,
    BusinessBenchmarkSuiteV1,
    BusinessBenchmarkSummaryV1,
    BusinessCaseCategory,
    business_benchmark_metric_partition,
)
from agenten.agent_factory.business_benchmark_store import (
    BusinessBenchmarkConflictError,
    FilesystemBusinessBenchmarkEvidenceStore,
    InMemoryBusinessBenchmarkRepository,
    PrivateBusinessBenchmarkStore,
    _reject_unsafe_evidence,
)
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_runtime.contracts import ArtifactRef


NOW = datetime(2026, 7, 26, 10, tzinfo=timezone.utc)
JOB_ID = UUID("00000000-0000-0000-0000-000000000101")
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000102")


class UnsafeSerializedModel:
    """Simulates an untrusted runtime object that bypassed contract construction."""

    def __init__(self, payload: dict[str, object], field_name: str, value: str) -> None:
        self._payload = payload | {field_name: value}

    def model_dump(self, *, mode: str, by_alias: bool) -> dict[str, object]:
        assert mode == "json"
        assert by_alias
        return self._payload


def _race_write_once(
    root: str,
    payload: bytes,
    start: multiprocessing.synchronize.Event,
    outcomes: multiprocessing.queues.Queue[str],
) -> None:
    start.wait()
    try:
        FilesystemBusinessBenchmarkEvidenceStore._write_once(
            Path(root) / "runs" / "race.json", payload
        )
    except BusinessBenchmarkConflictError:
        outcomes.put("conflict")
    else:
        outcomes.put("written")


def artifact(name: str) -> ArtifactRef:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return ArtifactRef(
        uri=f"artifact://business-benchmark-test/{name}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def model_digest(value: BusinessBenchmarkCaseV1) -> str:
    encoded = json.dumps(
        value.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def toy_suite() -> BusinessBenchmarkSuiteV1:
    cases = tuple(
        BusinessBenchmarkCaseV1(
            schema="captain.business-benchmark-case.v1",
            case_id=f"toy-{category.value}-{number}",
            profile_id="insurance_claims_resolution_swarm",
            category=category,
            redacted_input={"test_organization_id": "test-org", "test_person_id": "test-person"},
            expected_decision=(
                "escalate_coverage"
                if category is BusinessCaseCategory.MANDATORY_ESCALATION
                else "route_standard_review"
            ),
            required_rationale_fact_ids=("test-fact",),
            allowed_tool_intents=("none",),
            human_handoff_required=category is BusinessCaseCategory.MANDATORY_ESCALATION,
            severity=(
                "critical"
                if category is BusinessCaseCategory.MANDATORY_ESCALATION
                else "normal"
            ),
        )
        for category in BusinessCaseCategory
        for number in range(1, 4)
    )
    return BusinessBenchmarkSuiteV1(
        schema="captain.business-benchmark-suite.v1",
        suite_id="toy-claims-suite-v1",
        profile_id="insurance_claims_resolution_swarm",
        suite_version=1,
        cases=cases,
        created_at=NOW,
    )


def suite_reference() -> PrivateHoldoutRef:
    digest = "c" * 64
    holdout_id = f"holdout-{digest[:12]}"
    return PrivateHoldoutRef(
        holdout_id=holdout_id,
        uri=f"holdout://{holdout_id}",
        sha256=digest,
    )


def run_receipt(
    status: str = "succeeded", *, variant: str = "candidate", run_id: UUID | None = None
) -> BusinessBenchmarkRunReceiptV1:
    succeeded = status == "succeeded"
    return BusinessBenchmarkRunReceiptV1(
        schema="captain.business-benchmark-run-receipt.v1",
        run_id=run_id
        or UUID(
            "00000000-0000-0000-0000-000000000103"
            if variant == "candidate"
                else "00000000-0000-0000-0000-000000000104"
            ),
        request_id=UUID(
            "00000000-0000-0000-0000-000000000105"
            if variant == "candidate"
            else "00000000-0000-0000-0000-000000000106"
        ),
        execution_policy_sha256="c" * 64,
        runtime_session_id=f"benchmark-session-{variant}",
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        attempt=1,
        suite_ref=suite_reference(),
        suite_id="toy-claims-suite-v1",
        case_id="toy-ordinary-1",
        case_sha256=model_digest(toy_suite().cases[0]),
        variant=variant,
        candidate_ref=artifact("candidate") if variant == "candidate" else None,
        model_version="test-model-v1",
        allowed_tool_intents=("none",),
        maximum_cost_micro_usd=100,
        maximum_latency_ms=100,
        status=status,
        observed_decision="route_standard_review" if succeeded else None,
        observed_rationale_fact_ids=("test-fact",) if succeeded else (),
        observed_tool_intents=("none",),
        unsafe_tool_use=False,
        human_handoff_completed=False if succeeded else None,
        cost_micro_usd=20,
        latency_ms=20,
        evidence_refs=(artifact(f"{variant}-evidence"),) if succeeded else (),
        completed_at=NOW,
    )


def case_receipt() -> BusinessBenchmarkReceiptV1:
    return BusinessBenchmarkReceiptV1(
        schema="captain.business-benchmark-receipt.v1",
        receipt_id=UUID("00000000-0000-0000-0000-000000000105"),
        case_ref=artifact("case"),
        candidate=run_receipt(),
        baseline=run_receipt(variant="single_agent_baseline"),
        candidate_decision_correct=True,
        baseline_decision_correct=True,
        candidate_rationale_complete=True,
        baseline_rationale_complete=True,
        candidate_completion_complete=True,
        baseline_completion_complete=True,
        candidate_unsafe_tool_use=False,
        baseline_unsafe_tool_use=False,
        human_handoff_required=False,
        candidate_mandatory_handoff_missed=False,
        baseline_mandatory_handoff_missed=False,
        evaluated_at=NOW,
    )


def summary(
    *,
    disposition: BenchmarkDisposition = BenchmarkDisposition.PASSED,
    reason_codes: tuple[str, ...] = (),
) -> BusinessBenchmarkSummaryV1:
    metrics = tuple(
        BusinessBenchmarkCaseMetricV1(
            case_ref=ArtifactRef(
                uri=f"artifact://business-benchmark-case/{number:064x}",
                sha256=f"{number:064x}",
                media_type="application/json",
            ),
            candidate_unsafe_tool_use=False,
            baseline_unsafe_tool_use=False,
            candidate_mandatory_handoff_missed=False,
            baseline_mandatory_handoff_missed=False,
        )
        for number in range(1, 16)
    )
    payload: dict[str, object] = {
        "schema": "captain.business-benchmark-summary.v1",
        "summary_id": "00000000-0000-0000-0000-000000000106",
        "job_id": str(JOB_ID),
        "correlation_id": str(CORRELATION_ID),
        "subject_version": 1,
        "attempt": 1,
        "candidate_ref": artifact("candidate").model_dump(mode="json"),
        "suite_ref": suite_reference().model_dump(mode="json"),
        "suite_id": "toy-claims-suite-v1",
        "policy": BusinessBenchmarkPolicyV1(
            schema="captain.business-benchmark-policy.v1"
        ).model_dump(mode="json", by_alias=True),
        "candidate_correctness_bps": 10000,
        "baseline_correctness_bps": 9000,
        "candidate_rationale_completeness_bps": 10000,
        "baseline_rationale_completeness_bps": 10000,
        "candidate_completion_bps": 10000,
        "baseline_completion_bps": 10000,
        "candidate_cost_micro_usd": 100,
        "baseline_cost_micro_usd": 100,
        "candidate_latency_ms": 100,
        "baseline_latency_ms": 100,
        "cost_ratio_bps": 10000,
        "latency_ratio_bps": 10000,
        "unsafe_tool_uses": 0,
        "mandatory_handoff_misses": 0,
        "case_metrics": [metric.model_dump(mode="json") for metric in metrics],
        "missing_receipt_count": 0,
        "disposition": disposition.value,
        "reason_codes": reason_codes,
        "evaluated_at": "2026-07-26T10:00:00Z",
    }
    if reason_codes == ("missing_receipt",):
        payload["case_metrics"] = payload["case_metrics"][:-1]
        payload["missing_receipt_count"] = 1
    passed_metric_ids, failed_metric_ids = business_benchmark_metric_partition(reason_codes)
    payload["passed_metric_ids"] = passed_metric_ids
    payload["failed_metric_ids"] = failed_metric_ids
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload["artifact_ref"] = {
        "uri": f"artifact://business-benchmark-summary/{digest}",
        "sha256": digest,
        "media_type": "application/json",
    }
    return BusinessBenchmarkSummaryV1.model_validate(payload)


def test_private_store_exposes_only_suite_reference_to_public_reader(tmp_path: Path) -> None:
    store = PrivateBusinessBenchmarkStore.from_fixture(toy_suite(), tmp_path)

    reference = store.public_suite_ref()

    assert reference.uri.startswith("holdout://holdout-")
    assert "redacted_input" not in reference.model_dump_json()
    assert store.private_suite(reference) == toy_suite()


def test_private_store_uses_exact_profile_and_version_for_private_lookup(tmp_path: Path) -> None:
    store = PrivateBusinessBenchmarkStore.from_fixture(toy_suite(), tmp_path)

    assert store.suite_ref("insurance_claims_resolution_swarm", 1) == store.public_suite_ref()
    with pytest.raises(KeyError, match="profile"):
        store.suite_ref("customer_renewal_orchestration_team", 1)
    with pytest.raises(KeyError, match="reference"):
        store.private_suite(suite_reference())


def test_in_memory_repository_is_append_only_and_only_returns_private_suite_bodies() -> None:
    repository = InMemoryBusinessBenchmarkRepository()
    reference = repository.add_suite(toy_suite())

    assert repository.suite_ref("insurance_claims_resolution_swarm", 1) == reference
    assert repository.private_suite(reference) == toy_suite()
    assert repository.record_run_receipt(run_receipt()) == repository.record_run_receipt(run_receipt())
    with pytest.raises(BusinessBenchmarkConflictError):
        repository.record_run_receipt(run_receipt("failed"))


def test_changed_receipt_replay_is_rejected_and_identical_bytes_are_stable(tmp_path: Path) -> None:
    store = FilesystemBusinessBenchmarkEvidenceStore(tmp_path)
    receipt = run_receipt()

    first = store.record_run_receipt(receipt)
    path = store.path_for(first)
    before = path.read_bytes()

    assert store.record_run_receipt(receipt) == first
    assert path.read_bytes() == before
    with pytest.raises(BusinessBenchmarkConflictError):
        store.record_run_receipt(run_receipt("failed"))


def test_case_receipts_and_summaries_are_idempotent_and_queryable(tmp_path: Path) -> None:
    store = FilesystemBusinessBenchmarkEvidenceStore(tmp_path)

    case_ref = store.record_case_receipt(case_receipt())
    summary_ref = store.record_summary(summary())

    assert store.record_case_receipt(case_receipt()) == case_ref
    assert store.record_summary(summary()) == summary_ref
    assert store.summary(UUID("00000000-0000-0000-0000-000000000106")) == summary()
    assert summary_ref != summary().artifact_ref
    assert store.summary(UUID("00000000-0000-0000-0000-000000000106")).artifact_ref == summary().artifact_ref


def test_store_resolves_legacy_equivalent_summary_for_replay(tmp_path: Path) -> None:
    store = FilesystemBusinessBenchmarkEvidenceStore(tmp_path)
    legacy = summary()
    store.record_summary(legacy)
    payload = legacy.model_dump(mode="json", by_alias=True)
    payload["summary_id"] = "00000000-0000-0000-0000-000000000206"
    payload["evaluated_at"] = "2026-07-26T11:00:00Z"
    payload.pop("artifact_ref")
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    payload["artifact_ref"] = {
        "uri": f"artifact://business-benchmark-summary/{digest}",
        "sha256": digest,
        "media_type": "application/json",
    }
    replay = BusinessBenchmarkSummaryV1.model_validate(payload)

    assert store.canonical_summary(replay) == legacy


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("raw_provider_output", "captured provider response"),
        ("rawProviderOutput", "captured provider response"),
        ("transcript", "conversation transcript"),
        ("prompt", "system prompt"),
        ("case_body", "private case body"),
        ("credential_value", "Bearer private-token"),
        ("credentialValue", "Bearer private-token"),
        ("workspace", r"C:\\Users\\operator\\work"),
    ],
)
def test_store_rejects_unredacted_or_local_run_evidence(
    tmp_path: Path, field_name: str, value: str
) -> None:
    store = FilesystemBusinessBenchmarkEvidenceStore(tmp_path)
    unsafe = UnsafeSerializedModel(
        run_receipt().model_dump(mode="json", by_alias=True), field_name, value
    )

    with pytest.raises(ValueError, match="(private|secret|local|provider|transcript|prompt|case)"):
        store.record_run_receipt(unsafe)  # type: ignore[arg-type]


def test_store_rejects_unredacted_summary_evidence(tmp_path: Path) -> None:
    store = FilesystemBusinessBenchmarkEvidenceStore(tmp_path)
    unsafe = UnsafeSerializedModel(
        summary().model_dump(mode="json", by_alias=True),
        "raw_provider_output",
        "captured provider response",
    )

    with pytest.raises(ValueError, match="(private|provider)"):
        store.record_summary(unsafe)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("rawProviderOutput", "captured provider response", "private or raw"),
        ("accessToken", "redacted", "secret-like"),
    ],
)
def test_unsafe_scanner_normalizes_camel_case_keys(
    key: str, value: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _reject_unsafe_evidence({key: value}, "scanner test")


def test_unsafe_scanner_allows_normal_opaque_artifact_reference() -> None:
    _reject_unsafe_evidence(
        {
            "evidence_ref": artifact("normal-opaque-content").model_dump(mode="json"),
        },
        "scanner test",
    )


@pytest.mark.parametrize(
    "value",
    [
        {"metadata": {"source": "artifact://C:/Users/operator/private.json"}},
        {"metadata": {"source": "artifact:///home/operator/private.json"}},
        {"metadata": {"source": r"artifact://factory/\\server\\share"}},
        {"metadata": {"source": "artifact://factory/../private.json"}},
        {"metadata": {"note": "captured at C:/Users/operator/work"}},
        {"metadata": {"note": "captured:prefix/C:/Users/operator/work"}},
    ],
)
def test_unsafe_scanner_rejects_embedded_paths_in_metadata(value: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="(local path|traversal)"):
        _reject_unsafe_evidence(value, "scanner test")


def _record_with_reference(record_kind: str, reference: ArtifactRef) -> object:
    if record_kind == "run":
        return run_receipt().model_copy(update={"candidate_ref": reference})
    if record_kind == "case":
        return case_receipt().model_copy(update={"case_ref": reference})
    if record_kind == "summary":
        return summary().model_copy(update={"candidate_ref": reference})
    raise AssertionError(f"unexpected record kind: {record_kind}")


def _record_in_store(
    store: FilesystemBusinessBenchmarkEvidenceStore, record_kind: str, record: object
) -> ArtifactRef:
    if record_kind == "run":
        return store.record_run_receipt(record)  # type: ignore[arg-type]
    if record_kind == "case":
        return store.record_case_receipt(record)  # type: ignore[arg-type]
    if record_kind == "summary":
        return store.record_summary(record)  # type: ignore[arg-type]
    raise AssertionError(f"unexpected record kind: {record_kind}")


@pytest.mark.parametrize("record_kind", ["run", "case", "summary"])
@pytest.mark.parametrize(
    "uri",
    [
        "artifact://C:/Users/operator/private.json",
        "artifact:///home/operator/private.json",
        r"artifact://factory/\\server\\share\\private.json",
        "artifact://factory/../private.json",
    ],
)
def test_store_rejects_local_or_traversal_artifact_uris(
    tmp_path: Path, record_kind: str, uri: str
) -> None:
    store = FilesystemBusinessBenchmarkEvidenceStore(tmp_path)
    unsafe_reference = artifact("unsafe-reference").model_copy(update={"uri": uri})
    record = _record_with_reference(record_kind, unsafe_reference)

    with pytest.raises(ValueError, match="(local path|traversal)"):
        _record_in_store(store, record_kind, record)


def test_write_once_is_append_only_when_processes_race_with_different_content(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    outcomes = context.Queue()
    first = b'{"record":"first"}'
    second = b'{"record":"second"}'
    processes = tuple(
        context.Process(target=_race_write_once, args=(str(tmp_path), payload, start, outcomes))
        for payload in (first, second)
    )
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    assert sorted(outcomes.get(timeout=5) for _ in processes) == ["conflict", "written"]
    assert (tmp_path / "runs" / "race.json").read_bytes() in {first, second}


@pytest.mark.parametrize(
    "unsafe_text",
    [
        r"diag foo/C:\Windows\System32",
        "file:///var/lib/captain/private.json",
        "/opt/captain/private.json",
        r"diagnostic \\server\share\private.json",
        "diagnostic ../private.json",
    ],
)
def test_store_rejects_embedded_local_paths_in_valid_run_receipts(
    tmp_path: Path, unsafe_text: str
) -> None:
    store = FilesystemBusinessBenchmarkEvidenceStore(tmp_path)
    receipt = run_receipt().model_copy(
        update={"observed_rationale_fact_ids": (unsafe_text,)}
    )

    with pytest.raises(ValueError, match="(local path|file URI|traversal)"):
        store.record_run_receipt(receipt)


@pytest.mark.parametrize(
    "unsafe_text",
    [
        r"reason C:\Windows\System32",
        "file:/etc/captain/private.json",
        "/srv/captain/private.json",
        "reason ../private.json",
    ],
)
def test_store_rejects_embedded_local_paths_in_valid_failed_summaries(
    tmp_path: Path, unsafe_text: str
) -> None:
    store = FilesystemBusinessBenchmarkEvidenceStore(tmp_path)
    valid_failed = summary(
        disposition=BenchmarkDisposition.FAILED,
        reason_codes=("missing_receipt",),
    )
    failed = valid_failed.model_copy(
        update={
            "candidate_ref": artifact("unsafe-candidate").model_copy(
                update={"uri": unsafe_text}
            )
        }
    )

    with pytest.raises(ValueError, match="(local path|file URI|traversal)"):
        store.record_summary(failed)
