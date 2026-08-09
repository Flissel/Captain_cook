"""Resumable, one-slice Captain API client for the canonical live demo."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from .configuration import DemoConfiguration
from .contracts import CANONICAL_INPUT_PATHS
from .processes import ManagedProcessSupervisor


class DemoClientError(RuntimeError):
    """The Captain demo request cannot safely proceed."""


class DemoTimeout(DemoClientError):
    """Preserve terminal timeout exit code 124 for operator evidence."""

    exit_code = 124


class JsonHttpPort(Protocol):
    def request_json(
        self,
        method: str,
        url: str,
        *,
        token: str,
        payload: dict[str, object] | None,
        timeout_seconds: float,
    ) -> dict[str, object]: ...


ProgressReporter = Callable[[dict[str, object]], None]


@dataclass(frozen=True, slots=True)
class _RunAuthority:
    assembly_id: UUID
    maximum_cost_usd: Decimal
    submit: dict[str, object]


class CapabilityDemoClient:
    def __init__(
        self,
        configuration: DemoConfiguration,
        *,
        supervisor: ManagedProcessSupervisor,
        http: JsonHttpPort | None = None,
        reporter: ProgressReporter = print,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._configuration = configuration
        self._supervisor = supervisor
        self._http = http or UrllibJsonHttp()
        self._report = reporter
        self._monotonic = monotonic
        self._state_path = configuration.state_root / "current-run.json"

    def run(
        self,
        *,
        allow_paid_run: bool,
        maximum_cost_usd: str,
        timeout_seconds: float,
    ) -> dict[str, object]:
        budget = _require_paid_authority(allow_paid_run, maximum_cost_usd)
        authority = self._build_authority(budget)
        deadline = self._deadline(timeout_seconds)
        state = self._new_state(authority)
        self._save_state(state)
        try:
            checkpoint = self._request(
                "POST",
                f"{self._configuration.bundle.gateway_base_url}/v1/capability-assemblies",
                token=self._gateway_token(),
                payload=authority.submit,
                deadline=deadline,
            )
            self._validate_checkpoint(checkpoint, authority.assembly_id)
            state["gateway_checkpoint"] = checkpoint
            self._save_state(state)
            return self._drive(authority, state, checkpoint, deadline)
        except DemoTimeout:
            state["status"] = "timed_out"
            state["last_exit_code"] = 124
            self._save_state(state)
            raise

    def resume(
        self,
        *,
        allow_paid_run: bool,
        maximum_cost_usd: str,
        timeout_seconds: float,
    ) -> dict[str, object]:
        budget = _require_paid_authority(allow_paid_run, maximum_cost_usd)
        state = self._load_state()
        if state.get("bundle_sha256") != self._configuration.bundle_sha256:
            raise DemoClientError("saved demo run belongs to a different sealed bundle")
        if state.get("maximum_cost_usd") != f"{budget:.2f}":
            raise DemoClientError("resume changed the authorized cost ceiling")
        try:
            assembly_id = UUID(str(state["assembly_id"]))
        except (KeyError, ValueError, TypeError):
            raise DemoClientError("saved demo assembly identity is invalid") from None
        authority = self._build_authority(budget)
        if authority.assembly_id != assembly_id:
            raise DemoClientError("saved demo request identity changed")
        deadline = self._deadline(timeout_seconds)
        try:
            checkpoint = self._request(
                "GET",
                self._projection_url(assembly_id),
                token=self._gateway_token(),
                payload=None,
                deadline=deadline,
            )
            self._validate_checkpoint(checkpoint, assembly_id)
            state["gateway_checkpoint"] = checkpoint
            state["status"] = "resuming"
            state["last_exit_code"] = None
            self._save_state(state)
            return self._drive(authority, state, checkpoint, deadline)
        except DemoTimeout:
            state["status"] = "timed_out"
            state["last_exit_code"] = 124
            self._save_state(state)
            raise

    def _drive(
        self,
        authority: _RunAuthority,
        state: dict[str, object],
        checkpoint: dict[str, object],
        deadline: float,
    ) -> dict[str, object]:
        advances = 0
        while checkpoint.get("status") != "ready_to_use":
            completed = _integer(checkpoint.get("completed_slices"), "completed_slices")
            self._apply_controlled_restart(state, checkpoint, deadline)
            if advances >= 64:
                raise DemoClientError("Captain assembly exceeded the bounded slice lifecycle")
            request_sha256 = checkpoint.get("request_sha256")
            if not isinstance(request_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", request_sha256) is None:
                raise DemoClientError("Captain checkpoint request digest is invalid")
            operation_id = uuid5(
                authority.assembly_id,
                f"advance:{completed}:{request_sha256}",
            )
            receipt = self._request(
                "POST",
                f"{self._projection_url(authority.assembly_id)}/advances",
                token=self._gateway_token(),
                payload={
                    "schema": "captain.capability-assembly-advance.v1",
                    "assembly_id": str(authority.assembly_id),
                    "operation_id": str(operation_id),
                    "request_sha256": request_sha256,
                    "maximum_slices": 1,
                },
                deadline=deadline,
            )
            if (
                receipt.get("assembly_id") != str(authority.assembly_id)
                or receipt.get("request_sha256") != request_sha256
                or receipt.get("operation_id") != str(operation_id)
                or receipt.get("maximum_slices") != 1
                or _integer(receipt.get("end_completed_slices"), "end_completed_slices")
                - _integer(receipt.get("start_completed_slices"), "start_completed_slices")
                != _integer(receipt.get("executed_slices"), "executed_slices")
            ):
                raise DemoClientError("Captain advance receipt changed request authority")
            state["last_advance_receipt"] = receipt
            self._save_state(state)
            checkpoint = self._request(
                "GET",
                self._projection_url(authority.assembly_id),
                token=self._gateway_token(),
                payload=None,
                deadline=deadline,
            )
            self._validate_checkpoint(checkpoint, authority.assembly_id)
            new_completed = _integer(checkpoint.get("completed_slices"), "completed_slices")
            if new_completed < completed or new_completed > completed + 1:
                raise DemoClientError("Captain checkpoint progress escaped one slice")
            state["gateway_checkpoint"] = checkpoint
            state["status"] = str(checkpoint.get("status"))
            self._save_state(state)
            self._report(
                {
                    "schema": "captain.demo-progress.v1",
                    "status": str(checkpoint.get("status")),
                    "completed_slices": new_completed,
                    "assembly_id": str(authority.assembly_id),
                }
            )
            advances += 1
        self._apply_controlled_restart(state, checkpoint, deadline)
        gateway_evidence = self._request(
            "GET",
            f"{self._projection_url(authority.assembly_id)}/final-evidence",
            token=self._gateway_token(),
            payload=None,
            deadline=deadline,
        )
        minibook_url = self._configuration.bundle.minibook_evidence_url_template.format(
            assembly_id=authority.assembly_id
        )
        minibook_evidence = self._request(
            "GET",
            minibook_url,
            token=self._configuration.environment[
                self._configuration.bundle.minibook_token_env
            ],
            payload=None,
            deadline=deadline,
        )
        if _canonical_json(gateway_evidence) != _canonical_json(minibook_evidence):
            raise DemoClientError("Minibook evidence does not equal Gateway final evidence")
        evidence_path = self._configuration.state_root / "evidence" / f"{authority.assembly_id}.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(evidence_path, gateway_evidence)
        state["status"] = "ready_to_use"
        state["last_exit_code"] = 0
        state["final_evidence_sha256"] = hashlib.sha256(
            _canonical_json(gateway_evidence)
        ).hexdigest()
        self._save_state(state)
        self._report(
            {
                "schema": "captain.demo-progress.v1",
                "status": "ready_to_use",
                "assembly_id": str(authority.assembly_id),
            }
        )
        return gateway_evidence

    def _apply_controlled_restart(
        self,
        state: dict[str, object],
        checkpoint: Mapping[str, object],
        deadline: float,
    ) -> None:
        completed = _integer(checkpoint.get("completed_slices"), "completed_slices")
        already = state.get("controlled_restart_completed") is True
        last_slice = checkpoint.get("last_slice")
        last_kind = last_slice.get("kind") if isinstance(last_slice, Mapping) else None
        if not already and last_kind == "resume_authorize":
            self._supervisor.restart_runtime(
                timeout_seconds=max(0.1, self._remaining(deadline))
            )
            state["controlled_restart_completed"] = True
            self._save_state(state)
            self._report(
                {
                    "schema": "captain.demo-progress.v1",
                    "status": "runtime_restarted",
                    "service": "runtime",
                    "completed_slices": completed,
                    "assembly_id": str(checkpoint["assembly_id"]),
                }
            )
        elif not already and completed > 22:
            raise DemoClientError("authorized Runtime restart boundary was missed")

    def _build_authority(self, budget: Decimal) -> _RunAuthority:
        ordered_digests = [
            self._configuration.bundle.canonical_inputs[path]
            for path in CANONICAL_INPUT_PATHS
        ]
        seed = "\n".join(
            (
                "captain.capability-demo-request.v1",
                self._configuration.bundle.repository_revision,
                self._configuration.bundle_sha256,
                f"{budget:.2f}",
                *ordered_digests,
            )
        )
        assembly_id = uuid5(NAMESPACE_URL, seed)
        total_cents = int(budget * 100)
        base, remainder = divmod(total_cents, 3)
        cents = (base, base, base + remainder)
        input_ids = ("sales-pipeline-brief", "incident-command", "claims-resolution")
        inputs: list[dict[str, object]] = []
        for input_id, path, allocation in zip(
            input_ids, CANONICAL_INPUT_PATHS, cents, strict=True
        ):
            inputs.append(
                {
                    "input_id": input_id,
                    "repository_path": path,
                    "correlation_id": str(uuid5(assembly_id, f"correlation:{input_id}")),
                    "reservation": {
                        "reservation_id": str(uuid5(assembly_id, f"reservation:{input_id}")),
                        "run_id": str(assembly_id),
                        "input_id": input_id,
                        "maximum_cost_usd": f"{Decimal(allocation) / 100:.2f}",
                    },
                }
            )
        idempotency_key = hashlib.sha256(
            f"captain.capability-demo-submit.v1\n{seed}".encode("utf-8")
        ).hexdigest()
        submit: dict[str, object] = {
            "schema": "captain.capability-assembly-submit.v1",
            "assembly_id": str(assembly_id),
            "idempotency_key": idempotency_key,
            "maximum_cost_usd": f"{budget:.2f}",
            "inputs": inputs,
        }
        return _RunAuthority(
            assembly_id=assembly_id,
            maximum_cost_usd=budget,
            submit=submit,
        )

    def _new_state(self, authority: _RunAuthority) -> dict[str, object]:
        return {
            "schema": "captain.capability-demo-run-state.v1",
            "bundle_sha256": self._configuration.bundle_sha256,
            "assembly_id": str(authority.assembly_id),
            "maximum_cost_usd": f"{authority.maximum_cost_usd:.2f}",
            "request_idempotency_key": authority.submit["idempotency_key"],
            "status": "submitting",
            "last_exit_code": None,
            "controlled_restart_completed": False,
        }

    def _validate_checkpoint(
        self, checkpoint: Mapping[str, object], assembly_id: UUID
    ) -> None:
        if checkpoint.get("assembly_id") != str(assembly_id):
            raise DemoClientError("Captain checkpoint changed the assembly identity")
        status = checkpoint.get("status")
        if status not in {"admitted", "advancing", "ready_to_use"}:
            raise DemoClientError("Captain checkpoint status is invalid")
        _integer(checkpoint.get("completed_slices"), "completed_slices")

    def _request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        payload: dict[str, object] | None,
        deadline: float,
    ) -> dict[str, object]:
        remaining = self._remaining(deadline)
        try:
            return self._http.request_json(
                method,
                url,
                token=token,
                payload=payload,
                timeout_seconds=remaining,
            )
        except (TimeoutError, DemoTimeout):
            raise DemoTimeout("Captain demo operation timed out") from None

    def _projection_url(self, assembly_id: UUID) -> str:
        return (
            f"{self._configuration.bundle.gateway_base_url}"
            f"/v1/capability-assemblies/{assembly_id}"
        )

    def _gateway_token(self) -> str:
        token = self._configuration.environment.get("CAPTAIN_GATEWAY_TOKEN", "")
        if not token:
            raise DemoClientError("Captain Gateway token is unavailable")
        return token

    def _deadline(self, timeout_seconds: float) -> float:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise DemoClientError("demo timeout must be positive and finite")
        return self._monotonic() + timeout_seconds

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise DemoTimeout("Captain demo operation timed out")
        return remaining

    def _load_state(self) -> dict[str, object]:
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise DemoClientError("resumable demo state is unavailable") from None
        if not isinstance(payload, dict) or payload.get("schema") != "captain.capability-demo-run-state.v1":
            raise DemoClientError("resumable demo state is invalid")
        return payload

    def _save_state(self, state: Mapping[str, object]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self._state_path, state)


class UrllibJsonHttp:
    def request_json(
        self,
        method: str,
        url: str,
        *,
        token: str,
        payload: dict[str, object] | None,
        timeout_seconds: float,
    ) -> dict[str, object]:
        body = None if payload is None else _canonical_json(payload)
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
        except TimeoutError:
            raise
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError("Captain demo HTTP request timed out") from None
            raise DemoClientError("Captain demo HTTP boundary is unavailable") from exc
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DemoClientError("Captain demo HTTP response is invalid") from None
        if not isinstance(value, dict):
            raise DemoClientError("Captain demo HTTP response is not an object")
        return value


def _require_paid_authority(allow: bool, raw_budget: str) -> Decimal:
    if not allow:
        raise DemoClientError("explicit paid-run authorization is required")
    if re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?", raw_budget) is None:
        raise DemoClientError("maximum cost must be an invariant decimal")
    try:
        budget = Decimal(raw_budget)
    except InvalidOperation:
        raise DemoClientError("maximum cost must be an invariant decimal") from None
    if not budget.is_finite() or budget < Decimal("0.03") or budget > Decimal("6.00"):
        raise DemoClientError("maximum total provider cost is USD 6.00")
    return budget.quantize(Decimal("0.01"))


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DemoClientError(f"Captain {label} is invalid")
    return value


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _atomic_write_json(path: Path, payload: object) -> None:
    descriptor, raw_temp = tempfile.mkstemp(prefix="demo-run-", suffix=".tmp", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical_json(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


__all__ = [
    "CapabilityDemoClient",
    "DemoClientError",
    "DemoTimeout",
    "UrllibJsonHttp",
]
