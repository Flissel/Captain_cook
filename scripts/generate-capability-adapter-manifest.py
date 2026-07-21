"""Generate Package-C's digest-pinned static adapter manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agenten.agent_factory.capability_live_adapters import (
    write_capability_adapter_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--module-path", type=Path, required=True)
    parser.add_argument("--factory-symbol", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    written = write_capability_adapter_manifest(
        workspace_root=args.workspace_root,
        module_path=args.module_path,
        factory_symbol=args.factory_symbol,
        output_directory=args.output_directory,
    )
    print(
        json.dumps(
            {
                "schema": "captain.capability-factory-adapter-manifest.v2",
                "manifest_path": str(written.path),
                "manifest_sha256": written.sha256,
                "module_sha256": written.module_sha256,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
