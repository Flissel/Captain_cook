"""Render a review-only TO_BE_BUILT migration candidate."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agenten.agent_factory.input_migration import render_migration_candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overwrite-candidate", action="store_true")
    args = parser.parse_args(argv)
    source = args.source.resolve()
    output = args.output.resolve()
    if source == output or (source.name == "TO_BE_BUILT.md" and output == source):
        print("refusing to overwrite canonical source", file=sys.stderr)
        return 1
    if output.exists() and not args.overwrite_candidate:
        print("candidate output already exists", file=sys.stderr)
        return 1
    try:
        report = render_migration_candidate(source.read_bytes())
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report.candidate, encoding="utf-8", newline="\n")
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"migration failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"findings={len(report.findings)} output={output}")
    return 2 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
