from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

import pytest

from agenten.agent_factory.hermes_effect_evidence import (
    FilesystemHermesProviderEffectStore,
)


def _write_unresolved_effect(root: Path) -> None:
    effects = root / "effects"
    effects.mkdir(parents=True)
    (effects / f"{'a' * 64}.json").write_text(
        json.dumps(
            {
                "schema": "captain.hermes-provider-effect.v1",
                "estimated_cost_usd": None,
            }
        ),
        encoding="utf-8",
    )


def test_unresolved_provider_effect_remains_fail_closed_without_reserve(
    tmp_path: Path,
) -> None:
    _write_unresolved_effect(tmp_path)

    with pytest.raises(ValueError, match="incomplete"):
        FilesystemHermesProviderEffectStore(tmp_path).total_estimated_cost_usd()


def test_unresolved_provider_effect_consumes_full_operator_reserve(
    tmp_path: Path,
) -> None:
    _write_unresolved_effect(tmp_path)

    assert FilesystemHermesProviderEffectStore(
        tmp_path,
        unresolved_effect_reserve_usd=Decimal("0.25"),
    ).total_estimated_cost_usd() == Decimal("0.25")
