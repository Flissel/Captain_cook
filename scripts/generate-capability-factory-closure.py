"""Generate the deterministic repository-local import closure for Factory runtime."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRY = "gateway.capability_factory_runtime_internal"
OUTPUT = ROOT / "gateway/capability_factory_runtime_closure.json"


def _path_for(module: str) -> Path | None:
    candidate = ROOT.joinpath(*module.split(".")).with_suffix(".py")
    return candidate if candidate.is_file() else None


def _imports(module: str, source: bytes) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(ast.parse(source, filename=module)):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            result.add(node.module)
    return {name for name in result if _path_for(name) is not None}


def main() -> int:
    pending, seen = [ENTRY], set()
    entries = []
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _path_for(module)
        if path is None:
            raise RuntimeError(f"repository module is unavailable: {module}")
        source = path.read_bytes()
        tree = ast.parse(source, filename=module)
        entries.append({
            "module": module,
            "path": path.relative_to(ROOT).as_posix(),
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "ast_sha256": hashlib.sha256(
                ast.dump(tree, annotate_fields=True, include_attributes=False).encode()
            ).hexdigest(),
            "head_blob": hashlib.sha1(
                b"blob " + str(len(source)).encode("ascii") + b"\0" + source
            ).hexdigest(),
        })
        pending.extend(sorted(_imports(module, source) - seen, reverse=True))
    entries.sort(key=lambda item: item["module"])
    config = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    payload = {
        "schema": "captain.capability-factory-runtime-closure.v1",
        "root_module": ENTRY,
        "root_revision_binding": "runtime-expected-head",
        "config_sha256": hashlib.sha256(config).hexdigest(),
        "entries": entries,
    }
    OUTPUT.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8", newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
