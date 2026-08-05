"""Dry-run or apply the isolated Claims/Renewal benchmark bootstrap."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from typing import Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agenten.agent_factory.business_benchmark_demo_provisioning import (
    BusinessBenchmarkDemoProvisioner,
    BusinessBenchmarkDemoPlanSettings,
    BusinessBenchmarkDemoProvisioningSettings,
)
from agenten.agent_factory.business_benchmark_contracts import BusinessBenchmarkPolicyV1
from gateway.business_benchmark_demo import GatewayBusinessBenchmarkDemoError


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Provision two idempotent Agent Factory benchmark jobs against the "
            "local isolated captain_test Gateway. Defaults to a side-effect-free dry-run."
        )
    )
    parser.add_argument("--apply", action="store_true", help="write through Gateway/MariaDB")
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="require the credential-free dry-run planning contract",
    )
    parser.add_argument("--workspace-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--issued-at",
        help=(
            "UTC provisioning epoch; reuse the dry-run value for --apply within 15 minutes"
        ),
    )
    parser.add_argument("--model", default="gpt-4.1-mini")
    parser.add_argument("--maximum-usd-per-team", default="5.00")
    parser.add_argument(
        "--execution-mode",
        choices=("demo", "release"),
        default="demo",
    )
    parser.add_argument("--suite-version", type=int, default=1)
    parser.add_argument(
        "--seed-version-id",
        default="business-benchmark-demo-2026-07",
    )
    parser.add_argument("--policy-id", default="captain-business-value-v1")
    parser.add_argument("--candidate-only-safety-gates", action="store_true")
    parser.add_argument("--relative-efficiency-diagnostics", action="store_true")
    parser.add_argument("--minimum-correctness-uplift-bps", type=int, default=0)
    parser.add_argument("--minimum-completion-uplift-bps", type=int, default=0)
    return parser.parse_args(argv)


def _issued_at(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("--issued-at must be an RFC3339 UTC timestamp")
    return parsed.astimezone(timezone.utc)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and args.plan_only:
        print("provisioning failed: --apply and --plan-only are mutually exclusive", file=sys.stderr)
        return 1
    dsn = os.environ.get("TEST_MARIADB_DSN", "") if args.apply else ""
    try:
        common_settings = {
            "workspace_root": args.workspace_root,
            "issued_at": _issued_at(args.issued_at),
            "model": args.model,
            "maximum_usd_per_team": args.maximum_usd_per_team,
            "execution_mode": args.execution_mode,
            "suite_version": args.suite_version,
            "seed_version_id": args.seed_version_id,
            "benchmark_policy": BusinessBenchmarkPolicyV1(
                schema="captain.business-benchmark-policy.v1",
                policy_id=args.policy_id,
                candidate_only_safety_gates=args.candidate_only_safety_gates,
                enforce_relative_efficiency_gates=(
                    not args.relative_efficiency_diagnostics
                ),
                minimum_correctness_uplift_bps=(
                    args.minimum_correctness_uplift_bps
                ),
                minimum_completion_uplift_bps=(
                    args.minimum_completion_uplift_bps
                ),
            ),
        }
        settings = (
            BusinessBenchmarkDemoProvisioningSettings(
                **common_settings,
                test_mariadb_dsn=dsn,
            )
            if args.apply
            else BusinessBenchmarkDemoPlanSettings(**common_settings)
        )
        gateway = None
        provisioner = BusinessBenchmarkDemoProvisioner(settings)
        if args.apply:
            from gateway.business_benchmark_demo import (
                GatewayBusinessBenchmarkDemoAuthority,
            )

            provisioner.validate_apply_preconditions()
            gateway = GatewayBusinessBenchmarkDemoAuthority(dsn)
            provisioner = BusinessBenchmarkDemoProvisioner(settings, gateway=gateway)
        result = provisioner.apply() if args.apply else provisioner.plan()
    except (GatewayBusinessBenchmarkDemoError, OSError, ValueError) as exc:
        message = str(exc)
        if dsn:
            message = message.replace(dsn, "[redacted]")
        print(f"provisioning failed: {message}", file=sys.stderr)
        return 1
    print(result.model_dump_json(by_alias=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
