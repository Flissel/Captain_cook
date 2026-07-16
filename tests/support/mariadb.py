"""Safety guard for destructive MariaDB integration tests."""

from __future__ import annotations

from urllib.parse import unquote, urlparse


ISOLATED_TEST_DATABASE = "captain_test"


def assert_isolated_test_database(dsn: str | None) -> None:
    """Reject any DSN that does not target the disposable test database."""
    message = "TEST_MARIADB_DSN must target the exact isolated database captain_test"
    if not isinstance(dsn, str) or not dsn:
        raise ValueError(message)

    try:
        parsed = urlparse(dsn)
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc

    database = unquote(parsed.path[1:]) if parsed.path.startswith("/") else ""
    if (
        parsed.scheme not in {"mysql", "mariadb"}
        or not parsed.hostname
        or database != ISOLATED_TEST_DATABASE
    ):
        raise ValueError(message)
