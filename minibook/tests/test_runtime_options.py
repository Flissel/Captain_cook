from __future__ import annotations

import pytest

from swarm.runtime_options import parse_runtime_options


def test_runtime_options_enable_noninteractive_deadline() -> None:
    options = parse_runtime_options(("--non-interactive", "--max-runtime-seconds", "120"))

    assert options.interactive is False
    assert options.max_runtime_seconds == 120.0


def test_runtime_options_bind_creation_job_and_result_files() -> None:
    options = parse_runtime_options(
        (
            "--creation-job-file",
            "work/creation-job.json",
            "--result-file",
            "work/creation-result.json",
            "--artifact-root",
            "work/.captain-cook/creation-cas",
        )
    )

    assert options.creation_job_file == "work/creation-job.json"
    assert options.result_file == "work/creation-result.json"
    assert options.artifact_root == "work/.captain-cook/creation-cas"


@pytest.mark.parametrize(
    "argv",
    (
        ("--creation-job-file", "work/creation-job.json"),
        ("--result-file", "work/creation-result.json"),
        (
            "--creation-job-file",
            "work/creation-job.json",
            "--result-file",
            "work/creation-result.json",
        ),
        ("--artifact-root", "work/.captain-cook/creation-cas"),
    ),
)
def test_runtime_options_require_complete_creation_paths(argv: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="creation-job-file.*result-file.*artifact-root"):
        parse_runtime_options(argv)


@pytest.mark.parametrize("argv", (("--max-runtime-seconds", "0"), ("--max-runtime-seconds", "invalid")))
def test_runtime_options_reject_invalid_deadlines(argv: tuple[str, str]) -> None:
    with pytest.raises(ValueError, match="max-runtime-seconds"):
        parse_runtime_options(argv)
