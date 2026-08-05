"""Issue one fail-closed Captain recovery authority for a Hermes replay."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
from uuid import UUID


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agenten.agent_factory.hermes_cli import load_factory_skill_replay_record
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from gateway.factory_hermes_retry_authority import (
    FilesystemFactoryHermesRetryAuthority,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay-root", type=Path, required=True)
    parser.add_argument("--authority-root", type=Path, required=True)
    parser.add_argument("--job-id", type=UUID, required=True)
    parser.add_argument("--attempt", type=int, choices=range(1, 6), default=2)
    parser.add_argument(
        "--step",
        choices=tuple(
            step.value
            for step in FactorySkillStep
            if step is not FactorySkillStep.SEAL_CODEX_BUILD
        ),
        default=FactorySkillStep.IMPROVE_TEAM.value,
    )
    parser.add_argument(
        "--maximum-additional-cost-usd", type=Decimal, default=Decimal("0.03")
    )
    parser.add_argument(
        "--prior-attempt-reserve-usd", type=Decimal, default=Decimal("0.40")
    )
    parser.add_argument(
        "--benchmark-reserve-usd", type=Decimal, default=Decimal("0.20")
    )
    parser.add_argument(
        "--internal-total-cap-usd", type=Decimal, default=Decimal("0.79")
    )
    parser.add_argument(
        "--user-total-cap-eur", type=Decimal, default=Decimal("1.00")
    )
    return parser


def _canonical_authority_root(replay_root: Path, authority_root: Path) -> Path:
    resolved_replay_root = replay_root.resolve()
    expected = (
        resolved_replay_root.parent.parent / "hermes-retry-authorizations"
    ).resolve()
    resolved_authority_root = authority_root.resolve()
    if resolved_authority_root != expected:
        raise FactoryDispatchError(
            "Hermes retry authority root must be the canonical sibling of "
            "hermes-evidence"
        )
    return resolved_authority_root


def main() -> int:
    args = _parser().parse_args()
    replay_root = args.replay_root.resolve()
    authority_root = _canonical_authority_root(replay_root, args.authority_root)
    selected_step = FactorySkillStep(args.step)
    matches = []
    for path in sorted(replay_root.glob("*.json")):
        record = load_factory_skill_replay_record(path)
        if (
            record.invocation.job_id == args.job_id
            and record.invocation.attempt == args.attempt
            and record.invocation.step is selected_step
            and record.state == "failed"
        ):
            matches.append(record)
    if len(matches) != 1:
        raise FactoryDispatchError(
            "exactly one failed replay for the requested attempt and step is required"
        )
    authority = FilesystemFactoryHermesRetryAuthority(
        authority_root
    ).issue(
        matches[0],
        now=datetime.now(timezone.utc),
        maximum_additional_cost_usd=args.maximum_additional_cost_usd,
        prior_attempt_reserve_usd=args.prior_attempt_reserve_usd,
        benchmark_reserve_usd=args.benchmark_reserve_usd,
        internal_total_cap_usd=args.internal_total_cap_usd,
        user_total_cap_eur=args.user_total_cap_eur,
    )
    print(
        json.dumps(
            {
                "schema": "captain.factory-hermes-retry-issuance-result.v1",
                "job_id": str(authority.job_id),
                "idempotency_key": authority.idempotency_key,
                "authorization_ref": authority.authorization_ref.model_dump(
                    mode="json"
                ),
                "failed_replay_ref": authority.failed_replay_ref.model_dump(
                    mode="json"
                ),
                "maximum_additional_cost_usd": format(
                    authority.maximum_additional_cost_usd,
                    "f",
                ),
                "internal_total_cap_usd": format(
                    authority.internal_total_cap_usd,
                    "f",
                ),
                "user_total_cap_eur": format(
                    authority.user_total_cap_eur,
                    "f",
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
