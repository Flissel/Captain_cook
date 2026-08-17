"""Regenerate the repository-owned capability Factory descriptor deterministically."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "gateway/capability_factory_runtime_adapter.py"
DESCRIPTOR = ROOT / "gateway/capability_factory_runtime_descriptor.json"
IMPLEMENTATION = ROOT / "gateway/capability_factory_runtime_internal.py"


def main() -> int:
    source = ADAPTER.read_bytes()
    tree = ast.parse(source, filename=ADAPTER.as_posix())
    payload = json.loads(DESCRIPTOR.read_text(encoding="utf-8"))
    payload["adapter_sha256"] = hashlib.sha256(source).hexdigest()
    payload["adapter_ast_sha256"] = hashlib.sha256(
        ast.dump(tree, annotate_fields=True, include_attributes=False).encode("utf-8")
    ).hexdigest()
    implementation = IMPLEMENTATION.read_bytes()
    payload["implementation_path"] = IMPLEMENTATION.relative_to(ROOT).as_posix()
    payload["implementation_sha256"] = hashlib.sha256(implementation).hexdigest()
    payload["implementation_ast_sha256"] = hashlib.sha256(
        ast.dump(
            ast.parse(implementation, filename=IMPLEMENTATION.as_posix()),
            annotate_fields=True,
            include_attributes=False,
        ).encode("utf-8")
    ).hexdigest()
    payload["implementation_head_blob"] = hashlib.sha1(
        b"blob " + str(len(implementation)).encode("ascii") + b"\0" + implementation
    ).hexdigest()
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    DESCRIPTOR.write_text(content, encoding="utf-8", newline="\n")
    print(f"CAPTAIN_CAPABILITY_FACTORY_ADAPTER_SHA256={payload['adapter_sha256']}")
    print(
        "CAPTAIN_CAPABILITY_FACTORY_PLAN_SHA256="
        + hashlib.sha256(content.encode("utf-8")).hexdigest()
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
