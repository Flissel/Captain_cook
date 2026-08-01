from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from agenten.agent_factory.orchestration import FactoryDispatchError


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "issue-factory-hermes-retry.py"


def _module():
    spec = spec_from_file_location("issue_factory_hermes_retry", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_retry_issuer_accepts_only_canonical_authority_sibling(
    tmp_path: Path,
) -> None:
    module = _module()
    replay_root = (
        tmp_path / "runtime-state" / "hermes-evidence" / "skill-replays"
    )
    authority_root = tmp_path / "runtime-state" / "hermes-retry-authorizations"

    assert (
        module._canonical_authority_root(replay_root, authority_root)
        == authority_root.resolve()
    )


def test_retry_issuer_rejects_ambiguous_runtime_state_root(tmp_path: Path) -> None:
    module = _module()
    replay_root = (
        tmp_path / "runtime-state" / "hermes-evidence" / "skill-replays"
    )

    with pytest.raises(FactoryDispatchError, match="canonical sibling"):
        module._canonical_authority_root(
            replay_root,
            tmp_path / "runtime-state",
        )
