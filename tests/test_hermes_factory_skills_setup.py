from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "configure-hermes-factory-skills.ps1"
SKILL_NAMES = (
    "captain-factory-discover",
    "captain-factory-brief-codex",
    "captain-factory-execute-team",
    "captain-factory-evaluate-team",
    "captain-factory-improve-team",
    "captain-factory-report-captain",
)


def _write_fake_hermes(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "fake_hermes.py"
    fake.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            import json
            import os
            from pathlib import Path
            import sys

            import yaml


            args = sys.argv[1:]
            home = Path(os.environ["HERMES_HOME"])
            config_path = home / "config.yaml"


            def load_config() -> dict:
                if not config_path.exists():
                    return {}
                return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}


            def save_config(config: dict) -> None:
                home.mkdir(parents=True, exist_ok=True)
                config_path.write_text(
                    yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
                )


            def external_dirs(config: dict) -> list[str]:
                value = config.get("skills", {}).get("external_dirs", [])
                return [value] if isinstance(value, str) else list(value)


            def resolved_external_dirs(config: dict) -> list[Path]:
                roots = []
                for value in external_dirs(config):
                    root = Path(os.path.expandvars(value)).expanduser()
                    roots.append(root.resolve() if root.is_absolute() else (home / root).resolve())
                return roots


            def discovered_skills(root: Path):
                if not root.is_dir():
                    return
                for skill_file in sorted(root.rglob("SKILL.md")):
                    relative_parts = skill_file.relative_to(root).parts
                    if any(
                        part
                        in {
                            ".git",
                            ".github",
                            ".hub",
                            ".archive",
                            ".venv",
                            "venv",
                            "node_modules",
                            "site-packages",
                            "__pycache__",
                            ".tox",
                            ".nox",
                            ".pytest_cache",
                            ".mypy_cache",
                            ".ruff_cache",
                        }
                        for part in relative_parts
                    ):
                        continue
                    content = skill_file.read_text(encoding="utf-8-sig")
                    frontmatter = {}
                    if content.startswith("---"):
                        parts = content.split("---", 2)
                        if len(parts) == 3:
                            frontmatter = yaml.safe_load(parts[1]) or {}
                    yield str(frontmatter.get("name") or skill_file.parent.name), skill_file


            home.mkdir(parents=True, exist_ok=True)
            with (home / "calls.log").open("a", encoding="utf-8") as log:
                log.write(json.dumps(args) + "\\n")

            if args[:2] == ["config", "get"]:
                value = load_config().get("skills", {}).get("external_dirs", [])
                print(json.dumps(value))
            elif args[:2] == ["config", "set"]:
                config = load_config()
                config.setdefault("skills", {})["external_dirs"] = json.loads(args[3])
                save_config(config)
                print("set skills.external_dirs")
            elif args[:2] == ["config", "unset"]:
                config = load_config()
                config.setdefault("skills", {}).pop("external_dirs", None)
                save_config(config)
                print("unset skills.external_dirs")
            elif args[:2] == ["skills", "list"]:
                config = load_config()
                disabled = set(config.get("skills", {}).get("disabled", []))
                seen = set()
                for source, root in [("local", home / "skills")] + [
                    ("external", path) for path in resolved_external_dirs(config)
                ]:
                    for name, skill_file in discovered_skills(root) or ():
                        if name in seen:
                            continue
                        seen.add(name)
                        if name in disabled:
                            continue
                        print(f"{name} | {source} | enabled | {skill_file.parent.resolve()}")
            elif args[:2] == ["bundles", "create"]:
                name = args[2]
                skills = [args[index + 1] for index, value in enumerate(args) if value == "--skill"]
                description = args[args.index("--description") + 1]
                instruction = args[args.index("--instruction") + 1]
                bundle_dir = home / "skill-bundles"
                bundle_dir.mkdir(parents=True, exist_ok=True)
                (bundle_dir / f"{name}.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "name": name,
                            "description": description,
                            "skills": skills,
                            "instruction": instruction,
                        },
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )
                print(f"Created bundle: /{name}")
            elif args[:2] == ["bundles", "show"]:
                name = args[2]
                bundle = yaml.safe_load(
                    (home / "skill-bundles" / f"{name}.yaml").read_text(encoding="utf-8")
                )
                print(f"/{name}")
                for skill in bundle["skills"]:
                    print(f"- {skill}")
            elif args[:2] == ["bundles", "delete"]:
                name = args[2]
                bundle_path = home / "skill-bundles" / f"{name}.yaml"
                if not bundle_path.exists():
                    print(f"Bundle not found: /{name}", file=sys.stderr)
                    raise SystemExit(1)
                bundle_path.unlink()
                print(f"Deleted bundle: /{name}")
            else:
                raise SystemExit(f"unsupported fake Hermes command: {args}")
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    command = bin_dir / "hermes.ps1"
    command.write_text(
        f'& "{sys.executable}" "{fake}" @args\nexit $LASTEXITCODE\n',
        encoding="utf-8",
    )
    return bin_dir


def _environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    if shutil.which("pwsh") is None:
        pytest.skip("PowerShell 7 is required")
    bin_dir = _write_fake_hermes(tmp_path)
    hermes_home = tmp_path / "hermes"
    env = dict(os.environ)
    env["HERMES_HOME"] = str(hermes_home)
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    return env, hermes_home


def _command(repository_root: Path, *, remove: bool = False) -> list[str]:
    command = [
        "pwsh",
        "-NoProfile",
        "-File",
        str(SCRIPT),
        "-RepositoryRoot",
        str(repository_root),
    ]
    if remove:
        command.append("-Remove")
    return command


def _copy_skill_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    destination = repository / "agenten" / "agent_factory" / "skills"
    shutil.copytree(ROOT / "agenten" / "agent_factory" / "skills", destination)
    return repository


def test_configure_script_preserves_external_dirs_and_is_idempotent(
    tmp_path: Path,
) -> None:
    env, hermes_home = _environment(tmp_path)
    unrelated = (tmp_path / "unrelated-skills").resolve()
    unrelated.mkdir()
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"skills": {"external_dirs": [str(unrelated)]}}),
        encoding="utf-8",
    )

    first = subprocess.run(
        _command(ROOT), env=env, text=True, capture_output=True, check=True
    )
    second = subprocess.run(
        _command(ROOT), env=env, text=True, capture_output=True, check=True
    )

    skill_root = str((ROOT / "agenten" / "agent_factory" / "skills").resolve())
    config = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert config["skills"]["external_dirs"] == [str(unrelated), skill_root]
    assert config["skills"]["external_dirs"].count(skill_root) == 1
    bundle = yaml.safe_load(
        (hermes_home / "skill-bundles" / "captain-agent-factory-loop.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert tuple(bundle["skills"]) == SKILL_NAMES
    assert "configured" in first.stdout.lower()
    assert "already configured" in second.stdout.lower()


def test_configure_script_rejects_changed_or_missing_released_skill(
    tmp_path: Path,
) -> None:
    env, hermes_home = _environment(tmp_path)
    repository = _copy_skill_repository(tmp_path)
    changed = (
        repository
        / "agenten"
        / "agent_factory"
        / "skills"
        / "captain-factory-discover"
        / "SKILL.md"
    )
    changed.write_text(changed.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")

    result = subprocess.run(
        _command(repository), env=env, text=True, capture_output=True
    )

    assert result.returncode != 0
    assert "digest mismatch" in (result.stdout + result.stderr).lower()
    assert not (hermes_home / "config.yaml").exists()


@pytest.mark.parametrize("source", ("local", "external"))
def test_configure_script_rejects_nested_frontmatter_shadow(
    tmp_path: Path, source: str
) -> None:
    env, hermes_home = _environment(tmp_path)
    shadow_root = (
        hermes_home / "skills" if source == "local" else tmp_path / "shadow-skills"
    )
    shadow = shadow_root / "nested" / "alias-directory"
    shadow.mkdir(parents=True)
    (shadow / "SKILL.md").write_text(
        f"---\nname: {SKILL_NAMES[0]}\ndescription: shadow\n---\n",
        encoding="utf-8",
    )
    if source == "external":
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            yaml.safe_dump({"skills": {"external_dirs": [str(shadow_root.resolve())]}}),
            encoding="utf-8",
        )

    result = subprocess.run(_command(ROOT), env=env, text=True, capture_output=True)

    assert result.returncode != 0
    assert "shadow" in (result.stdout + result.stderr).lower()
    assert not (hermes_home / "skill-bundles").exists()


def test_configure_script_preserves_hermes_home_relative_external_dir(
    tmp_path: Path,
) -> None:
    env, hermes_home = _environment(tmp_path)
    relative = Path("team-skills")
    (hermes_home / relative).mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"skills": {"external_dirs": [relative.as_posix()]}}),
        encoding="utf-8",
    )

    subprocess.run(_command(ROOT), env=env, text=True, capture_output=True, check=True)

    config = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert config["skills"]["external_dirs"] == [
        relative.as_posix(),
        str((ROOT / "agenten" / "agent_factory" / "skills").resolve()),
    ]


def test_configure_script_rejects_disabled_skill_before_bundle(tmp_path: Path) -> None:
    env, hermes_home = _environment(tmp_path)
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"skills": {"disabled": [SKILL_NAMES[0]]}}), encoding="utf-8"
    )

    result = subprocess.run(_command(ROOT), env=env, text=True, capture_output=True)

    assert result.returncode != 0
    assert "missing or disabled" in (result.stdout + result.stderr).lower()
    assert not (hermes_home / "skill-bundles").exists()


def test_remove_deletes_only_repository_path_and_factory_bundle(tmp_path: Path) -> None:
    env, hermes_home = _environment(tmp_path)
    unrelated = (tmp_path / "unrelated-skills").resolve()
    unrelated.mkdir()
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"skills": {"external_dirs": [str(unrelated)]}}),
        encoding="utf-8",
    )
    subprocess.run(_command(ROOT), env=env, text=True, capture_output=True, check=True)

    removed = subprocess.run(
        _command(ROOT, remove=True),
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    config = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert config["skills"]["external_dirs"] == [str(unrelated)]
    assert not (
        hermes_home / "skill-bundles" / "captain-agent-factory-loop.yaml"
    ).exists()
    assert "removed" in removed.stdout.lower()

    removed_again = subprocess.run(
        _command(ROOT, remove=True),
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    config = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert config["skills"]["external_dirs"] == [str(unrelated)]
    assert "already removed" in removed_again.stdout.lower()


def test_remove_succeeds_when_key_path_and_bundle_are_absent(tmp_path: Path) -> None:
    env, hermes_home = _environment(tmp_path)

    result = subprocess.run(
        _command(ROOT, remove=True),
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "already removed" in result.stdout.lower()
    calls = (hermes_home / "calls.log").read_text(encoding="utf-8")
    assert '["bundles", "delete", "captain-agent-factory-loop"]' in calls
    assert not (hermes_home / "config.yaml").exists()


def test_runbook_documents_setup_verification_and_scoped_rollback() -> None:
    text = (ROOT / "docs" / "AGENT_FACTORY_RUNBOOK.md").read_text(encoding="utf-8")

    for phrase in (
        "configure-hermes-factory-skills.ps1",
        "hermes skills list --enabled-only",
        "hermes bundles show captain-agent-factory-loop",
        "-Remove",
        "preserves unrelated external directories",
    ):
        assert phrase.lower() in text.lower()
    assert "hermes skills reset" not in text.lower()
