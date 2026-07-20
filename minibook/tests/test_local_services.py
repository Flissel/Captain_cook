from __future__ import annotations

from pathlib import Path

from swarm.local_services import ensure_frontend_dependencies


def test_frontend_dependencies_are_installed_when_next_is_missing(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], Path]] = []

    def runner(command: list[str], *, cwd: Path) -> int:
        calls.append((tuple(command), cwd))
        (cwd / "node_modules" / ".bin").mkdir(parents=True)
        (cwd / "node_modules" / ".bin" / "next").write_text("", encoding="utf-8")
        return 0

    assert ensure_frontend_dependencies(tmp_path, npm_command="npm", runner=runner) is True
    assert calls == [(('npm', 'ci', '--no-audit', '--no-fund'), tmp_path)]


def test_frontend_dependency_check_does_not_reinstall_next(tmp_path: Path) -> None:
    executable = tmp_path / "node_modules" / ".bin" / "next"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")

    assert ensure_frontend_dependencies(tmp_path, npm_command="npm", runner=lambda *_args, **_kwargs: 1) is True
