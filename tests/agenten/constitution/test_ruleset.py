"""Unit tests for load_constitution's YAML-loading body (unit U2)."""
import textwrap

from agenten.constitution.ruleset import ConstitutionRuleset, load_constitution


def test_load_default_constitution_from_repo_yaml():
    ruleset = load_constitution()
    assert isinstance(ruleset, ConstitutionRuleset)
    assert ruleset.version
    assert ruleset.scope_statement
    assert ruleset.quality_rubric
    assert isinstance(ruleset.prohibited_topics, list)
    assert ruleset.default_budget.max_depth == 4


def test_load_constitution_from_explicit_path(tmp_path):
    yaml_path = tmp_path / "custom.yaml"
    yaml_path.write_text(
        textwrap.dedent(
            """
            version: "custom-v3"
            scope_statement: "Only widgets."
            quality_rubric: "Must be a widget."
            prohibited_topics:
              - forbidden_thing
            default_budget:
              max_depth: 2
              max_total_subproblems: 10
              max_fanout_per_node: 3
            """
        )
    )
    ruleset = load_constitution(str(yaml_path))
    assert ruleset.version == "custom-v3"
    assert ruleset.scope_statement == "Only widgets."
    assert ruleset.prohibited_topics == ["forbidden_thing"]
    assert ruleset.default_budget.max_depth == 2
    assert ruleset.default_budget.max_total_subproblems == 10
    assert ruleset.default_budget.max_fanout_per_node == 3
    assert ruleset.default_budget.max_tokens is None


def test_load_constitution_fills_missing_optional_fields():
    yaml_path_content = "version: \"minimal-v1\"\n"
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(yaml_path_content)
        ruleset = load_constitution(path)
        assert ruleset.version == "minimal-v1"
        assert ruleset.scope_statement == ""
        assert ruleset.quality_rubric == ""
        assert ruleset.prohibited_topics == []
        assert ruleset.default_budget.max_depth == 4  # DecompositionBudget default
    finally:
        os.remove(path)
