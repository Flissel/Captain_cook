"""Initialize a local Captain-private benchmark catalog without revealing its seed."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Sequence, TextIO
import sys

from agenten.agent_factory.input_document import load_factory_input
from agenten.agent_factory.private_benchmark_catalog import (
    VerifiedFilePrivateBenchmarkCatalog,
    repository_forbidden_roots,
    write_private_benchmark_catalog,
)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
) -> int:
    arguments = _parser().parse_args(argv)
    repository_root = Path(arguments.repository_root).resolve(strict=True)
    private_root = Path(arguments.private_root).resolve(strict=False)
    manifest_path = _safe_relative(arguments.manifest_path)
    input_paths = tuple(_safe_relative(item) for item in arguments.input)
    documents = tuple(
        load_factory_input(_repository_input(repository_root, path))
        for path in input_paths
    )
    forbidden_roots = repository_forbidden_roots(repository_root)
    target = private_root.joinpath(*PurePosixPath(manifest_path).parts)
    if target.is_file() and not arguments.seed_stdin:
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        VerifiedFilePrivateBenchmarkCatalog.open(
            private_root=private_root,
            manifest_path=manifest_path,
            expected_sha256=digest,
            expected_input_sha256s=tuple(item.input_ref.sha256 for item in documents),
            forbidden_roots=forbidden_roots,
        )
    else:
        seed = os.urandom(32) if not arguments.seed_stdin else _ephemeral_stdin_seed(
            sys.stdin if stdin is None else stdin
        )
        reference = write_private_benchmark_catalog(
            private_root,
            documents,
            seed=seed,
            manifest_path=manifest_path,
            forbidden_roots=forbidden_roots,
        )
        digest = reference.sha256
    print(json.dumps({
        "private_root": str(private_root),
        "manifest_path": manifest_path,
        "manifest_sha256": digest,
        "input_sha256s": [item.input_ref.sha256 for item in documents],
    }, sort_keys=True, separators=(",", ":")))
    return 0


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("catalog and input paths must be safe relative paths")
    return path.as_posix()


def _repository_input(repository_root: Path, relative_path: str) -> Path:
    lexical = repository_root
    linked = False
    for part in PurePosixPath(relative_path).parts:
        lexical /= part
        linked = linked or lexical.is_symlink()
    resolved = lexical.resolve(strict=True)
    if not resolved.is_relative_to(repository_root) or linked:
        raise ValueError("catalog input path escapes repository authority")
    return resolved


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--private-root", required=True)
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--manifest-path", default="catalog.json")
    parser.add_argument(
        "--seed-stdin", action="store_true",
        help="read an ephemeral seed from standard input without persisting it",
    )
    return parser


def _ephemeral_stdin_seed(stream: TextIO) -> bytes:
    seed = stream.readline().rstrip("\r\n").encode("utf-8")
    if len(seed) < 32:
        raise ValueError("private benchmark seed requires at least 256 bits")
    return seed


if __name__ == "__main__":
    raise SystemExit(main())
