from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_captain_builder_compose_isolated_from_vibemind() -> None:
    compose = (ROOT / "docker-compose.captain-n8n.yml").read_text(encoding="utf-8")

    assert "name: captain-n8n-builder" in compose
    assert '127.0.0.1:${CAPTAIN_N8N_PORT:-5679}:5678' in compose
    assert "DB_TYPE=postgresdb" in compose
    assert "docker.sock" not in compose
    assert "vibemind" not in compose.lower()
    assert "external: true" not in compose
    assert "captain_n8n_postgres_data" in compose
    assert "captain_n8n_data" in compose
