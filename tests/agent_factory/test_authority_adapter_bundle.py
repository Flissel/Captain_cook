"""Acceptance tests for the digest-pinned authority adapter bundle."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agenten.agent_factory.authority_adapter_bundle import (
    AuthorityAdapterBundleError,
    BUNDLE_SOURCE_PATHS,
    load_adapter_bundle,
    pin_adapter_bundle,
    write_adapter_bundle,
)
from agenten.agent_factory.single_open import SingleOpenError, read_verified_bytes

COMMITTED_BUNDLE = Path("config/authority-adapter-bundle.v1.json")


def test_committed_bundle_pins_current_repository_adapters() -> None:
    bundle, bundle_sha256 = load_adapter_bundle(COMMITTED_BUNDLE)
    assert len(bundle.adapters) == 5
    assert tuple(entry.ref.role for entry in bundle.adapters) == (
        "captain",
        "gateway",
        "runtime",
        "minibook",
        "n8n",
    )
    assert len(bundle_sha256) == 64
    repinned = pin_adapter_bundle(BUNDLE_SOURCE_PATHS)
    assert repinned == bundle


def test_pinned_bundle_round_trips_byte_stably(tmp_path: Path) -> None:
    bundle = pin_adapter_bundle(BUNDLE_SOURCE_PATHS)
    first = tmp_path / "bundle-a.json"
    second = tmp_path / "bundle-b.json"
    write_adapter_bundle(bundle, first)
    write_adapter_bundle(bundle, second)
    assert first.read_bytes() == second.read_bytes()
    loaded, _ = load_adapter_bundle(first)
    assert loaded == bundle


def test_digest_mismatch_names_role_but_never_content(tmp_path: Path) -> None:
    bundle = pin_adapter_bundle(BUNDLE_SOURCE_PATHS)
    payload = json.loads(
        Path(COMMITTED_BUNDLE).read_text(encoding="utf-8")
    )
    payload["adapters"][1]["ref"]["sha256"] = "0" * 64
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AuthorityAdapterBundleError) as excinfo:
        load_adapter_bundle(tampered)
    message = str(excinfo.value)
    assert "gateway" in message
    forbidden_fragment = read_verified_bytes(
        BUNDLE_SOURCE_PATHS["gateway"], maximum_size=16 * 1024 * 1024
    )[:64]
    assert forbidden_fragment.decode("utf-8", errors="ignore") not in message
    assert bundle.adapters[1].ref.sha256 not in message


def test_bundle_source_paths_are_pinned_to_lf_line_endings() -> None:
    """Guard against .gitattributes drifting away from BUNDLE_SOURCE_PATHS.

    Every file in BUNDLE_SOURCE_PATHS is hashed byte-for-byte, so each one
    must resolve `eol=lf`, or a Windows checkout normalizes it to CRLF and
    the pinned digest can never verify (see the committed-bundle test
    above, and .gitattributes for the full explanation). This test derives
    its expectations straight from BUNDLE_SOURCE_PATHS rather than a second
    hardcoded list, so an added/renamed role without a matching
    .gitattributes entry fails here instead of only on Windows.
    """
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available on this runner")

    posix_paths = [path.as_posix() for path in BUNDLE_SOURCE_PATHS.values()]
    try:
        result = subprocess.run(
            [git, "check-attr", "eol", "--", *posix_paths],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        pytest.skip(f"git check-attr is unavailable on this runner: {error}")

    resolved: dict[str, str] = {}
    for line in result.stdout.splitlines():
        path_part, separator, value = line.rpartition(": eol: ")
        if not separator:
            continue
        resolved[path_part] = value.strip()

    for role, path in BUNDLE_SOURCE_PATHS.items():
        posix_path = path.as_posix()
        assert resolved.get(posix_path) == "lf", (
            f"BUNDLE_SOURCE_PATHS[{role!r}] ({posix_path}) is hashed "
            "byte-for-byte by the authority adapter bundle but "
            ".gitattributes does not pin it to eol=lf; a Windows checkout "
            "will normalize it to CRLF and the pinned digest will never "
            "verify there."
        )


def test_bundle_missing_role_fails_closed(tmp_path: Path) -> None:
    payload = json.loads(COMMITTED_BUNDLE.read_text(encoding="utf-8"))
    payload["adapters"] = payload["adapters"][:-1]
    truncated = tmp_path / "truncated.json"
    truncated.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AuthorityAdapterBundleError, match="n8n"):
        load_adapter_bundle(truncated)


def test_single_open_rejects_symlinked_bundle(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text(COMMITTED_BUNDLE.read_text(encoding="utf-8"), encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this runner")
    with pytest.raises((AuthorityAdapterBundleError, SingleOpenError)):
        load_adapter_bundle(link)


def test_single_open_rejects_hardlinked_target(tmp_path: Path) -> None:
    original = tmp_path / "original.bin"
    original.write_bytes(b"adapter-bytes")
    aliased = tmp_path / "aliased.bin"
    try:
        os.link(original, aliased)
    except (OSError, NotImplementedError):
        pytest.skip("hard links are unavailable on this runner")
    with pytest.raises(SingleOpenError):
        read_verified_bytes(original, maximum_size=1024)


def test_single_open_enforces_size_cap_and_rejects_empty(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.bin"
    oversized.write_bytes(b"x" * 2048)
    with pytest.raises(SingleOpenError, match="size limit"):
        read_verified_bytes(oversized, maximum_size=1024)
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    with pytest.raises(SingleOpenError, match="empty"):
        read_verified_bytes(empty, maximum_size=1024)
