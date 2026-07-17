import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def imported_modules(directory: Path) -> set[str]:
    imports: set[str] = set()
    for path in directory.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
    return imports


def test_planning_does_not_import_execution_or_review_processes() -> None:
    imports = imported_modules(ROOT / "agenten" / "planning")

    assert not any(module.startswith("agenten.execution") for module in imports)
    assert not any(module.startswith("agenten.review") for module in imports)


def test_review_has_no_execution_release_or_storage_authority() -> None:
    imports = imported_modules(ROOT / "agenten" / "review")
    forbidden = (
        "agenten.execution",
        "agenten.delivery",
        "agenten.planning.canonical_plan",
        "agenten.planning.canonical_compiler",
        "agenten.planning.canonical_publisher",
        "gateway",
        "blockchain",
    )

    assert not any(module.startswith(forbidden) for module in imports)
    assert "agenten.planning.canonical_contracts" in imports


def test_execution_consumes_review_contract_but_not_review_process() -> None:
    imports = imported_modules(ROOT / "agenten" / "execution")

    assert "agenten.review.contracts" in imports
    assert "agenten.review.process" not in imports
    assert "agenten.review.artifacts" not in imports
