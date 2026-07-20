from __future__ import annotations

import pytest

from swarm.runtime_options import parse_runtime_options


def test_runtime_options_enable_noninteractive_deadline() -> None:
    options = parse_runtime_options(("--non-interactive", "--max-runtime-seconds", "120"))

    assert options.interactive is False
    assert options.max_runtime_seconds == 120.0


@pytest.mark.parametrize("argv", (("--max-runtime-seconds", "0"), ("--max-runtime-seconds", "invalid")))
def test_runtime_options_reject_invalid_deadlines(argv: tuple[str, str]) -> None:
    with pytest.raises(ValueError, match="max-runtime-seconds"):
        parse_runtime_options(argv)
