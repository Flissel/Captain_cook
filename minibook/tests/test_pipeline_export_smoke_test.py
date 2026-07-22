from __future__ import annotations

from pathlib import Path

from minibook.swarm.pipeline import _render_export_smoke_test


def test_export_smoke_test_checks_packaged_autogen_modules(tmp_path: Path) -> None:
    package = tmp_path / "package"
    tests = package / "tests"
    sources = package / "autogen"
    tests.mkdir(parents=True)
    sources.mkdir()
    (sources / "main.py").write_text("print('ready')\n", encoding="utf-8")
    test_path = tests / "test_generated_team.py"
    test_path.write_text(_render_export_smoke_test(), encoding="utf-8")

    namespace: dict[str, object] = {"__file__": str(test_path)}
    exec(compile(test_path.read_text(encoding="utf-8"), str(test_path), "exec"), namespace)
    namespace["test_generated_autogen_sources_compile"]()
