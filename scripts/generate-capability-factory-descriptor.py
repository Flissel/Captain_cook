"""Regenerate the repository-owned capability Factory descriptor deterministically."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "gateway/capability_factory_runtime_adapter.py"
DESCRIPTOR = ROOT / "gateway/capability_factory_runtime_descriptor.json"


def main() -> int:
    source = ADAPTER.read_bytes()
    tree = ast.parse(source, filename=ADAPTER.as_posix())
    payload = json.loads(DESCRIPTOR.read_text(encoding="utf-8"))
    payload["adapter_sha256"] = hashlib.sha256(source).hexdigest()
    payload["adapter_ast_sha256"] = hashlib.sha256(
        ast.dump(tree, annotate_fields=True, include_attributes=False).encode("utf-8")
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
