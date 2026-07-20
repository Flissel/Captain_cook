from pathlib import Path


def test_captain_skill_evaluation_operational_chain_is_explicit() -> None:
    """Task 7 must expose one auditable Captain-owned operational chain."""

    architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")

    assert "## Hermes skill-evaluation release path" in architecture
    assert (
        "request -> skill usage -> build/test evidence -> candidate retained "
        "-> Gateway validation -> skill published -> ready-to-use promotion"
        in architecture
    )

