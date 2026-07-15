import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import pymysql
from pymysql.cursors import DictCursor
from urllib.parse import unquote, urlparse

from blockchain.mariadb_storage import MariaDBStorage


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")


def execute_sql(sql: str, params: tuple[Any, ...] = ()) -> None:
    assert TEST_DSN is not None
    parsed = urlparse(TEST_DSN)
    connection = pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=unquote(parsed.path.lstrip("/")),
        cursorclass=DictCursor,
    )
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
        connection.commit()


def make_block(index: int, previous_hash: str) -> dict[str, Any]:
    return {
        "index": index,
        "block_type": "work_batch",
        "data": {"batch_id": f"batch-{index}"},
        "status": "pending",
        "previous_hash": previous_hash,
        "parent_index": None,
        "children": [],
        "metadata": {"source": "test"},
        "hash": f"hash-{index}",
    }


@pytest.fixture
def storage() -> MariaDBStorage:
    assert TEST_DSN is not None
    result = MariaDBStorage(TEST_DSN)
    result.clear()
    yield result
    result.clear()


def test_round_trips_a_chain(storage: MariaDBStorage) -> None:
    blocks = [make_block(0, "0"), make_block(1, "hash-0")]

    storage.save(blocks)

    assert storage.load() == blocks


def test_concurrent_appends_both_persist(storage: MariaDBStorage) -> None:
    storage.append_block(make_block(0, "0"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(storage.append_block, [make_block(1, "hash-0"), make_block(2, "hash-1")]))

    assert [block["index"] for block in storage.load()] == [0, 1, 2]


def test_malformed_row_does_not_wipe_valid_chain(storage: MariaDBStorage) -> None:
    storage.append_block(make_block(0, "0"))
    execute_sql(
        "INSERT INTO blocks (`index`, block_type, data, status, children, metadata, hash, previous_hash) "
        "VALUES (1, 'work_batch', '[]', 'pending', '[]', '{}', %s, %s)",
        ("1" * 64, "0" * 64),
    )

    with pytest.raises(ValueError, match="Malformed ledger row"):
        storage.load()

    execute_sql("DELETE FROM blocks WHERE `index` = 1")
    assert storage.load() == [make_block(0, "0")]


def test_status_projection_filters_in_sql(storage: MariaDBStorage) -> None:
    pending = make_block(0, "0")
    succeeded = make_block(1, "hash-0")
    succeeded["status"] = "succeeded"
    storage.save([pending, succeeded])

    assert storage.load_by_status("succeeded") == [succeeded]
