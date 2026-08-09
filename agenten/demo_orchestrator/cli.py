"""Operator CLI for the sealed capability demo lifecycle."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from .client import CapabilityDemoClient, DemoClientError, DemoTimeout
from .configuration import (
    DemoConfigurationError,
    initialize_private_environment,
    load_demo_configuration,
)
from .processes import (
    DemoProcessError,
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
        records = ManagedProcessSupervisor(configuration).start_all(
            timeout_seconds=timeout_seconds
        )
        return {
            "schema": "captain.demo-operator-result.v1",
            "status": "ready",
            "services": [record.service for record in records],
        }

    def status(self, timeout_seconds: float) -> dict[str, object]:
        configuration = load_demo_configuration(self._repository_root)
        records = ManagedProcessSupervisor(configuration).status(
            timeout_seconds=timeout_seconds
        )
        names = [record.service for record in records]
        expected = [service.name for service in configuration.bundle.services]
        return {
            "schema": "captain.demo-operator-result.v1",
            "status": "ready" if names == expected else "degraded",
            "services": names,
        }

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
        supervisor = ManagedProcessSupervisor(configuration)
        healthy = supervisor.status(timeout_seconds=min(timeout_seconds, 5))
        if [record.service for record in healthy] != [
            service.name for service in configuration.bundle.services
        ]:
            raise DemoProcessError("all sealed demo services must be healthy before a paid run")
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
        stopped = stop_managed_processes(self._repository_root)
        return {
            "schema": "captain.demo-operator-result.v1",
            "status": "stopped",
            "services": [record.service for record in stopped],
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
    except DemoTimeout as exc:
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
