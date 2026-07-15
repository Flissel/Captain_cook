from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_compose_owns_mailpit_and_mariadb_but_not_n8n() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "  mailpit:" in compose
    assert "  mariadb:" in compose
    assert "  n8n:" not in compose
    assert "ledger_data:/var/lib/mysql" in compose


def test_env_example_documents_external_n8n_and_local_ports() -> None:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")

    for entry in (
        "N8N_URL=http://localhost:15678",
        "N8N_CONTAINER_URL=http://host.docker.internal:15678",
        "MAILPIT_WEB_PORT=8025",
        "MAILPIT_SMTP_PORT=1025",
        "MARIADB_PORT=3306",
    ):
        assert entry in env
