"""Handle-verified, write-once files below one lexical runtime root."""

from __future__ import annotations

import os
import stat
from contextlib import AbstractContextManager
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from uuid import uuid4


_REPARSE_POINT = 0x400


class ConfinedFileError(ValueError):
    """A runtime file operation could not prove confinement or immutability."""


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _is_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _windows_final_path(stream: BinaryIO) -> Path:
    import msvcrt

    return _windows_final_path_from_handle(msvcrt.get_osfhandle(stream.fileno()))


def _windows_final_path_from_handle(handle: int) -> Path:
    import ctypes
    from ctypes import wintypes

    get_final_path = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_final_path(handle, buffer, len(buffer), 0)
    if length == 0 or length >= len(buffer):
        raise ConfinedFileError("runtime file final path could not be verified")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


class _VerifiedDirectory(AbstractContextManager["_VerifiedDirectory"]):
    """Hold one verified parent identity stable for create and publish."""

    def __init__(self, path: Path, *, root: Path) -> None:
        self._path = path
        self._root = root
        self._descriptor: int | None = None
        self._windows_handle: int | None = None

    def __enter__(self) -> "_VerifiedDirectory":
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
            create_file.argtypes = (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            )
            create_file.restype = wintypes.HANDLE
            handle = create_file(
                str(self._path),
                0x80000000,
                0x00000001 | 0x00000002,
                None,
                3,
                0x02000000,
                None,
            )
            if handle == wintypes.HANDLE(-1).value:
                raise ConfinedFileError("runtime file parent handle is unavailable")
            self._windows_handle = int(handle)
            final = _absolute_lexical(_windows_final_path_from_handle(int(handle)))
        else:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                self._descriptor = os.open(self._path, flags)
                final = _absolute_lexical(
                    Path(os.readlink(Path("/proc/self/fd") / str(self._descriptor)))
                )
            except OSError:
                self.__exit__(None, None, None)
                raise ConfinedFileError(
                    "runtime file parent handle is unavailable"
                ) from None
        try:
            final.relative_to(self._root)
        except ValueError:
            self.__exit__(None, None, None)
            raise ConfinedFileError("runtime file parent handle escaped its root") from None
        if final != self._path:
            self.__exit__(None, None, None)
            raise ConfinedFileError("runtime file parent identity changed")
        return self

    def __exit__(self, *_args: object) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        if self._windows_handle is not None:
            import ctypes

            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
                self._windows_handle
            )
            self._windows_handle = None

    def open(self, name: str, flags: int, mode: int) -> int:
        if self._descriptor is not None:
            return os.open(name, flags, mode, dir_fd=self._descriptor)
        return os.open(self._path / name, flags, mode)

    def link(self, source: str, target: str) -> None:
        if self._descriptor is not None:
            os.link(
                source,
                target,
                src_dir_fd=self._descriptor,
                dst_dir_fd=self._descriptor,
                follow_symlinks=False,
            )
            return
        os.link(self._path / source, self._path / target, follow_symlinks=False)

    def unlink(self, name: str) -> None:
        if self._descriptor is not None:
            os.unlink(name, dir_fd=self._descriptor)
        else:
            (self._path / name).unlink(missing_ok=True)

    def fsync(self) -> None:
        if self._descriptor is not None:
            os.fsync(self._descriptor)

    def same_file(self, first: str, second: str) -> bool:
        if self._descriptor is not None:
            return os.stat(first, dir_fd=self._descriptor) == os.stat(
                second,
                dir_fd=self._descriptor,
            )
        return os.path.samefile(self._path / first, self._path / second)


def _final_path_for_open_file(stream: BinaryIO, *, requested_path: Path) -> Path:
    if os.name == "nt":
        return _windows_final_path(stream)
    try:
        value = os.readlink(Path("/proc/self/fd") / str(stream.fileno()))
    except OSError:
        raise ConfinedFileError("runtime file final path could not be verified") from None
    if value.endswith(" (deleted)"):
        raise ConfinedFileError("runtime file final path could not be verified")
    del requested_path
    return Path(value)


def _relative_parts(relative: str | Path) -> tuple[str, ...]:
    value = PurePosixPath(str(relative).replace("\\", "/"))
    if value.is_absolute() or not value.parts or any(
        part in {"", ".", ".."} for part in value.parts
    ):
        raise ConfinedFileError("runtime file path is not a safe relative path")
    return value.parts


class ConfinedFileStore:
    """Read and create exact bytes without following reparse-point prefixes."""

    def __init__(self, root: Path) -> None:
        self._root = _absolute_lexical(root)
        self._ensure_root()

    @property
    def root(self) -> Path:
        return self._root

    def _ensure_root(self) -> None:
        nearest = self._root
        while not nearest.exists() and nearest != nearest.parent:
            nearest = nearest.parent
        if _is_reparse(nearest) or nearest.resolve(strict=True) != nearest:
            raise ConfinedFileError("runtime file root has a symlink or reparse prefix")
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if _is_reparse(self._root) or self._root.resolve(strict=True) != self._root:
            raise ConfinedFileError("runtime file root is not confined")

    def _parent(self, parts: tuple[str, ...]) -> Path:
        self._ensure_root()
        parent = self._root
        for component in parts[:-1]:
            parent = parent / component
            if parent.exists():
                if _is_reparse(parent) or not parent.is_dir():
                    raise ConfinedFileError("runtime file parent has a symlink or reparse point")
            else:
                try:
                    parent.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                if _is_reparse(parent) or not parent.is_dir():
                    raise ConfinedFileError(
                        "runtime file parent has a symlink or reparse point"
                    )
            if parent.resolve(strict=True) != parent:
                raise ConfinedFileError("runtime file parent escaped its confined root")
        return parent

    def read(self, relative: str | Path) -> bytes:
        parts = _relative_parts(relative)
        target = self._parent(parts) / parts[-1]
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        if os.name != "nt":
            flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
        except OSError:
            raise ConfinedFileError("runtime file is unavailable") from None
        try:
            stream = os.fdopen(descriptor, "rb", closefd=True)
        except Exception:
            os.close(descriptor)
            raise
        with stream:
            self._verify_open_file(stream, target)
            try:
                return stream.read()
            except OSError:
                raise ConfinedFileError("runtime file could not be read") from None

    def write_once(
        self,
        relative: str | Path,
        content: bytes,
        *,
        conflict: str,
    ) -> bool:
        if not isinstance(content, bytes):
            raise TypeError("runtime file content must be bytes")
        parts = _relative_parts(relative)
        parent = self._parent(parts)
        target_name = parts[-1]
        temporary_name = f".{target_name}.{uuid4().hex}.tmp"
        temporary = parent / temporary_name
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        if os.name != "nt":
            flags |= getattr(os, "O_NOFOLLOW", 0)
        with _VerifiedDirectory(parent, root=self._root) as directory:
            try:
                descriptor = directory.open(temporary_name, flags, 0o600)
            except OSError:
                raise ConfinedFileError("runtime file could not be created") from None
            try:
                try:
                    stream = os.fdopen(descriptor, "wb", closefd=True)
                except Exception:
                    os.close(descriptor)
                    raise
                with stream:
                    self._verify_open_file(stream, temporary)
                    try:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    except OSError:
                        raise ConfinedFileError(
                            "runtime file could not be persisted"
                        ) from None
                try:
                    directory.link(temporary_name, target_name)
                except FileExistsError:
                    if self.read(relative) == content:
                        return False
                    raise ConfinedFileError(conflict) from None
                except OSError:
                    raise ConfinedFileError(
                        "runtime file could not be atomically published"
                    ) from None
                try:
                    if self.read(relative) != content:
                        raise ConfinedFileError(conflict)
                    directory.fsync()
                except Exception:
                    try:
                        if directory.same_file(temporary_name, target_name):
                            directory.unlink(target_name)
                    except OSError:
                        pass
                    raise
                return True
            finally:
                try:
                    directory.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def regular_files(self, relative_directory: str | Path) -> tuple[Path, ...]:
        parts = _relative_parts(relative_directory)
        directory = self.require_directory(Path(*parts))
        found: list[Path] = []
        pending = [directory]
        while pending:
            current = pending.pop()
            for child in current.iterdir():
                if _is_reparse(child):
                    raise ConfinedFileError("runtime directory contains a reparse point")
                if child.is_dir():
                    pending.append(child)
                elif child.is_file():
                    found.append(child.relative_to(self._root))
                else:
                    raise ConfinedFileError("runtime directory contains a special file")
        return tuple(sorted(found, key=lambda item: item.as_posix()))

    def require_directory(self, relative_directory: str | Path) -> Path:
        parts = _relative_parts(relative_directory)
        self._ensure_root()
        directory = self._root
        for component in parts:
            directory = directory / component
            if _is_reparse(directory) or not directory.is_dir():
                raise ConfinedFileError("runtime directory is unavailable or reparsed")
            if directory.resolve(strict=True) != directory:
                raise ConfinedFileError("runtime directory escaped its confined root")
        return directory

    def _verify_open_file(self, stream: BinaryIO, requested_path: Path) -> None:
        try:
            final_path = _absolute_lexical(
                _final_path_for_open_file(stream, requested_path=requested_path)
            )
            final_path.relative_to(self._root)
            metadata = os.fstat(stream.fileno())
        except (OSError, ValueError):
            raise ConfinedFileError("runtime file final handle escaped its confined root") from None
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfinedFileError("runtime file handle is not a regular file")

__all__ = ["ConfinedFileError", "ConfinedFileStore"]
