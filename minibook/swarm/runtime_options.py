"""Small, dependency-free runtime controls for one-shot Swarm runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class RuntimeOptions:
    interactive: bool = True
    max_runtime_seconds: float | None = None
    creation_job_file: str | None = None
    result_file: str | None = None
    skill_usage_receipt_file: str | None = None
    artifact_root: str | None = None
    source_archive_file: str | None = None


def parse_runtime_options(argv: Sequence[str]) -> RuntimeOptions:
    """Read only the runtime controls while leaving mode-specific parsing intact."""
    interactive = "--non-interactive" not in argv
    max_runtime_seconds: float | None = None
    creation_job_file = _option_value(argv, "--creation-job-file")
    result_file = _option_value(argv, "--result-file")
    skill_usage_receipt_file = _option_value(argv, "--skill-usage-receipt-file")
    artifact_root = _option_value(argv, "--artifact-root")
    source_archive_file = _option_value(argv, "--source-archive-file")
    creation_values = (
        creation_job_file,
        result_file,
        skill_usage_receipt_file,
        artifact_root,
    )
    if any(value is not None for value in creation_values) and not all(
        value is not None for value in creation_values
    ):
        raise ValueError(
            "--creation-job-file, --result-file, --skill-usage-receipt-file, "
            "and --artifact-root must be provided together"
        )
    if source_archive_file is not None and not all(
        value is not None for value in creation_values
    ):
        raise ValueError(
            "--source-archive-file requires --creation-job-file, --result-file, "
            "--skill-usage-receipt-file, and --artifact-root"
        )
    if "--max-runtime-seconds" in argv:
        index = argv.index("--max-runtime-seconds")
        if index + 1 >= len(argv):
            raise ValueError("--max-runtime-seconds requires a positive number")
        try:
            max_runtime_seconds = float(argv[index + 1])
        except ValueError as exc:
            raise ValueError("--max-runtime-seconds requires a positive number") from exc
        if max_runtime_seconds <= 0:
            raise ValueError("--max-runtime-seconds requires a positive number")
    return RuntimeOptions(
        interactive=interactive,
        max_runtime_seconds=max_runtime_seconds,
        creation_job_file=creation_job_file,
        result_file=result_file,
        skill_usage_receipt_file=skill_usage_receipt_file,
        artifact_root=artifact_root,
        source_archive_file=source_archive_file,
    )


def _option_value(argv: Sequence[str], name: str) -> str | None:
    if name not in argv:
        return None
    index = argv.index(name)
    if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
        raise ValueError(f"{name} requires a file path")
    value = argv[index + 1]
    if not value or "\x00" in value:
        raise ValueError(f"{name} requires a file path")
    return value
