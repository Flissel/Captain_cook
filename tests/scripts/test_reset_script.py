from pathlib import Path


SCRIPT = Path("scripts/reset.sh")


def test_reset_script_preserves_learning_and_n8n_volumes() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "hermes" not in text.lower()
    assert "docker volume rm" not in text
    assert "down -v" not in text
    assert "n8n_data" not in text


def test_reset_script_orders_quiesce_archive_then_external_cleanup() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    disable = text.index("disable_worker_crons")
    stop = text.index("stop_workers")
    archive = text.index("archive_run")
    n8n = text.index("delete_n8n_workflows")
    mailpit = text.index("wipe_mailpit")

    assert disable < stop < archive < n8n < mailpit


def test_reset_script_supports_optional_minibook_reset() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "--wipe-minibook" in text
    assert "minibook.db" in text
