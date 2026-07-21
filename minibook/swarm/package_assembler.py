"""Deterministic, fail-closed assembly of private capability candidates."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class PackageAssemblyError(RuntimeError):
    pass


@dataclass(frozen=True)
class AssembledPackage:
    archive_path: Path
    archive_sha256: str
    manifest_sha256: str


_EXCLUDED_NAMES = {".env", ".git", ".hg", "__pycache__", ".pytest_cache"}
_EXCLUDED_SUFFIXES = {".log", ".pyc", ".pyo"}
_INTEGRATION_FIELDS = {
    "workflow", "input_schema", "output_schema", "idempotency", "timeout",
    "retry", "duplicate", "failure", "compensation",
}


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise PackageAssemblyError("package path must be safe and relative")
    return path


class PackageAssembler:
    def assemble(
        self,
        source: Path,
        archive_path: Path,
        *,
        startup_command: tuple[str, ...],
        integration_contracts: tuple[dict[str, object], ...] = (),
    ) -> AssembledPackage:
        source = source.resolve()
        if not source.is_dir():
            raise PackageAssemblyError("candidate source is not a directory")
        self._validate_startup(source, startup_command)
        for contract in integration_contracts:
            if set(contract) != _INTEGRATION_FIELDS:
                raise PackageAssemblyError("integration contract is incomplete")
            _safe_relative(str(contract["workflow"]))
        files: dict[str, bytes] = {}
        directories: set[str] = set()
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source).as_posix()
            if any(part in _EXCLUDED_NAMES for part in path.relative_to(source).parts):
                continue
            if path.is_symlink():
                raise PackageAssemblyError("candidate packages must not contain symlinks")
            if path.is_dir():
                directories.add(relative.rstrip("/") + "/")
                continue
            if path.suffix.lower() in _EXCLUDED_SUFFIXES or "transcript" in path.name.lower():
                continue
            _safe_relative(relative)
            files[relative] = path.read_bytes()
        required = {"autogen/", "skills/", "tests/", "evidence/", "RUNBOOK.md"}
        if integration_contracts:
            required.add("n8n/")
        present = directories | set(files)
        if not required.issubset(present):
            raise PackageAssemblyError("candidate package is missing required layout")
        entries = [
            {"path": name, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for name, content in sorted(files.items())
        ]
        manifest = {
            "schema": "minibook.team-manifest.v1",
            "startup_command": list(startup_command),
            "required_layout": sorted(required),
            "files": entries,
            "integrations": list(integration_contracts),
        }
        manifest_bytes = json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        archive_entries = dict(files)
        archive_entries["team-manifest.json"] = manifest_bytes
        for directory in directories:
            archive_entries.setdefault(directory, b"")
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, content in sorted(archive_entries.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o755 if name.endswith("/") else 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
        archive_bytes = archive_path.read_bytes()
        return AssembledPackage(
            archive_path=archive_path,
            archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )

    def _validate_startup(self, source: Path, command: tuple[str, ...]) -> None:
        if len(command) < 2 or command[0] != "python":
            raise PackageAssemblyError("startup executable is not allow-listed")
        entry = _safe_relative(command[1])
        entry_path = source / Path(*entry.parts)
        if not entry_path.is_file() or entry_path.suffix != ".py":
            raise PackageAssemblyError("startup entrypoint is missing or unsupported")
        for path in source.rglob("*.py"):
            if path.is_symlink():
                raise PackageAssemblyError("candidate packages must not contain symlinks")
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except (OSError, SyntaxError, UnicodeError) as exc:
                raise PackageAssemblyError("candidate Python import validation failed") from exc
        with tempfile.TemporaryDirectory(prefix="minibook-candidate-") as temporary:
            workspace = Path(temporary) / "candidate"
            shutil.copytree(source, workspace)
            environment = {
                key: value for key, value in os.environ.items()
                if key in {"PATH", "SystemRoot", "WINDIR", "TEMP", "TMP"}
            }
            try:
                completed = subprocess.run(
                    [sys.executable, *command[1:]], cwd=workspace, env=environment,
                    capture_output=True, timeout=10, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise PackageAssemblyError("candidate startup validation failed") from exc
            if completed.returncode != 0:
                raise PackageAssemblyError("candidate startup validation failed")
