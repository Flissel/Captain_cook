"""Fail-closed file boundary for one-shot Captain creation runs."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .contracts import CreationJobV1, CreationResultV1


def load_creation_job(path: Path) -> CreationJobV1:
    if not path.is_file():
        raise FileNotFoundError("creation job file is unavailable")
    return CreationJobV1.model_validate_json(path.read_text(encoding="utf-8"))


def write_creation_result_atomic(path: Path, result: CreationResultV1) -> None:
    """Create one result exactly once; stale success files are never reused."""

    if path.exists():
        raise FileExistsError("creation result file already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        raise FileExistsError("creation result temporary file already exists")
    content = json.dumps(
        result.model_dump(mode="json", by_alias=True, exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
