"""Pre-run definition gate for the live-demo conversation patterns.

Task 7 of the production-authority-assembly plan requires the three
conversation patterns to be defined as fixtures before any live run and
their digests recorded. This gate proves the fixtures are complete,
declarative, secret-free, and byte-stable under the single-open read.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from agenten.agent_factory.single_open import sha256_of_verified_read

FIXTURE_ROOT = Path("tests/fixtures/live_demo/conversation_patterns")
EXPECTED_KEYS = {
    "schema_name",
    "pattern_key",
    "description",
    "participants",
    "turns",
    "terminal_assertion",
}
SECRET_MARKERS = re.compile(r"(?i)(api[_-]?key|password|secret|token=)")


def _fixture_paths() -> list[Path]:
    return sorted(FIXTURE_ROOT.glob("pattern-*.json"))


def test_exactly_three_patterns_are_defined() -> None:
    assert len(_fixture_paths()) == 3


def test_patterns_are_complete_declarative_and_secret_free() -> None:
    keys = set()
    for path in _fixture_paths():
        body, digest = sha256_of_verified_read(path, maximum_size=64 * 1024)
        payload = json.loads(body)
        assert set(payload) == EXPECTED_KEYS, path.name
        assert payload["schema_name"] == "captain.live-demo-conversation-pattern.v1"
        assert len(payload["turns"]) >= 3, path.name
        assert not SECRET_MARKERS.search(body.decode("utf-8")), path.name
        assert len(digest) == 64
        keys.add(payload["pattern_key"])
    assert keys == {"triage_handoff", "integration_lease", "resume_recovery"}


def test_pattern_digests_are_byte_stable() -> None:
    for path in _fixture_paths():
        _, first = sha256_of_verified_read(path, maximum_size=64 * 1024)
        _, second = sha256_of_verified_read(path, maximum_size=64 * 1024)
        assert first == second, path.name
