"""Operator CLI for the sealed capability demo lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

from .client import CapabilityDemoClient, DemoClientError, DemoTimeout
from .configuration import (
    DemoConfigurationError,
    initialize_private_environment,
    load_demo_configuration,
)
from .processes import (
    DemoProcessError,
    DemoProcessTimeout,
    ManagedProcessSupervisor,
    stop_managed_processes,
)


class DemoActionsPort(Protocol):
    def init(self) -> dict[str, object]: ...

    def start(self, timeout_seconds: float) -> dict[str, object]: ...

    def status(self, timeout_seconds: float) -> dict[str, object]: ...

    def run(
        self, *, allow_paid_run: bool, maximum_cost_usd: str, timeout_seconds: float
    ) -> dict[str, object]: ...

    def resume(
        self, *, allow_paid_run: bool, maximum_cost_usd: str, timeout_seconds: float
    ) -> dict[str, object]: ...

    def stop(self) -> dict[str, object]: ...


class DefaultDemoActions:
    def __init__(self, repository_root: Path) -> None:
        self._repository_root = repository_root

    def init(self) -> dict[str, object]:
        initialize_private_environment(
            self._repository_root, reporter=lambda _: None
        )
        return {
            "schema": "captain.demo-operator-result.v1",
            "status": "initialized",
        }

    def start(self, timeout_seconds: float) -> dict[str, object]:
        configuration = load_demo_configuration(self._repository_root)
        services = self._operate(configuration, "start", timeout_seconds)
        return {
            "schema": "captain.demo-operator-result.v1",
            "status": "ready",
            "services": services,
        }

    def status(self, timeout_seconds: float) -> dict[str, object]:
        configuration = load_demo_configuration(self._repository_root)
        controls = configuration.require_operator_process_bootstrap()
        root = Path(
            configuration.environment.get(
                "CAPTAIN_PROCESS_STATE_ROOT", ".captain-cook/processes"
            )
        )
        state_root = root if root.is_absolute() else self._repository_root / root
        states = []
        for control in controls.services:
            try:
                state = json.loads(
                    (state_root / f"{control.service}.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                raise DemoProcessError("D11 managed process state is unavailable") from None
            if (
                state.get("service") != control.service
                or state.get("status") != "running"
                or not state.get("identity_kind")
                or not state.get("identity_value")
            ):
                raise DemoProcessError("D11 managed process identity is invalid")
            states.append(control.service)
        return {
            "schema": "captain.demo-operator-result.v1",
            "status": "ready",
            "services": states,
        }

    def _operate(
        self, configuration, action: str, timeout_seconds: float,
    ) -> list[str]:
        controls = configuration.require_operator_process_bootstrap().services
        selected = tuple(reversed(controls)) if action == "stop" else controls
        operated: list[str] = []
        environment = {**os.environ, **configuration.environment}
        for control in selected:
            state = self._invoke_control(
                control, action=action, environment=environment,
                timeout_seconds=timeout_seconds,
            )
            if (
                not isinstance(state, dict)
                or state.get("service") != control.service
                or state.get("status") != ("stopped" if action == "stop" else "running")
                or (action != "stop" and not state.get("identity_value"))
            ):
                raise DemoProcessError(
                    f"D11 operator {action} failed for {control.service}"
                )
            operated.append(control.service)
        return operated

    def _invoke_control(
        self, control, *, action: str, environment: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object] | None:
        raw = getattr(control, f"{action}_argv")
        argv = (sys.executable, *raw[1:])
        try:
            result = subprocess.run(
                argv, cwd=self._repository_root, env=environment,
                capture_output=True, text=True, check=False,
                timeout=max(0.1, timeout_seconds),
            )
            value = json.loads(result.stdout) if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _restart_runtime(self, configuration, *, timeout_seconds: float) -> object:
        control = configuration.require_operator_process_bootstrap().services[1]
        state = self._invoke_control(
            control, action="restart",
            environment={**os.environ, **configuration.environment},
            timeout_seconds=timeout_seconds,
        )
        if (
            state is None
            or state.get("service") != "runtime"
            or state.get("status") != "running"
            or state.get("operation") != "restart"
            or not state.get("identity_value")
        ):
            raise DemoProcessError("D11 operator restart failed for runtime")
        return state

    def run(
        self, *, allow_paid_run: bool, maximum_cost_usd: str, timeout_seconds: float
    ) -> dict[str, object]:
        return self._execute(
            resume=False,
            allow_paid_run=allow_paid_run,
            maximum_cost_usd=maximum_cost_usd,
            timeout_seconds=timeout_seconds,
        )

    def resume(
        self, *, allow_paid_run: bool, maximum_cost_usd: str, timeout_seconds: float
    ) -> dict[str, object]:
        return self._execute(
            resume=True,
            allow_paid_run=allow_paid_run,
            maximum_cost_usd=maximum_cost_usd,
            timeout_seconds=timeout_seconds,
        )

    def _execute(
        self,
        *,
        resume: bool,
        allow_paid_run: bool,
        maximum_cost_usd: str,
        timeout_seconds: float,
    ) -> dict[str, object]:
        configuration = load_demo_configuration(self._repository_root)
        supervisor = SimpleNamespace(
            restart_runtime=lambda *, timeout_seconds: self._restart_runtime(
                configuration, timeout_seconds=timeout_seconds
            )
        )
        client = CapabilityDemoClient(
            configuration,
            supervisor=supervisor,
            reporter=lambda event: print(
                json.dumps(event, sort_keys=True, separators=(",", ":"))
            ),
        )
        evidence = (
            client.resume(
                allow_paid_run=allow_paid_run,
                maximum_cost_usd=maximum_cost_usd,
                timeout_seconds=timeout_seconds,
            )
            if resume
            else client.run(
                allow_paid_run=allow_paid_run,
                maximum_cost_usd=maximum_cost_usd,
                timeout_seconds=timeout_seconds,
            )
        )
        return {
            "schema": "captain.demo-operator-result.v1",
            "status": evidence.get("status", "ready_to_use"),
            "assembly_id": evidence.get("run_id"),
        }

    def stop(self) -> dict[str, object]:
        try:
            configuration = load_demo_configuration(self._repository_root)
            stopped = self._operate(configuration, "stop", 30.0)
        except DemoConfigurationError:
            legacy = stop_managed_processes(self._repository_root)
            stopped = [record.service for record in legacy]
        return {
            "schema": "captain.demo-operator-result.v1",
            "status": "stopped",
            "services": stopped,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Captain capability demo lifecycle")
    parser.add_argument(
        "action", choices=("init", "start", "status", "run", "resume", "stop")
    )
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--allow-paid-run", action="store_true")
    parser.add_argument("--max-cost-usd", default="6.00")
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    actions_factory: Callable[[Path], DemoActionsPort] = DefaultDemoActions,
) -> int:
    args = build_parser().parse_args(argv)
    if args.action in {"run", "resume"}:
        error = _paid_argument_error(args.allow_paid_run, args.max_cost_usd)
        if error is not None:
            print(error, file=sys.stderr)
            return 2
    try:
        actions = actions_factory(args.repository_root)
        if args.action == "init":
            result = actions.init()
        elif args.action == "start":
            result = actions.start(args.timeout_seconds)
        elif args.action == "status":
            result = actions.status(args.timeout_seconds)
        elif args.action == "run":
            result = actions.run(
                allow_paid_run=True,
                maximum_cost_usd=args.max_cost_usd,
                timeout_seconds=args.timeout_seconds,
            )
        elif args.action == "resume":
            result = actions.resume(
                allow_paid_run=True,
                maximum_cost_usd=args.max_cost_usd,
                timeout_seconds=args.timeout_seconds,
            )
        else:
            result = actions.stop()
    except (DemoTimeout, DemoProcessTimeout) as exc:
        print(str(exc), file=sys.stderr)
        return 124
    except (DemoConfigurationError, DemoProcessError, DemoClientError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def _paid_argument_error(allow_paid_run: bool, raw_budget: str) -> str | None:
    if not allow_paid_run:
        return "explicit paid-run authorization is required"
    if re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?", raw_budget) is None:
        return "maximum cost must be an invariant decimal"
    try:
        budget = Decimal(raw_budget)
    except InvalidOperation:
        return "maximum cost must be an invariant decimal"
    if not budget.is_finite() or budget < Decimal("0.03") or budget > Decimal("6.00"):
        return "maximum total provider cost is USD 6.00"
    return None


__all__ = ["DefaultDemoActions", "build_parser", "main"]
