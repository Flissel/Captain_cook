"""Verify that Captain Cook's judge-facing submission evidence is complete."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REQUIRED_FILES = (
    "README.md",
    "artifacts/demo-run.json",
    "docs/DEMO.md",
    "docs/VIDEO_SCRIPT.md",
    "docs/DEVPOST_CHECKLIST.md",
    "docs/THIRD_PARTY_NOTICES.md",
    "docs/WORKSTREAMS.md",
    "docs/MCP_SETUP.md",
    "docs/codex-sessions.md",
)
REQUIRED_ARTIFACT_FIELDS = ("success", "problem_id", "done_count", "blocks")


def validate_submission(root: Path) -> list[str]:
    """Return every missing or malformed evidence requirement under ``root``."""
    errors = [
        f"Missing required file: {path}"
        for path in REQUIRED_FILES
        if not (root / path).is_file()
    ]
    artifact_path = root / "artifacts/demo-run.json"
    if not artifact_path.is_file():
        return errors

    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        errors.append(f"Evidence artifact is invalid JSON: {error.msg}")
        return errors

    if not isinstance(artifact, dict):
        errors.append("Evidence artifact must be a JSON object")
        return errors

    for field in REQUIRED_ARTIFACT_FIELDS:
        if field not in artifact:
            errors.append(f"Evidence artifact missing field: {field}")
    if artifact.get("success") is not True:
        errors.append("Evidence artifact is not successful")
    if not isinstance(artifact.get("blocks"), list):
        errors.append("Evidence artifact field 'blocks' must be a list")
    return errors


def main() -> int:
    errors = validate_submission(Path.cwd())
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("Submission evidence check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
