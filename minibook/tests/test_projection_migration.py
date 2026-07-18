from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import sys


MIGRATION = Path(__file__).parents[1] / "scripts" / "migrate_schema.py"


def test_unexpected_migration_error_rolls_back_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    database = tmp_path / "invalid-empty.db"

    result = subprocess.run(
        [sys.executable, str(MIGRATION), str(database)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Done." not in result.stdout
    with sqlite3.connect(database) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    assert tables == []
