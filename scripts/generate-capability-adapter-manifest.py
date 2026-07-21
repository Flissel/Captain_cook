"""Generate canonical digest-pinned capability adapter manifests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agenten.agent_factory.capability_factory_production import (
    AdapterManifestKind,
    generate_adapter_manifest,
)
from agenten.agent_factory.capability_live_adapters import (
    write_capability_adapter_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", type=Path, default=Path.cwd())
    module_group = parser.add_mutually_exclusive_group(required=True)
    module_group.add_argument("--module", type=Path)
    module_group.add_argument("--module-path", type=Path)
    parser.add_argument("--factory-symbol", required=True)
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--target", type=Path)
    target_group.add_argument("--output-directory", type=Path)
    parser.add_argument(
        "--kind",
        choices=tuple(kind.value for kind in AdapterManifestKind),
    )
    arguments = parser.parse_args()
    module_path = arguments.module or arguments.module_path
    assert module_path is not None

    if arguments.output_directory is not None:
        if arguments.kind not in (None, AdapterManifestKind.ENTRYPOINT.value):
            parser.error("--output-directory supports only the entrypoint manifest")
        written = write_capability_adapter_manifest(
            workspace_root=arguments.workspace_root,
            module_path=module_path,
            factory_symbol=arguments.factory_symbol,
            output_directory=arguments.output_directory,
        )
        report = {
            "schema": "captain.capability-factory-adapter-manifest.v2",
            "manifest_path": str(written.path),
            "manifest_sha256": written.sha256,
            "module_sha256": written.module_sha256,
        }
    else:
        if arguments.kind is None:
            parser.error("--kind is required with --target")
        assert arguments.target is not None
        generated = generate_adapter_manifest(
            workspace_root=arguments.workspace_root,
            module_path=module_path,
            factory_symbol=arguments.factory_symbol,
            target_path=arguments.target,
            kind=AdapterManifestKind(arguments.kind),
        )
        report = {
            "manifest_path": str(generated.path),
            "manifest_sha256": generated.sha256,
            "module_sha256": generated.module_sha256,
            "kind": generated.kind.value,
        }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
