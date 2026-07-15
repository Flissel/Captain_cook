from pathlib import Path

from tests import architecture_fitness
from tests.architecture_fitness import (
    BoundaryRule,
    ImportedModule,
    find_boundary_violations,
    find_import_cycles,
    imports_in_file,
)


ROOT = Path(__file__).parents[1]


def test_import_parser_handles_import_and_from_import(tmp_path: Path):
    source = tmp_path / "sample.py"
    source.write_text(
        "import agenten.runtime.event_bus\n"
        "from blockchain.storage import LedgerStorage\n",
        encoding="utf-8",
    )

    assert imports_in_file(source) == [
        ImportedModule("agenten.runtime.event_bus", 1),
        ImportedModule("blockchain.storage", 2),
    ]


def test_boundary_rule_reports_source_target_and_line(tmp_path: Path):
    source = tmp_path / "agenten" / "events" / "bad.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from agenten.orchestration.pipeline import build_pipeline\n",
        encoding="utf-8",
    )
    rule = BoundaryRule(
        source_prefixes=("agenten.events",),
        forbidden_import_prefixes=("agenten.orchestration",),
        reason="event contracts must not depend on composition",
    )

    violations = find_boundary_violations(tmp_path, [rule])

    assert len(violations) == 1
    assert str(violations[0]) == (
        "agenten/events/bad.py:1 imports agenten.orchestration.pipeline: "
        "event contracts must not depend on composition"
    )


def test_root_runtime_respects_declared_architecture_boundaries():
    core_packages = (
        "agenten.events",
        "agenten.runtime",
        "agenten.decomposition",
        "agenten.constitution",
        "agenten.spawning",
        "agenten.workers",
        "agenten.supervision",
        "agenten.ledger_bridge",
        "agenten.tools",
        "agenten.household",
    )
    rules = [
        BoundaryRule(
            source_prefixes=core_packages,
            forbidden_import_prefixes=(
                "agenten.orchestration",
                "agenten.demo",
                "agenten.Captain",
                "agenten.workflows",
                "chats",
            ),
            reason="core runtime must not depend on composition or compatibility entrypoints",
        ),
        BoundaryRule(
            source_prefixes=("agenten", "blockchain", "chats", "config"),
            forbidden_import_prefixes=("minibook", "hermes_agent", "hermes-agent"),
            reason="adjacent products must stay outside the root runtime import graph",
        ),
        BoundaryRule(
            source_prefixes=("blockchain",),
            forbidden_import_prefixes=("agenten",),
            allowed_sources=("blockchain.web_scamler",),
            reason="ledger infrastructure must not depend on agent implementations",
        ),
    ]

    violations = find_boundary_violations(ROOT, rules)

    assert violations == [], "\n" + "\n".join(str(item) for item in violations)


def test_import_cycle_detection_returns_canonical_cycle(tmp_path: Path):
    package = tmp_path / "sample"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "a.py").write_text("from sample.b import value\n", encoding="utf-8")
    (package / "b.py").write_text("from sample.c import value\n", encoding="utf-8")
    (package / "c.py").write_text("from sample.a import value\n", encoding="utf-8")

    assert find_import_cycles(tmp_path, ("sample",)) == [
        ("sample.a", "sample.b", "sample.c", "sample.a")
    ]


def test_root_runtime_has_no_internal_import_cycles():
    packages = (
        "agenten.events",
        "agenten.runtime",
        "agenten.decomposition",
        "agenten.constitution",
        "agenten.spawning",
        "agenten.workers",
        "agenten.supervision",
        "agenten.ledger_bridge",
        "agenten.tools",
        "agenten.household",
    )

    cycles = find_import_cycles(ROOT, packages)

    assert cycles == [], "\n" + "\n".join(" -> ".join(cycle) for cycle in cycles)


def test_symbol_reference_rule_reports_names_and_attributes(tmp_path: Path):
    direct = tmp_path / "agenten" / "direct.py"
    direct.parent.mkdir(parents=True)
    direct.write_text("store = MariaDBStorage()\n", encoding="utf-8")
    aliased = tmp_path / "agenten" / "aliased.py"
    aliased.write_text(
        "from blockchain.mariadb_storage import MariaDBStorage as Storage\n"
        "store = Storage()\n",
        encoding="utf-8",
    )
    qualified = tmp_path / "blockchain" / "qualified.py"
    qualified.parent.mkdir(parents=True)
    qualified.write_text("store = storage.MariaDBStorage()\n", encoding="utf-8")
    allowed = tmp_path / "gateway" / "allowed.py"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("store = MariaDBStorage()\n", encoding="utf-8")
    foreign_worktree = tmp_path / ".worktrees" / "other" / "foreign.py"
    foreign_worktree.parent.mkdir(parents=True)
    foreign_worktree.write_text("store = MariaDBStorage()\n", encoding="utf-8")

    finder = getattr(architecture_fitness, "find_symbol_references", None)
    assert finder is not None, "find_symbol_references is not implemented"

    references = finder(
        tmp_path,
        symbol="MariaDBStorage",
        allowed_paths=("gateway/",),
    )

    assert [str(item) for item in references] == [
        "agenten/aliased.py:1 references MariaDBStorage",
        "agenten/direct.py:1 references MariaDBStorage",
        "blockchain/qualified.py:1 references MariaDBStorage",
    ]


def test_only_gateway_may_reference_mariadb_storage():
    finder = getattr(architecture_fitness, "find_symbol_references", None)
    assert finder is not None, "find_symbol_references is not implemented"

    violations = finder(
        ROOT,
        symbol="MariaDBStorage",
        allowed_paths=(
            "gateway/",
            "tests/",
            "scripts/migrate_sqlite_delivery_ledger.py",
        ),
    )

    assert violations == [], "\n" + "\n".join(str(item) for item in violations)
