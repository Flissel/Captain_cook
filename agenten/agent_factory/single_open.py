"""TOCTOU-safe single-open file reading for authority adapter loading.

This module extracts the descriptor-verified read procedure proven in
``agenten/agent_factory/codex_build_execution.py`` so new loaders reuse it
instead of copying it: the target is stat'ed without following symlinks,
opened exactly once with ``O_NOFOLLOW`` where available, its identity is
compared before and after the open, every byte is read from that single
descriptor under a hard size cap, and the identity and version are compared
again after the read. Any mismatch fails closed.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

_READ_CHUNK_BYTES = 64 * 1024


class SingleOpenError(OSError):
    """Raised when a single-open read cannot be completed fail-closed."""


def is_plain_regular_file(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and not (reparse_flag and file_attributes & reparse_flag)
    )


def file_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def file_version(metadata: os.stat_result) -> tuple[int, int, int]:
    return (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def read_verified_bytes(target: Path, *, maximum_size: int) -> bytes:
    """Read ``target`` exactly once through a verified descriptor."""
    descriptor: int | None = None
    try:
        before_path = os.stat(target, follow_symlinks=False)
        if not is_plain_regular_file(before_path):
            raise SingleOpenError(f"refusing non-regular target: {target.name}")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags)
        opened = os.fstat(descriptor)
        after_open_path = os.stat(target, follow_symlinks=False)
        if (
            not is_plain_regular_file(opened)
            or file_identity(before_path) != file_identity(opened)
            or file_identity(after_open_path) != file_identity(opened)
        ):
            raise SingleOpenError(f"target identity changed during open: {target.name}")
        chunks: list[bytes] = []
        size = 0
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = None
            while chunk := source.read(_READ_CHUNK_BYTES):
                size += len(chunk)
                if size > maximum_size:
                    raise SingleOpenError(
                        f"target exceeds configured size limit: {target.name}"
                    )
                chunks.append(chunk)
            after_read = os.fstat(source.fileno())
        if size == 0:
            raise SingleOpenError(f"target is empty: {target.name}")
        after_path = os.stat(target, follow_symlinks=False)
        if (
            file_identity(after_read) != file_identity(opened)
            or file_identity(after_path) != file_identity(opened)
            or file_version(after_read) != file_version(opened)
        ):
            raise SingleOpenError(f"target changed during read: {target.name}")
        return b"".join(chunks)
    except OSError as error:
        if isinstance(error, SingleOpenError):
            raise
        raise SingleOpenError(f"single-open read failed: {target.name}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def sha256_of_verified_read(target: Path, *, maximum_size: int) -> tuple[bytes, str]:
    body = read_verified_bytes(target, maximum_size=maximum_size)
    return body, hashlib.sha256(body).hexdigest()
