import pytest

from agenten.coding_agent.acceptance import changed_paths, run_acceptance
from agenten.coding_agent.sandbox import SandboxCommand, SandboxProtocolError
from tests.coding_agent.fakes import FakeSandbox, ok


@pytest.mark.asyncio
async def test_acceptance_runs_with_cache_purge_and_pythondontwritebytecode():
    def handler(image, command):
        if "fails" in command.shell:
            return ok("E" * 5000, exit_code=1)
        return ok("passed")

    sandbox = FakeSandbox(handler)
    report = await run_acceptance(
        sandbox, image_id="img-1", commands=("pytest -q a", "pytest -q fails"), workdir="/work", timeout_seconds=60
    )

    assert [c.passed for c in report.checks] == [True, False]
    assert report.passed is False
    assert len(report.checks[1].output_tail) == 2000
    assert all(img == "img-1" for img, _ in sandbox.calls)
    # Check exact shell string includes purge + PYTHONDONTWRITEBYTECODE + subshell wrapping
    expected_shell = (
        "cd /work && find . -path ./.git -prune -o \\( -name __pycache__ -o -name .pytest_cache "
        "-o -name .mypy_cache -o -name .ruff_cache \\) -prune -exec rm -rf {} + && "
        "export PYTHONDONTWRITEBYTECODE=1 && ( pytest -q a\n)"
    )
    assert sandbox.calls[0][1].shell == expected_shell
    assert sandbox.calls[0][1].env == {}


@pytest.mark.asyncio
async def test_acceptance_wraps_command_with_semicolon_in_subshell():
    """Verify that commands containing ';' are wrapped in parentheses to prevent shell injection after purge failure."""
    def handler(image, command):
        return ok("ok")

    sandbox = FakeSandbox(handler)
    report = await run_acceptance(
        sandbox, image_id="img-1", commands=("cmd1; cmd2",), workdir="/app", timeout_seconds=60
    )

    # Command with ";" must be wrapped in parentheses
    assert "( cmd1; cmd2\n)" in sandbox.calls[0][1].shell
    assert report.checks[0].passed is True
    assert all(img == "img-1" for img, _ in sandbox.calls)


@pytest.mark.asyncio
async def test_timeout_fails_the_check():
    sandbox = FakeSandbox(lambda image, command: ok(exit_code=0, timed_out=True))
    report = await run_acceptance(sandbox, image_id="i", commands=("x",), workdir="/work", timeout_seconds=5)
    assert report.checks[0].passed is False


@pytest.mark.asyncio
async def test_changed_paths_detects_added_removed_modified():
    def handler(image, command):
        if "sha256sum" in command.shell:
            if image == "img-before":
                # src/old.py removed, src/app.py modified, tests/existing.py unchanged
                return ok("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2  ./src/old.py\0"
                          "0000000000000000000000000000000000000000000000000000000000000000  ./src/app.py\0"
                          "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff  ./tests/existing.py\0")
            else:  # img-after
                # src/app.py modified (different hash), tests/new.py added, tests/existing.py unchanged
                return ok("9999999999999999999999999999999999999999999999999999999999999999  ./src/app.py\0"
                          "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  ./tests/new.py\0"
                          "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff  ./tests/existing.py\0")
        return ok()

    sandbox = FakeSandbox(handler)
    paths = await changed_paths(sandbox, before_image_id="img-before", after_image_id="img-after", workdir="/work")
    # src/old.py (removed), src/app.py (modified), tests/new.py (added) — existing.py not included
    assert paths == ("src/app.py", "src/old.py", "tests/new.py")


@pytest.mark.asyncio
async def test_changed_paths_handles_renames_reports_both_paths():
    def handler(image, command):
        if "sha256sum" in command.shell:
            if image == "before":
                return ok("abc123abc123abc123abc123abc123abc123abc123abc123abc123abc123abc1  ./old_name.py\0")
            else:  # after
                return ok("abc123abc123abc123abc123abc123abc123abc123abc123abc123abc123abc1  ./new_name.py\0")
        return ok()

    sandbox = FakeSandbox(handler)
    paths = await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/app")
    # Rename: old_name.py is gone (removed), new_name.py is added — report both
    assert paths == ("new_name.py", "old_name.py")


@pytest.mark.asyncio
async def test_changed_paths_handles_special_filenames():
    def handler(image, command):
        if "sha256sum" in command.shell:
            # Filenames: space, literal " -> ", non-ASCII, double quote
            manifest = ("1111111111111111111111111111111111111111111111111111111111111111  ./file with spaces.py\0"
                        "2222222222222222222222222222222222222222222222222222222222222222  ./forbidden -> src/ok.py\0"
                        "3333333333333333333333333333333333333333333333333333333333333333  ./café.py\0"
                        "4444444444444444444444444444444444444444444444444444444444444444  ./\"quoted.txt\0")
            if image == "before":
                return ok(manifest)
            else:  # after: all changed
                return ok("")
        return ok()

    sandbox = FakeSandbox(handler)
    paths = await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")
    # All four files are removed (changed)
    assert sorted(paths) == sorted(("file with spaces.py", "forbidden -> src/ok.py", "café.py", "\"quoted.txt"))


@pytest.mark.asyncio
async def test_changed_paths_narrow_cache_rules():
    """Test narrow cache exclusion: only __pycache__/*.pyc and top-level cache dirs."""
    def handler(image, command):
        if "sha256sum" in command.shell:
            if image == "before":
                return ok("")
            else:
                # Exclude: a/__pycache__/m.cpython-311.pyc, .pytest_cache/v/x
                # Report: src/__pycache__/payload.py, src/x.pyc, pkg/.mypy_cache/x, cache.py, pycache_notes/x.py
                manifest = (
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  ./a/__pycache__/m.cpython-311.pyc\0"
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  ./.pytest_cache/v/x\0"
                    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc  ./src/__pycache__/payload.py\0"
                    "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd  ./src/x.pyc\0"
                    "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee  ./pkg/.mypy_cache/x\0"
                    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff  ./cache.py\0"
                    "1010101010101010101010101010101010101010101010101010101010101010  ./pycache_notes/x.py\0"
                )
                return ok(manifest)
        return ok()

    sandbox = FakeSandbox(handler)
    paths = await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")
    # Excluded: a/__pycache__/m.cpython-311.pyc, .pytest_cache/v/x
    # Reported: src/__pycache__/payload.py, src/x.pyc, pkg/.mypy_cache/x, cache.py, pycache_notes/x.py
    assert sorted(paths) == sorted((
        "src/__pycache__/payload.py", "src/x.pyc", "pkg/.mypy_cache/x",
        "cache.py", "pycache_notes/x.py"
    ))


@pytest.mark.asyncio
async def test_changed_paths_excludes_caches():
    def handler(image, command):
        if "sha256sum" in command.shell:
            if image == "before":
                return ok("")
            else:
                # Only top-level .pytest_cache is excluded, not src/.pytest_cache
                # src/app.pyc is sourceless (not in __pycache__), so reported
                # __pycache__/foo.pyc is excluded (parent is __pycache__)
                # .pytest_cache/v is excluded (top-level)
                manifest = ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  ./__pycache__/foo.pyc\0"
                            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  ./src/app.pyc\0"
                            "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc  ./.pytest_cache/v\0"
                            "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd  ./src/app.py\0")
                return ok(manifest)
        return ok()

    sandbox = FakeSandbox(handler)
    paths = await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")
    # Excluded: __pycache__/foo.pyc, .pytest_cache/v (top-level cache)
    # Reported: src/app.pyc (sourceless), src/app.py
    assert sorted(paths) == sorted(("src/app.py", "src/app.pyc"))


@pytest.mark.asyncio
async def test_changed_paths_validates_manifest_format():
    def handler(image, command):
        if "sha256sum" in command.shell:
            # Malformed: no "  " separator (only one space)
            return ok("abcd1234 ./file.py\0")
        return ok()

    sandbox = FakeSandbox(handler)
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")


@pytest.mark.asyncio
async def test_changed_paths_validates_hash_is_hex():
    def handler(image, command):
        if "sha256sum" in command.shell:
            # Malformed: hash is 64 chars but not hex (has 'Z')
            return ok("abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcZ  ./file.py\0")
        return ok()

    sandbox = FakeSandbox(handler)
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")


@pytest.mark.asyncio
async def test_changed_paths_rejects_digest_with_trailing_newline():
    def handler(image, command):
        if "sha256sum" in command.shell:
            # Malformed: valid hex digest followed by newline (fullmatch should reject trailing chars)
            return ok("abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdef\n  ./file.py\0")
        return ok()

    sandbox = FakeSandbox(handler)
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")


@pytest.mark.asyncio
async def test_changed_paths_validates_hash_length():
    def handler(image, command):
        if "sha256sum" in command.shell:
            # Malformed: hash is too short (not 64 hex chars)
            return ok("abc123  ./file.py\0")
        return ok()

    sandbox = FakeSandbox(handler)
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")


@pytest.mark.asyncio
async def test_changed_paths_validates_path_prefix():
    def handler(image, command):
        if "sha256sum" in command.shell:
            # Malformed: path doesn't start with "./"
            return ok("1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef  file.py\0")
        return ok()

    sandbox = FakeSandbox(handler)
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")


@pytest.mark.asyncio
async def test_changed_paths_rejects_dotdot_segments():
    def handler(image, command):
        if "sha256sum" in command.shell:
            # Malformed: path contains ".." segment
            return ok("1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef  ./src/../etc/passwd\0")
        return ok()

    sandbox = FakeSandbox(handler)
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")


@pytest.mark.asyncio
async def test_changed_paths_rejects_empty_path():
    def handler(image, command):
        if "sha256sum" in command.shell:
            # Malformed: path is just "./"
            return ok("1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef  ./\0")
        return ok()

    sandbox = FakeSandbox(handler)
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")


@pytest.mark.asyncio
async def test_changed_paths_manifest_command_exact_with_pipefail():
    calls_made = []
    def handler(image, command):
        calls_made.append(command.shell)
        if "sha256sum" in command.shell:
            return ok("")
        return ok()

    sandbox = FakeSandbox(handler)
    await changed_paths(sandbox, before_image_id="img-1", after_image_id="img-2", workdir="/work")

    # Verify the exact manifest command literal: derived from _manifest_command("/work")
    expected_manifest_cmd = (
        "set -o pipefail; cd /work && "
        "find . -path ./.git -prune -o -type f -print0 "
        "| sort -z | xargs -0 -r sha256sum -z -- && "
        "find . -path ./.git -prune -o -type l -printf 'L\\0%p\\0%Y\\0%l\\0'"
    )
    manifest_calls = [c for c in calls_made if "sha256sum" in c]
    assert len(manifest_calls) == 2
    for call in manifest_calls:
        assert call == expected_manifest_cmd


@pytest.mark.asyncio
async def test_changed_paths_raises_on_manifest_exit_code():
    sandbox = FakeSandbox(lambda image, command: ok("error", exit_code=1) if "sha256sum" in command.shell else ok())
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")


@pytest.mark.asyncio
async def test_changed_paths_raises_on_manifest_timeout():
    sandbox = FakeSandbox(lambda image, command: ok(timed_out=True) if "sha256sum" in command.shell else ok())
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")


@pytest.mark.asyncio
async def test_changed_paths_invokes_find_sha256sum_correctly():
    calls_made = []
    def handler(image, command):
        calls_made.append(command.shell)
        if "sha256sum" in command.shell:
            return ok("")
        return ok()

    sandbox = FakeSandbox(handler)
    await changed_paths(sandbox, before_image_id="img-1", after_image_id="img-2", workdir="/test/path")

    # Verify the exact manifest command is used
    manifest_calls = [c for c in calls_made if "sha256sum" in c]
    assert len(manifest_calls) == 2
    for call in manifest_calls:
        assert "find . -path ./.git -prune" in call
        assert "-print0" in call
        assert "sha256sum -z" in call
        assert "cd /test/path" in call


def test_protected_paths_identifies_judge_rigging_files():
    from agenten.coding_agent.acceptance import protected_paths

    changed = (
        "src/a.py", "tests/conftest.py", "pytest.ini", "setup.cfg", "pyproject.toml",
        ".coveragerc", "lib/x.pth", "sitecustomize.py", "normal.py"
    )
    protected = protected_paths(changed)
    assert sorted(protected) == sorted((
        "tests/conftest.py", "pytest.ini", "setup.cfg", "pyproject.toml",
        ".coveragerc", "lib/x.pth", "sitecustomize.py"
    ))


def test_protected_paths_includes_sourceless_pyc():
    from agenten.coding_agent.acceptance import protected_paths

    # lib/evil.pyc is not in __pycache__ so it's not a cache file, thus protected
    changed = ("lib/good.py", "lib/evil.pyc")
    protected = protected_paths(changed)
    assert protected == ("lib/evil.pyc",)


def test_protected_paths_exempt_via_explicitly_allowed():
    from agenten.coding_agent.acceptance import protected_paths

    changed = ("src/conftest.py", "tests/pytest.ini", "lib/setup.cfg")

    # No explicit allowance
    protected = protected_paths(changed)
    assert sorted(protected) == sorted(("src/conftest.py", "tests/pytest.ini", "lib/setup.cfg"))

    # Exact path in explicitly_allowed
    protected = protected_paths(changed, explicitly_allowed=("src/conftest.py",))
    assert sorted(protected) == sorted(("lib/setup.cfg", "tests/pytest.ini"))

    # Directory prefix does NOT exempt
    protected = protected_paths(changed, explicitly_allowed=("src/",))
    assert sorted(protected) == sorted(("src/conftest.py", "tests/pytest.ini", "lib/setup.cfg"))

    # "." does NOT exempt
    protected = protected_paths(changed, explicitly_allowed=(".",))
    assert sorted(protected) == sorted(("src/conftest.py", "tests/pytest.ini", "lib/setup.cfg"))


def test_protected_paths_handles_nested_paths():
    from agenten.coding_agent.acceptance import protected_paths

    changed = ("build/conftest.py", "src/pytest.ini", "lib/deep/x.pth")
    protected = protected_paths(changed)
    # Only the basename is checked
    assert sorted(protected) == sorted(("build/conftest.py", "src/pytest.ini", "lib/deep/x.pth"))


@pytest.mark.asyncio
async def test_changed_paths_hidden_files_still_reported():
    """Verify that .gitignore-style hiding is irrelevant: manifest reports all files."""
    def handler(image, command):
        if "sha256sum" in command.shell:
            if image == "before":
                return ok("")
            else:
                # build/secret.txt would normally be .gitignore'd but is in manifest
                return ok("2020202020202020202020202020202020202020202020202020202020202020  ./build/secret.txt\0")
        return ok()

    sandbox = FakeSandbox(handler)
    paths = await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")
    assert paths == ("build/secret.txt",)


def test_sandbox_command_repr_hides_env_and_stdin():
    command = SandboxCommand(shell="claude -p", timeout_seconds=60, env={"ANTHROPIC_AUTH_TOKEN": "tok-secret"}, stdin="prompt")
    assert "tok-secret" not in repr(command)
    assert "prompt" not in repr(command)


# --- I1: symlinks are recorded by link text, never followed -----------------------------

D1 = "1" * 64
D2 = "2" * 64


def _link(path, target, kind="f"):
    return f"L\0./{path}\0{kind}\0{target}\0"


def test_manifest_command_hashes_regular_files_and_lists_symlinks_without_following():
    from agenten.coding_agent.acceptance import _manifest_command

    assert _manifest_command("/work") == (
        "set -o pipefail; cd /work && "
        "find . -path ./.git -prune -o -type f -print0 "
        "| sort -z | xargs -0 -r sha256sum -z -- && "
        "find . -path ./.git -prune -o -type l -printf 'L\\0%p\\0%Y\\0%l\\0'"
    )
    # sha256sum (which follows links) is only ever fed regular files
    hashing_find = _manifest_command("/work").split("| sort -z")[0]
    assert "-type l" not in hashing_find


@pytest.mark.asyncio
async def test_manifest_records_directory_and_dangling_symlinks_by_link_text():
    from agenten.coding_agent.acceptance import workspace_manifest

    stdout = f"{D1}  ./src/app.py\0" + _link(".venv/lib64", "lib", "d") + _link("src/gone", "missing.py", "N")
    sandbox = FakeSandbox(lambda image, command: ok(stdout))
    manifest = await workspace_manifest(sandbox, image_id="img", workdir="/work")
    assert manifest == {
        "src/app.py": D1,
        ".venv/lib64": "symlink:lib",
        "src/gone": "symlink:missing.py",
    }


@pytest.mark.asyncio
async def test_unchanged_directory_symlink_is_not_a_change():
    stdout = f"{D1}  ./src/app.py\0" + _link(".venv/lib64", "lib", "d")
    sandbox = FakeSandbox(lambda image, command: ok(stdout))
    paths = await changed_paths(sandbox, before_image_id="a", after_image_id="b", workdir="/work")
    assert paths == ()


@pytest.mark.asyncio
async def test_added_removed_and_retargeted_symlinks_are_changed_paths():
    def handler(image, command):
        if image == "before":
            return ok(_link("src/alias.py", "a.py") + _link("src/old", "x.py"))
        return ok(_link("src/alias.py", "b.py") + _link("src/new", "y.py"))

    paths = await changed_paths(FakeSandbox(handler), before_image_id="before", after_image_id="after", workdir="/work")
    assert paths == ("src/alias.py", "src/new", "src/old")


@pytest.mark.asyncio
async def test_file_replaced_by_symlink_is_a_changed_path():
    def handler(image, command):
        if image == "before":
            return ok(f"{D1}  ./src/a.py\0")
        return ok(_link("src/a.py", "b.py"))

    paths = await changed_paths(FakeSandbox(handler), before_image_id="before", after_image_id="after", workdir="/work")
    assert paths == ("src/a.py",)


@pytest.mark.parametrize(
    "stdout",
    [
        "L\0./x\0f\0",  # truncated link record
        "L\0./x\0f\0target",  # missing terminator
        "L\0./x\0f\0\0",  # empty target
        "L\0x\0f\0t\0",  # path without ./
        "L\0./a/../b\0f\0t\0",  # dot-dot path
        "L\0./x\0ff\0t\0",  # bad kind
        "L\0./x\0f\0t\0" + "L\0./x\0f\0u\0",  # duplicate path
        f"{D1}  ./x\0" + "L\0./x\0f\0t\0",  # same path as file and link
        f"{D1}  ./x",  # unterminated file record
        f"{D1}  ./x\0\0",  # empty record
    ],
)
@pytest.mark.asyncio
async def test_manifest_parsing_is_strict(stdout):
    from agenten.coding_agent.acceptance import workspace_manifest

    sandbox = FakeSandbox(lambda image, command: ok(stdout))
    with pytest.raises(SandboxProtocolError):
        await workspace_manifest(sandbox, image_id="img", workdir="/work")


@pytest.mark.asyncio
async def test_workspace_changes_exposes_changed_symlinks_of_the_after_image():
    from agenten.coding_agent.acceptance import SymlinkEntry, workspace_changes

    def handler(image, command):
        if image == "before":
            return ok(_link(".venv/lib64", "lib", "d"))
        return ok(_link(".venv/lib64", "lib", "d") + _link("src/evil", "/etc/passwd", "f"))

    changes = await workspace_changes(FakeSandbox(handler), before_image_id="before", after_image_id="after", workdir="/work")
    assert changes.paths == ("src/evil",)
    assert changes.symlinks == (SymlinkEntry(path="src/evil", target="/etc/passwd", target_kind="f"),)


@pytest.mark.parametrize(
    "path,target,kind,unsafe",
    [
        ("src/a.py", "b.py", "f", False),
        ("src/a.py", "../lib/b.py", "f", False),
        ("src/gone", "missing.py", "N", False),
        ("src/abs", "/etc/passwd", "f", True),
        ("src/up", "../../outside", "N", True),
        ("top", "..", "d", True),
        ("src/dir", "pkg", "d", True),
        ("src/git", "../.git/config", "f", True),
    ],
)
def test_unsafe_symlinks(path, target, kind, unsafe):
    from agenten.coding_agent.acceptance import SymlinkEntry, unsafe_symlinks

    entry = SymlinkEntry(path=path, target=target, target_kind=kind)
    assert unsafe_symlinks((entry,)) == ((path,) if unsafe else ())


@pytest.mark.asyncio
async def test_trailing_comment_cannot_swallow_the_subshell_close():
    sandbox = FakeSandbox(lambda image, command: ok("ok"))
    await run_acceptance(sandbox, image_id="i", commands=("pytest -q  # all tests",), workdir="/work", timeout_seconds=60)
    assert sandbox.calls[0][1].shell.endswith("&& ( pytest -q  # all tests\n)")
