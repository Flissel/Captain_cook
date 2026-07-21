from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_opt_in_main_starts_and_stops_background_creation_runtime(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "MINIBOOK_CREATION_DB": str(tmp_path / "creation.sqlite3"),
            "MINIBOOK_CREATION_ARTIFACTS": str(tmp_path / "artifacts"),
            "MINIBOOK_API_KEY": "isolated-test-key",
            "MINIBOOK_TEST_CORE_DB": str(tmp_path / "minibook.sqlite3"),
        }
    )
    program = """
import os
from fastapi.testclient import TestClient
from minibook.src import main

assert main._configured_creation is not None
assert main._configured_creation.runtime.active_job_ids == ()
main.DB_PATH = os.environ["MINIBOOK_TEST_CORE_DB"]
with TestClient(main.app) as client:
    assert main._configured_creation.runtime._loop is not None
    response = client.get(
        "/api/v1/creation-capabilities",
        headers={"Authorization": "Bearer isolated-test-key"},
    )
    assert response.status_code == 200
    assert response.json()["creation_jobs"] is True
assert main._configured_creation.runtime._loop is None
"""
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
