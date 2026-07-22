"""Regression coverage for the generated AutoGen runtime termination contract."""

from minibook.swarm.knowledge import GENERIC_MAIN_PY


def test_generated_runtime_honors_project_message_limit() -> None:
    """A project.yml limit must not be silently expanded by the loader."""
    assert "MaxMessageTermination(term_val)" in GENERIC_MAIN_PY
    assert "max(term_val, 50)" not in GENERIC_MAIN_PY
