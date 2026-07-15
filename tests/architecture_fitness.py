"""Small AST-based architecture checks used by the test suite."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ImportedModule:
    name: str
    line: int


@dataclass(frozen=True)
class BoundaryRule:
    source_prefixes: tuple[str, ...]
    forbidden_import_prefixes: tuple[str, ...]
    reason: str
    allowed_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class BoundaryViolation:
    source: Path
    imported: ImportedModule
    reason: str

    def __str__(self) -> str:
        return (
            f"{self.source.as_posix()}:{self.imported.line} imports "
            f"{self.imported.name}: {self.reason}"
        )


def imports_in_file(path: Path) -> list[ImportedModule]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[ImportedModule] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(ImportedModule(alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(ImportedModule(node.module, node.lineno))
    return sorted(imports, key=lambda item: item.line)


def find_boundary_violations(
    root: Path,
    rules: Iterable[BoundaryRule],
) -> list[BoundaryViolation]:
    configured_rules = tuple(rules)
    top_level_packages = {
        prefix.split(".", maxsplit=1)[0]
        for rule in configured_rules
        for prefix in rule.source_prefixes
    }
    violations: list[BoundaryViolation] = []

    for top_level in sorted(top_level_packages):
        package_root = root / top_level
        if not package_root.exists():
            continue
        for path in sorted(package_root.rglob("*.py")):
            source = _module_name(root, path)
            for rule in configured_rules:
                if not _matches_any(source, rule.source_prefixes):
                    continue
                if _matches_any(source, rule.allowed_sources):
                    continue
                for imported in imports_in_file(path):
                    if _matches_any(imported.name, rule.forbidden_import_prefixes):
                        violations.append(
                            BoundaryViolation(
                                source=path.relative_to(root),
                                imported=imported,
                                reason=rule.reason,
                            )
                        )
    return violations


def find_import_cycles(
    root: Path,
    package_prefixes: tuple[str, ...],
) -> list[tuple[str, ...]]:
    modules: dict[str, Path] = {}
    top_level_packages = {prefix.split(".", maxsplit=1)[0] for prefix in package_prefixes}
    for top_level in sorted(top_level_packages):
        package_root = root / top_level
        if not package_root.exists():
            continue
        for path in sorted(package_root.rglob("*.py")):
            module = _module_name(root, path)
            if _matches_any(module, package_prefixes):
                modules[module] = path

    graph: dict[str, set[str]] = {module: set() for module in modules}
    for module, path in modules.items():
        for imported in imports_in_file(path):
            if imported.name in modules:
                graph[module].add(imported.name)

    state: dict[str, int] = {module: 0 for module in modules}
    stack: list[str] = []
    positions: dict[str, int] = {}
    cycles: set[tuple[str, ...]] = set()

    def visit(module: str) -> None:
        state[module] = 1
        positions[module] = len(stack)
        stack.append(module)
        for target in sorted(graph[module]):
            if state[target] == 0:
                visit(target)
            elif state[target] == 1:
                cycle_body = stack[positions[target] :]
                cycles.add(_canonical_cycle(cycle_body))
        stack.pop()
        positions.pop(module)
        state[module] = 2

    for module in sorted(modules):
        if state[module] == 0:
            visit(module)
    return sorted(cycles)


def _module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _matches_any(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)


def _canonical_cycle(cycle_body: list[str]) -> tuple[str, ...]:
    rotations = [cycle_body[index:] + cycle_body[:index] for index in range(len(cycle_body))]
    canonical = min(rotations)
    return tuple(canonical + [canonical[0]])
