"""Managed, preflight-first process lifecycle for the capability demo."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from .configuration import DemoConfiguration
from .contracts import DemoServiceV1


class DemoProcessError(RuntimeError):
    """A process cannot be safely started, reused, restarted, or stopped."""


@dataclass(frozen=True, slots=True)
class SpawnedProcess:
    pid: int
    start_identity: str


@dataclass(frozen=True, slots=True)
class ManagedProcessRecord:
    service: str
    pid: int
    start_identity: str
    port: int
    command_sha256: str
    config_sha256: str
    bundle_sha256: str


class ProcessBackend(Protocol):
    def validate_compose(self, files: tuple[Path, ...], project: str) -> None: ...

    def start_compose(self, files: tuple[Path, ...], project: str) -> None: ...

    def listener_pid(self, port: int) -> int | None: ...

    def is_alive(self, pid: int, start_identity: str) -> bool: ...

    def spawn(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        environment: dict[str, str],
        stdout_path: Path,
    ) -> SpawnedProcess: ...

    def wait_authenticated_health(
        self, url: str, token: str, timeout_seconds: float
    ) -> bool: ...

    def terminate(self, pid: int, start_identity: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class _PreparedService:
    service: DemoServiceV1
    argv: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]
    record: ManagedProcessRecord
    reusable: bool


class ManagedProcessSupervisor:
    def __init__(
        self,
        configuration: DemoConfiguration,
        *,
        backend: ProcessBackend | None = None,
    ) -> None:
        self._configuration = configuration
        self._backend = backend or WindowsProcessBackend()
        self._state_path = configuration.state_root / "managed-processes.json"

    def start_all(self, *, timeout_seconds: float) -> tuple[ManagedProcessRecord, ...]:
        if timeout_seconds <= 0:
            raise DemoProcessError("service health timeout must be positive")
        prepared = self._preflight(timeout_seconds=timeout_seconds)
        if all(item.reusable for item in prepared):
            return tuple(item.record for item in prepared)
        compose_files = tuple(
            self._configuration.repository_root.joinpath(*PurePosixPath(relative).parts)
            for relative in self._configuration.bundle.compose_files
        )
        self._backend.start_compose(
            compose_files, self._configuration.bundle.compose_project
        )
        records = {record.service: record for record in self._load_records()}
        started: list[ManagedProcessRecord] = []
        try:
            for item in prepared:
                if item.reusable:
                    records[item.service.name] = item.record
                    continue
                spawned = self._backend.spawn(
                    item.argv,
                    item.cwd,
                    item.environment,
                    self._log_path(item.service.name),
                )
                record = ManagedProcessRecord(
                    **{
                        **asdict(item.record),
                        "pid": spawned.pid,
                        "start_identity": spawned.start_identity,
                    }
                )
                token = item.environment[item.service.health_token_env]
                if not self._backend.wait_authenticated_health(
                    item.service.health_url, token, timeout_seconds
                ):
                    raise DemoProcessError(
                        f"authenticated health failed for {item.service.name}"
                    )
                owner = self._backend.listener_pid(item.service.port)
                if owner != spawned.pid:
                    raise DemoProcessError(
                        f"listener ownership mismatch for {item.service.name}"
                    )
                records[item.service.name] = record
                started.append(record)
                self._save_records(tuple(records[name] for name in self._service_names() if name in records))
        except Exception:
            for record in reversed(started):
                self._backend.terminate(record.pid, record.start_identity)
                records.pop(record.service, None)
            self._save_records(tuple(records[name] for name in self._service_names() if name in records))
            raise
        result = tuple(records[name] for name in self._service_names())
        self._save_records(result)
        return result

    def restart_runtime(self, *, timeout_seconds: float) -> ManagedProcessRecord:
        records = {record.service: record for record in self._load_records()}
        existing = records.get("runtime")
        if existing is None:
            raise DemoProcessError("managed Runtime identity is unavailable")
        prepared = next(
            item for item in self._prepare_services(records) if item.service.name == "runtime"
        )
        if existing != prepared.record:
            raise DemoProcessError("managed Runtime configuration changed")
        if not self._backend.is_alive(existing.pid, existing.start_identity):
            raise DemoProcessError("managed Runtime process identity is stale")
        if self._backend.listener_pid(existing.port) != existing.pid:
            raise DemoProcessError("managed Runtime listener identity changed")
        if not self._backend.terminate(existing.pid, existing.start_identity):
            raise DemoProcessError("managed Runtime could not be stopped")
        if self._backend.listener_pid(existing.port) is not None:
            raise DemoProcessError("Runtime port remained occupied after managed stop")
        spawned = self._backend.spawn(
            prepared.argv,
            prepared.cwd,
            prepared.environment,
            self._log_path("runtime"),
        )
        replacement = ManagedProcessRecord(
            **{
                **asdict(prepared.record),
                "pid": spawned.pid,
                "start_identity": spawned.start_identity,
            }
        )
        token = prepared.environment[prepared.service.health_token_env]
        if (
            not self._backend.wait_authenticated_health(
                prepared.service.health_url, token, timeout_seconds
            )
            or self._backend.listener_pid(prepared.service.port) != spawned.pid
        ):
            self._backend.terminate(spawned.pid, spawned.start_identity)
            records.pop("runtime", None)
            self._save_records(tuple(records[name] for name in self._service_names() if name in records))
            raise DemoProcessError("restarted Runtime failed identity or health verification")
        records["runtime"] = replacement
        self._save_records(tuple(records[name] for name in self._service_names() if name in records))
        return replacement

    def status(self, *, timeout_seconds: float = 2) -> tuple[ManagedProcessRecord, ...]:
        records = self._load_records()
        services = {service.name: service for service in self._configuration.bundle.services}
        expected = {
            item.service.name: item.record
            for item in self._prepare_services(
                {record.service: record for record in records}
            )
        }
        healthy: list[ManagedProcessRecord] = []
        for record in records:
            service = services.get(record.service)
            if service is None or expected.get(record.service) != record:
                continue
            environment = self._configuration.environment_for(service.name)
            if (
                self._backend.is_alive(record.pid, record.start_identity)
                and self._backend.listener_pid(record.port) == record.pid
                and self._backend.wait_authenticated_health(
                    service.health_url,
                    environment[service.health_token_env],
                    timeout_seconds,
                )
            ):
                healthy.append(record)
        return tuple(healthy)

    def stop(self) -> tuple[ManagedProcessRecord, ...]:
        records = self._load_records()
        stopped: list[ManagedProcessRecord] = []
        remaining: list[ManagedProcessRecord] = []
        for record in reversed(records):
            if self._backend.terminate(record.pid, record.start_identity):
                stopped.append(record)
            else:
                remaining.append(record)
        self._save_records(tuple(reversed(remaining)))
        return tuple(stopped)

    def _preflight(self, *, timeout_seconds: float) -> tuple[_PreparedService, ...]:
        compose_files = tuple(
            self._configuration.repository_root.joinpath(*PurePosixPath(relative).parts)
            for relative in self._configuration.bundle.compose_files
        )
        self._backend.validate_compose(
            compose_files, self._configuration.bundle.compose_project
        )
        existing = {record.service: record for record in self._load_records()}
        prepared = self._prepare_services(existing)
        result: list[_PreparedService] = []
        for item in prepared:
            owner = self._backend.listener_pid(item.service.port)
            prior = existing.get(item.service.name)
            exact = prior == item.record
            reusable = bool(
                exact
                and prior is not None
                and self._backend.is_alive(prior.pid, prior.start_identity)
                and owner == prior.pid
                and self._backend.wait_authenticated_health(
                    item.service.health_url,
                    item.environment[item.service.health_token_env],
                    timeout_seconds,
                )
            )
            if not reusable and owner is not None:
                raise DemoProcessError(
                    f"foreign listener occupies required port {item.service.port}"
                )
            if prior is not None and not reusable and self._backend.is_alive(
                prior.pid, prior.start_identity
            ):
                raise DemoProcessError(
                    f"managed {item.service.name} configuration changed while process is alive"
                )
            result.append(
                _PreparedService(
                    service=item.service,
                    argv=item.argv,
                    cwd=item.cwd,
                    environment=item.environment,
                    record=item.record,
                    reusable=reusable,
                )
            )
        return tuple(result)

    def _prepare_services(
        self, existing: Mapping[str, ManagedProcessRecord]
    ) -> tuple[_PreparedService, ...]:
        prepared: list[_PreparedService] = []
        for service in self._configuration.bundle.services:
            argv = tuple(self._expand(argument) for argument in service.argv)
            executable = argv[0]
            if (
                ("\\" in executable or "/" in executable)
                and not Path(executable).is_file()
            ) or (
                "\\" not in executable
                and "/" not in executable
                and shutil.which(executable) is None
            ):
                raise DemoProcessError(
                    f"service executable is unavailable for {service.name}"
                )
            cwd = self._resolve_working_directory(service.working_directory)
            environment = self._child_environment(
                self._configuration.environment_for(service.name)
            )
            command_sha256 = _canonical_sha256(
                {"argv": argv, "cwd": str(cwd), "artifacts": service.artifact_paths}
            )
            secret_digests = {
                name: hashlib.sha256(environment[name].encode("utf-8")).hexdigest()
                for name in service.environment
            }
            config_sha256 = _canonical_sha256(
                {
                    "bundle_sha256": self._configuration.bundle_sha256,
                    "service": service.model_dump(mode="json"),
                    "command_sha256": command_sha256,
                    "secret_digests": secret_digests,
                }
            )
            prior = existing.get(service.name)
            record = ManagedProcessRecord(
                service=service.name,
                pid=prior.pid if prior is not None else 0,
                start_identity=prior.start_identity if prior is not None else "pending",
                port=service.port,
                command_sha256=command_sha256,
                config_sha256=config_sha256,
                bundle_sha256=self._configuration.bundle_sha256,
            )
            prepared.append(
                _PreparedService(
                    service=service,
                    argv=argv,
                    cwd=cwd,
                    environment=environment,
                    record=record,
                    reusable=False,
                )
            )
        return tuple(prepared)

    def _expand(self, value: str) -> str:
        return (
            value.replace("{python}", sys.executable)
            .replace("{repository}", str(self._configuration.repository_root))
            .replace("{state_root}", str(self._configuration.state_root))
        )

    def _resolve_working_directory(self, relative: str) -> Path:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or any(part in {"", ".."} for part in pure.parts):
            raise DemoProcessError("service working directory escaped repository")
        path = self._configuration.repository_root.joinpath(*pure.parts).resolve(strict=True)
        try:
            path.relative_to(self._configuration.repository_root)
        except ValueError:
            raise DemoProcessError("service working directory escaped repository") from None
        if not path.is_dir():
            raise DemoProcessError("service working directory is unavailable")
        return path

    @staticmethod
    def _child_environment(authority: Mapping[str, str]) -> dict[str, str]:
        inherited_names = (
            "PATH",
            "SYSTEMROOT",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "LOCALAPPDATA",
            "APPDATA",
            "PROGRAMDATA",
        )
        environment = {name: os.environ[name] for name in inherited_names if name in os.environ}
        environment.update(authority)
        environment["PYTHONUTF8"] = "1"
        return environment

    def _load_records(self) -> tuple[ManagedProcessRecord, ...]:
        return _read_managed_records(self._state_path)

    def _save_records(self, records: Sequence[ManagedProcessRecord]) -> None:
        _write_managed_records(self._state_path, records)

    def _log_path(self, service: str) -> Path:
        path = self._configuration.state_root / "logs" / f"{service}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _service_names(self) -> tuple[str, ...]:
        return tuple(service.name for service in self._configuration.bundle.services)


class WindowsProcessBackend:
    """Concrete Windows backend; shell calls remain argument-array based."""

    def validate_compose(self, files: tuple[Path, ...], project: str) -> None:
        self._compose(files, project, ("config", "--quiet"))

    def start_compose(self, files: tuple[Path, ...], project: str) -> None:
        self._compose(files, project, ("up", "-d"))

    @staticmethod
    def _compose(files: tuple[Path, ...], project: str, action: tuple[str, ...]) -> None:
        argv = ["docker", "compose", "-p", project]
        for path in files:
            argv.extend(("-f", str(path)))
        argv.extend(action)
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise DemoProcessError(f"Docker Compose {' '.join(action)} failed")

    def listener_pid(self, port: int) -> int | None:
        script = (
            "$items=@(Get-NetTCPConnection -LocalAddress '127.0.0.1' "
            f"-LocalPort {port} -State Listen -ErrorAction SilentlyContinue);"
            "if($items.Count -gt 1){exit 9};if($items.Count -eq 1){$items[0].OwningProcess}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 9:
            raise DemoProcessError(f"multiple listeners occupy required port {port}")
        if result.returncode != 0:
            raise DemoProcessError(f"could not inspect listener port {port}")
        value = result.stdout.strip()
        return int(value) if value else None

    def is_alive(self, pid: int, start_identity: str) -> bool:
        return self._start_identity(pid) == start_identity

    def spawn(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        environment: dict[str, str],
        stdout_path: Path,
    ) -> SpawnedProcess:
        with stdout_path.open("ab") as output:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        identity = self._start_identity(process.pid)
        if identity is None:
            raise DemoProcessError("started process identity is unavailable")
        return SpawnedProcess(pid=process.pid, start_identity=identity)

    def wait_authenticated_health(self, url: str, token: str, timeout_seconds: float) -> bool:
        import time
        import urllib.error
        import urllib.request

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            request = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {token}"}
            )
            try:
                with urllib.request.urlopen(request, timeout=min(2, timeout_seconds)) as response:
                    if response.status == 200:
                        return True
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.1)
        return False

    def terminate(self, pid: int, start_identity: str) -> bool:
        if not self.is_alive(pid, start_identity):
            return False
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    @staticmethod
    def _start_identity(pid: int) -> str | None:
        script = (
            f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue;"
            "if($null -ne $p){$p.StartTime.ToUniversalTime().Ticks}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
        value = result.stdout.strip()
        return value if result.returncode == 0 and value else None


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stop_managed_processes(
    repository_root: Path,
    *,
    backend: ProcessBackend | None = None,
) -> tuple[ManagedProcessRecord, ...]:
    """Stop only persisted PID/start identities without requiring D10/D11."""

    repository = repository_root.resolve(strict=True)
    state_path = repository / ".captain-cook/live-demo/managed-processes.json"
    records = _read_managed_records(state_path)
    selected_backend = backend or WindowsProcessBackend()
    stopped: list[ManagedProcessRecord] = []
    remaining: list[ManagedProcessRecord] = []
    for record in reversed(records):
        if selected_backend.terminate(record.pid, record.start_identity):
            stopped.append(record)
        else:
            remaining.append(record)
    _write_managed_records(state_path, tuple(reversed(remaining)))
    return tuple(stopped)


def _read_managed_records(path: Path) -> tuple[ManagedProcessRecord, ...]:
    if not path.exists():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != "captain.demo-managed-processes.v1":
            raise ValueError
        records = tuple(ManagedProcessRecord(**item) for item in payload["processes"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise DemoProcessError("managed process state is invalid") from None
    if len({record.service for record in records}) != len(records):
        raise DemoProcessError("managed process state contains duplicate services")
    return records


def _write_managed_records(
    path: Path, records: Sequence[ManagedProcessRecord]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "captain.demo-managed-processes.v1",
        "processes": [asdict(record) for record in records],
    }
    _atomic_write_json(path, payload)


def _atomic_write_json(path: Path, payload: object) -> None:
    descriptor, raw_temp = tempfile.mkstemp(prefix="managed-", suffix=".tmp", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


__all__ = [
    "DemoProcessError",
    "ManagedProcessRecord",
    "ManagedProcessSupervisor",
    "SpawnedProcess",
    "WindowsProcessBackend",
    "stop_managed_processes",
]
